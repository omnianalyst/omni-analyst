"""Historical on-chain flow: one address's transaction history, windowed.

``onchain.py::parse_flows`` reads ONE block, fetched through
``eth_getBlockByNumber``. That answers "what happened just now" and cannot
backfill: Ethereum produces ~7,200 blocks a day, so a year is ~2.6M blocks and
the free Etherscan tier allows 5 calls a second. A block-at-a-time backfill of
one year is ~144 hours of block fetches, nearly all of them returning
transactions that touch no address anyone is tracking.

The efficient shape is the transpose: ask Etherscan for one ADDRESS's
transaction list over a block range (``module=account&action=txlist``), which
returns every transfer touching that address in pages of up to 100. Measured
against the live API with a real key, ``Binance 14`` returned 100 transactions
spanning 55 blocks -- roughly 4.8M transactions per address-year, ~48,000 paged
calls, ~2.7 hours at 5 calls a second. That runtime is why this module is
windowed and resumable through ``chain/cursor.py`` rather than a loop: a process
that dies at hour two must resume where it stopped, with no gap and no
re-emission. One call here reads one window; the caller drives the sequence.

**Truncation is the failure this module exists to refuse.** Etherscan caps a
single query at 10,000 results however it is paged, and says nothing when it
hits the cap -- the response for a window holding 40,000 transfers is
indistinguishable from one holding exactly 10,000. Accepting it silently drops
an unknown subset of transfers, and an exchange-reserve total computed from the
survivors is wrong by an unknown amount while looking entirely healthy.
``fetch_address_history`` therefore treats a full 10,000 as truncated and raises
``WindowTruncated``; ``backfill_address`` halves the window and retries, down to
a single block, and raises rather than record a window it could not read whole.

Bitemporal: ``event_date`` and ``knowledge_date`` are both the transaction's own
``timeStamp``. A confirmed transaction is public the moment it is mined. Never
``now()``.

Vocabulary is ``onchain.py``'s, exactly: a transfer TO the tracked address is
``kind="exchange_inflow"`` / ``direction="inflow"``, FROM it is
``kind="exchange_outflow"`` / ``direction="outflow"``, and the size is
``amount_eth``. ``conviction/reserve.py`` and
``conviction/convergence_producer.py`` read those strings and that field; a
synonym here reads downstream as no flow at all.

Pacing is the caller's: nothing here sleeps between pages, because the rate
budget is shared across every adapter the scheduler runs and cannot be decided
one module at a time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from omni.ingest.chain.cursor import advance, next_range
from omni.ingest.labels import normalise_address
from omni.ingest.onchain import (
    ETHERSCAN_URL,
    FLOW,
    KNOWN_EXCHANGES,
    _from_unix,
    _params,
    _require_ok,
)
from omni.ingest.protocol import ClaimDraft, Unavailable, get_json

SOURCE = "etherscan"
PROVIDER_KEY = "etherscan"

# Etherscan serves at most this many results for one query, regardless of paging.
RESULT_CAP = 10_000
DEFAULT_PAGE_SIZE = 100

# The documented empty-set answer, which arrives error-SHAPED (status "0") with
# an empty list result. An empty block range is a normal outcome for a backfill,
# not a source failure, so it must not reach `_require_ok`.
_EMPTY_MESSAGE = "no transactions found"

_LAST_BLOCK_AT = "SELECT last_block_at FROM chain_cursor WHERE chain = $1"

HistoryFetcher = Callable[[dict[str, Any]], Awaitable[Any]]


class WindowTruncated(Unavailable):
    """A window returned Etherscan's 10,000-result cap.

    Subclasses ``Unavailable`` deliberately: if it escapes ``backfill_address``
    (a single block that alone exceeds the cap cannot be subdivided further) the
    fill pipeline records an honest ``unfillable`` rather than a crash. What it
    must never become is a silently short list of transfers.
    """


def _rows(payload: Any, *, what: str) -> list[Any]:
    """The result array, or a refusal.

    Etherscan reports "no transactions in this range" as ``status: "0"`` with
    ``message: "No transactions found"`` and an empty LIST result, while a real
    error is ``status: "0"`` with a STRING result. Only the first is data, so it
    is separated here before `_require_ok` refuses everything else.
    """
    if isinstance(payload, dict) and str(payload.get("status")) == "0":
        message = str(payload.get("message") or "").strip().lower()
        if message.startswith(_EMPTY_MESSAGE) and isinstance(payload.get("result"), list):
            return []
    result = _require_ok(payload, what=what).get("result")
    if not isinstance(result, list):
        raise Unavailable(
            f"Etherscan returned no result array for {what}; got {type(result).__name__}"
        )
    return result


def _wei(raw: Any) -> int | None:
    """``value`` as an exact integer count of wei, or ``None`` if unparseable.

    Etherscan sends ``value`` as a decimal STRING. A float would truncate
    silently through ``int()`` and a bool would parse as 1 wei, so neither is
    accepted: a value this module cannot read exactly is a skipped transaction,
    never an approximated one.
    """
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    try:
        wei = int(raw)
    except ValueError:
        return None
    return wei if wei >= 0 else None


def _block_number(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _latest_timestamp(txs: list[dict[str, Any]]) -> datetime | None:
    """The newest transaction timestamp in the window, from the RAW rows.

    Read before any skip rule applies: a zero-value or failed transaction still
    attests the time of the block it sits in, and that attestation is what the
    cursor's ``last_block_at`` records.
    """
    seen: list[datetime] = []
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        when = _from_unix(tx.get("timeStamp"))
        if when is not None:
            seen.append(when)
    return max(seen) if seen else None


def cursor_key(chain: str, address: str) -> str:
    """The cursor row this address's traversal advances.

    Traversal here is per ADDRESS, so it cannot share the plain per-chain cursor:
    blocks read for Binance 14 say nothing about what was read for Coinbase 1,
    and one shared row would mark the second address's unread blocks as covered.
    The ``:`` separator keeps these keys disjoint from the bare chain cursor
    ``onchain.py``'s block traversal uses.
    """
    return f"{chain}:{normalise_address(chain, address)}"


async def fetch_address_history(
    *,
    address: str,
    start_block: int,
    end_block: int,
    api_key: str,
    fetch_fn: HistoryFetcher | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Every transaction touching ``address`` in ``[start_block, end_block]``.

    Pages ``sort=asc`` from page 1 until a page comes back shorter than
    ``page_size`` (the last page) -- Etherscan does not report a total, so a
    short page is the only end-of-results signal.

    Raises ``WindowTruncated`` the moment the accumulated result reaches
    ``RESULT_CAP``. At the cap the response is ambiguous between "exactly 10,000
    transfers" and "some unknown larger number, of which you were handed the
    first 10,000", and nothing in the payload distinguishes them. Returning the
    list would hand the caller a flow total that is short by an unknown amount.
    """
    if not 1 <= page_size <= RESULT_CAP:
        raise ValueError(f"page_size must be in 1..{RESULT_CAP}, got {page_size}")
    if start_block < 0:
        raise ValueError("start_block cannot be negative")
    if end_block < start_block:
        raise ValueError("end_block precedes start_block")

    if fetch_fn is not None:
        return await _paginate(
            fetch_fn,
            address=address,
            start_block=start_block,
            end_block=end_block,
            api_key=api_key,
            page_size=page_size,
        )

    if not api_key:
        raise Unavailable("no Etherscan API key configured")

    import httpx

    # One client for the whole window: a window is up to 100 requests, and a
    # fresh connection per page pays the TLS handshake 100 times.
    async with httpx.AsyncClient(timeout=30.0) as client:

        async def fetch(params: dict[str, Any]) -> Any:
            resp = await get_json(client, ETHERSCAN_URL, params=params)
            if resp.status_code != 200:
                raise Unavailable(
                    f"Etherscan txlist returned HTTP {resp.status_code} for {address}"
                )
            return resp.json()

        return await _paginate(
            fetch,
            address=address,
            start_block=start_block,
            end_block=end_block,
            api_key=api_key,
            page_size=page_size,
        )


async def _paginate(
    fetch: HistoryFetcher,
    *,
    address: str,
    start_block: int,
    end_block: int,
    api_key: str,
    page_size: int,
) -> list[dict[str, Any]]:
    what = f"txlist for {address} in blocks {start_block}-{end_block}"
    collected: list[dict[str, Any]] = []
    max_pages = -(-RESULT_CAP // page_size)

    for page in range(1, max_pages + 1):
        params = _params(
            api_key,
            module="account",
            action="txlist",
            address=address,
            startblock=start_block,
            endblock=end_block,
            page=page,
            offset=page_size,
            sort="asc",
        )
        rows = _rows(await fetch(params), what=what)
        collected.extend(row for row in rows if isinstance(row, dict))

        if len(collected) >= RESULT_CAP:
            raise WindowTruncated(
                f"Etherscan capped {what} at {RESULT_CAP} results; the window is "
                "truncated by an unknown amount and cannot be treated as complete"
            )
        if len(rows) < page_size:
            return collected

    raise WindowTruncated(
        f"Etherscan capped {what} at {RESULT_CAP} results; the window is "
        "truncated by an unknown amount and cannot be treated as complete"
    )


def parse_address_history(
    txs: list[dict[str, Any]],
    *,
    address: str,
    chain: str = "eth",
    label: str | None = None,
) -> list[ClaimDraft]:
    """Flatten an address's transaction list into flow drafts.

    Direction is the tracked address's side of the transfer, exactly as
    ``onchain.py::parse_flows`` decides it: TO the address is
    ``exchange_inflow``, FROM it is ``exchange_outflow``. ``event_date`` and
    ``knowledge_date`` are both the transaction's ``timeStamp`` -- a confirmed
    transaction is public the moment it is mined. The claim ``key`` is the
    transaction hash, so a window re-read after a crash dedupes rather than
    double-counting.

    Four kinds of row are skipped, each because emitting it would state
    something untrue about the reserve:

    - **Zero-value.** Still a transaction, but it moved no ETH -- typically an
      ERC-20 transfer or contract call, whose ETH ``value`` is 0. It is skipped,
      not emitted: ``reserve.py`` gates on the COUNT of labelled flows
      (``len(signed_flows) < window``), so emitting them would let forty
      contract calls that moved nothing satisfy a threshold meant to require
      forty real transfers. The test is ``wei == 0`` on the parsed INTEGER, an
      exact comparison; the float ETH amount is never compared to zero.
    - **Failed.** ``isError == "1"`` means the transaction reverted and the
      stated ``value`` never moved. Counting it is fabricating a transfer.
    - **Self-transfer**, where both sides are the tracked address: nothing
      crossed the exchange boundary, and taking the ``to`` side first (as the
      one-block parser does, where the case cannot arise) would book it as an
      inflow that never arrived.
    - **Unreadable**: no ``timeStamp`` (no honest ``event_date``), no ``hash``
      (no dedupe identity), or a ``value`` that is not an exact integer.

    Neighbouring rows are unaffected by a skip -- one malformed transaction does
    not discard the window.
    """
    addr = normalise_address(chain, address)
    exchange = label if label is not None else KNOWN_EXCHANGES.get(addr)

    drafts: list[ClaimDraft] = []
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        when = _from_unix(tx.get("timeStamp"))
        if when is None:
            continue
        tx_hash = tx.get("hash")
        if not tx_hash or not isinstance(tx_hash, str):
            continue
        if str(tx.get("isError") or "0") == "1":
            continue
        wei = _wei(tx.get("value"))
        if wei is None or wei == 0:
            continue

        to_addr = normalise_address(chain, tx.get("to") or "")
        from_addr = normalise_address(chain, tx.get("from") or "")
        if to_addr == addr and from_addr == addr:
            continue
        if to_addr == addr:
            direction = "inflow"
        elif from_addr == addr:
            direction = "outflow"
        else:
            continue

        # This division is where precision is lost: wei above 2**53 (~0.009 ETH)
        # no longer round-trips through binary64, so `amount_eth` is an
        # approximation for every transfer of consequence. The exact integer is
        # carried alongside it as `amount_wei`, and as a STRING so it survives a
        # JSON reader that treats every number as a double.
        amount_eth = wei / 1e18

        drafts.append(
            ClaimDraft(
                claim_type=FLOW,
                key=tx_hash,
                value={
                    "kind": f"exchange_{direction}",
                    "exchange": exchange,
                    "direction": direction,
                    "amount_eth": amount_eth,
                    "amount_wei": str(wei),
                    "from": from_addr,
                    "to": to_addr,
                    "chain": chain,
                },
                event_date=when,
                knowledge_date=when,
                confidence=1.0,
                unit="ETH",
                evidence={
                    "block": _block_number(tx.get("blockNumber")),
                    "address": addr,
                },
            )
        )
    return drafts


@dataclass(frozen=True)
class HistoryReport:
    address: str
    chain: str
    cursor: str
    caught_up: bool = False
    advanced: bool = False
    start_block: int | None = None
    end_block: int | None = None
    requested_end_block: int | None = None
    transactions: int = 0
    halvings: int = 0
    drafts: tuple[ClaimDraft, ...] = ()

    @property
    def skipped(self) -> int:
        return self.transactions - len(self.drafts)


async def backfill_address(
    pool,
    *,
    address: str,
    chain: str,
    api_key: str,
    head_block: int,
    max_span: int,
    fetch_fn: HistoryFetcher | None = None,
    label: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> HistoryReport:
    """Read ONE resumable window of an address's history and advance its cursor.

    The range comes from ``cursor.next_range`` and the cursor moves through
    ``cursor.advance``; no block-range arithmetic is repeated here. A run killed
    mid-backfill resumes at ``last_block + 1`` on the next call -- no gap, and no
    window re-read.

    On ``WindowTruncated`` the window is HALVED and retried, repeatedly, because
    a truncated window's contents are unknowable and a smaller one may not be.
    ``end_block`` in the report is the end of the window actually read whole,
    which is what the cursor advances to; the blocks the halving gave up are
    picked up by the next call, so halving costs time, never coverage. A single
    block that still hits the cap cannot be subdivided, and raises.

    Returns without advancing when nothing was read and no prior cursor time
    exists (``advanced=False``): ``chain_cursor.last_block_at`` is NOT NULL and
    an empty first window supplies no observed block time, and inventing one is
    the fabrication this codebase refuses. Any later window containing a single
    transaction resolves it permanently.
    """
    key = cursor_key(chain, address)
    rng = await next_range(pool, key, head=head_block, max_span=max_span)
    if rng is None:
        return HistoryReport(address=address, chain=chain, cursor=key, caught_up=True)

    start = rng.start_block
    end = rng.end_block
    halvings = 0
    while True:
        try:
            txs = await fetch_address_history(
                address=address,
                start_block=start,
                end_block=end,
                api_key=api_key,
                fetch_fn=fetch_fn,
                page_size=page_size,
            )
            break
        except WindowTruncated:
            if end <= start:
                # One block, still over the cap: there is no smaller window, so
                # this range cannot be read whole. Refusing keeps the cursor
                # where it is; accepting would silently drop transfers.
                raise
            halvings += 1
            end = start + max(1, (end - start + 1) // 2) - 1

    drafts = parse_address_history(txs, address=address, chain=chain, label=label)

    # The newest timestamp OBSERVED in the window. It is at or below the
    # timestamp of `end` itself; reading that block's own header would cost an
    # extra call per window (tens of thousands across a year) and would not
    # change a single claim, so the cursor records what was seen rather than a
    # value fetched to make a column look tidy.
    block_time = _latest_timestamp(txs)
    if block_time is None:
        block_time = await pool.fetchval(_LAST_BLOCK_AT, key)
    if block_time is None:
        return HistoryReport(
            address=address,
            chain=chain,
            cursor=key,
            advanced=False,
            start_block=start,
            end_block=end,
            requested_end_block=rng.end_block,
            transactions=len(txs),
            halvings=halvings,
            drafts=tuple(drafts),
        )

    await advance(pool, key, to_block=end, block_time=block_time)
    return HistoryReport(
        address=address,
        chain=chain,
        cursor=key,
        advanced=True,
        start_block=start,
        end_block=end,
        requested_end_block=rng.end_block,
        transactions=len(txs),
        halvings=halvings,
        drafts=tuple(drafts),
    )
