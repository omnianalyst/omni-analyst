"""Microstructure ingestion: order book snapshot and trade tape.

Two consumers are currently working from assumptions, and this adapter exists to
give both a measured number instead:

1. ``venue/costs.py::entry_cost`` charges a caller-supplied ``spread_bps`` on
   every taker fill. That constant is a guess; the real spread is the distance
   between the best bid and the best ask on the venue the fill would route to,
   and the router accepts or rejects strategies on it.
2. ``detect/manipulation.py`` runs on OHLCV percentiles. Wash trading does not
   show up in a daily bar; it shows up in the tape and the book. Without this
   data the detector literally cannot see what it was built to see.

Sampled, not streamed. A book snapshot and a trade are point-in-time
observations a gap-filler fetches on demand; a streaming path is a later phase.

The targeted venue is OKX (V5 ``/api/v5/market/books`` and
``/api/v5/market/trades``). Those endpoints are public and keyless, but OKX's
market-data terms still restrict serving the data on to third parties. The
credential catalog therefore classifies OKX as ``byo_only``: user-scoped demand
can write claims pinned to that user, while shared demand without an owner is
refused. The adapter declares ``provider_key = venue`` because the licence is
per venue; collapsing every venue onto one key would give one venue's terms to
all of them. ``ClaimDraft`` has no audience field, so the writer remains the
single place that enforces this decision.

Routing mirrors ``OnChainAdapter``/``DerivativesAdapter``: a ``"<kind>:<symbol>"``
key dispatches to one of two parse paths. An unknown kind raises
``Unavailable``, never returns ``[]`` -- an empty list reads as "nothing
happened in this window", a different fact from "I do not serve that kind".

**Refusing a crossed book is the single most important check in this file.**
``best_bid > best_ask`` is physically impossible in a live market (it is instant
risk-free arbitrage); a payload carrying it is stale, mis-parsed or pre-open
noise, never a market. A crossed book silently produces a *negative* spread,
which flows into ``costs.py`` as a negative cost, which makes an unprofitable
strategy look profitable. So it raises ``Unavailable``. A *locked* market
(``best_bid == best_ask``, spread zero) is the opposite case: it is a real, if
unusual, observation and is emitted -- zero spread is a legitimate reading, and
filtering it would be fabrication by omission. The two are distinguished by a
strict ``>``: the refusal is for a negative spread, not a zero one.

**``spread_bps`` is computed against the mid, never the bid or the ask.**
``costs.py::entry_cost`` charges the *half*-spread against the mid as the
reference price; a spread stated against the wrong reference is a systematic
bias in every cost estimate downstream. All price/size arithmetic is done in
``Decimal`` and stored as its decimal string, so a tight book sums and divides
without float representation error and a zero spread is an exact ``Decimal(0)``
-- never a ``1e-17`` that an ``== 0`` guard would miss.

Bitemporal rule: a book snapshot and a trade are knowable the instant they are
observed, so ``knowledge_date == event_date`` for every claim here, using the
venue's own ``ts``. A book with no timestamp raises ``Unavailable`` rather than
stamping ``now()`` -- an unstamped snapshot cannot be placed in time, and a
backtest reading it would be reading the present.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable, get_json

SOURCE = "microstructure"

ORDERBOOK_SNAPSHOT = "orderbook_snapshot"
TRADE_TAPE = "trade_tape"

BASE_URLS: dict[str, str] = {
    "okx": "https://www.okx.com",
}

BOOK_PATH = "/api/v5/market/books"
TRADES_PATH = "/api/v5/market/trades"

MicrostructureFetcher = Callable[[str], Awaitable[Any]]


def _event_date(ts: Any) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _decimal(value: Any) -> Decimal | None:
    """Parse a venue price/size string into ``Decimal`` without touching float.

    Returns ``None`` for a missing or unparseable value so the caller skips the
    entry rather than substituting a default. ``str(value)`` is fed to
    ``Decimal`` so a value already coerced by a JSON layer still parses
    faithfully, but OKX publishes every price and size as a decimal string and
    the spread must never round through float.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _raise_if_error(payload: Any, venue: str) -> None:
    if isinstance(payload, dict):
        code = payload.get("code")
        if code is not None and str(code) != "0":
            raise Unavailable(
                f"{venue} returned error code {code}: {payload.get('msg', 'no message')}"
            )


def parse_book(payload: Any, *, symbol: str, venue: str, depth: int = 20) -> list[ClaimDraft]:
    """Turn an OKX ``/api/v5/market/books`` response into one snapshot draft.

    OKX returns ``{"code", "msg", "data": [{"asks": [...], "bids": [...],
    "ts", "seqId"}]}``; each level is ``[price, size, ...]`` (only the first
    two elements are meaningful here). Bids arrive in descending price order,
    asks in ascending, so ``bids[0]``/``asks[0]`` are the tops.

    A crossed book (``best_bid > best_ask``) raises: it cannot be a market, and
    its negative spread would become a negative cost downstream. A locked book
    (``best_bid == best_ask``, spread zero) is a real observation and is
    emitted. An empty bid or ask side raises rather than reading the missing
    side as zero depth. A book with no ``ts`` raises rather than stamping
    ``now()``.

    ``spread_bps`` is ``(ask - bid) / mid * 10000`` -- against the mid, the
    reference price ``costs.py`` charges the half-spread against. The depth
    fields are the cumulative size across however many levels the book actually
    has up to ``depth``; a thin book is summed as-is, never padded with zeros.
    """
    _raise_if_error(payload, venue)
    data = payload.get("data") if isinstance(payload, dict) else None
    book = data[0] if data else {}
    bids = book.get("bids") or []
    asks = book.get("asks") or []

    if not bids:
        raise Unavailable(
            f"{venue} {symbol} book has no bids; refusing to treat a missing side as zero depth"
        )
    if not asks:
        raise Unavailable(
            f"{venue} {symbol} book has no asks; refusing to treat a missing side as zero depth"
        )

    best_bid = _decimal(bids[0][0]) if bids[0] else None
    best_ask = _decimal(asks[0][0]) if asks[0] else None
    if best_bid is None or best_ask is None:
        raise Unavailable(
            f"{venue} {symbol} book top of book is unparseable (bid={bids[0]!r}, ask={asks[0]!r})"
        )

    if best_bid > best_ask:
        raise Unavailable(
            f"crossed book for {symbol} on {venue}: "
            f"best_bid {best_bid} > best_ask {best_ask} -- not a market"
        )

    mid = (best_bid + best_ask) / Decimal(2)
    if mid <= 0:
        raise Unavailable(f"{venue} {symbol} book has a non-positive mid ({mid}); bad data")
    spread_absolute = best_ask - best_bid
    spread_bps = spread_absolute / mid * Decimal(10_000)

    bid_depth_n = Decimal(0)
    for level in bids[:depth]:
        size = _decimal(level[1]) if len(level) > 1 else None
        if size is not None:
            bid_depth_n += size
    ask_depth_n = Decimal(0)
    for level in asks[:depth]:
        size = _decimal(level[1]) if len(level) > 1 else None
        if size is not None:
            ask_depth_n += size

    when = _event_date(book.get("ts"))
    if when is None:
        raise Unavailable(
            f"{venue} {symbol} book snapshot carries no timestamp; refusing to stamp now()"
        )

    return [
        ClaimDraft(
            claim_type=ORDERBOOK_SNAPSHOT,
            key=f"{venue}:{symbol}",
            value={
                "best_bid": str(best_bid),
                "best_ask": str(best_ask),
                "mid": str(mid),
                "spread_absolute": str(spread_absolute),
                "spread_bps": str(spread_bps),
                "bid_depth_n": str(bid_depth_n),
                "ask_depth_n": str(ask_depth_n),
                "symbol": symbol,
                "venue": venue,
            },
            event_date=when,
            knowledge_date=when,
            confidence=1.0,
        )
    ]


def parse_tape(payload: Any, *, symbol: str, venue: str) -> list[ClaimDraft]:
    """Turn an OKX ``/api/v5/market/trades`` response into one draft per trade.

    OKX returns ``{"code", "msg", "data": [{"instId", "side", "px", "sz",
    "tradeId", "ts"}]}``. ``side`` is the *taker* aggressor side ("buy"/"sell")
    and is preserved unchanged -- a manipulation detector reads net taker flow
    from it, and an adapter reinterpreting it would invert the signal. Price and
    size stay decimal-faithful.

    A trade missing its timestamp, price or size is skipped, never emitted with
    a substituted value. The claim ``key`` is the venue's ``tradeId`` when
    present (falling back to a composite) so two trades do not collapse onto one
    claim. ``knowledge_date == event_date``: a trade is knowable the instant it
    prints.
    """
    _raise_if_error(payload, venue)
    data = payload.get("data") if isinstance(payload, dict) else None
    trades = data if isinstance(data, list) else []

    drafts: list[ClaimDraft] = []
    for trade in trades:
        when = _event_date(trade.get("ts"))
        price = _decimal(trade.get("px"))
        size = _decimal(trade.get("sz"))
        if when is None or price is None or size is None:
            continue
        side = trade.get("side")
        trade_id = trade.get("tradeId")
        key = str(trade_id) if trade_id else f"{venue}:{symbol}:{when.isoformat()}:{side}:{price}"
        drafts.append(
            ClaimDraft(
                claim_type=TRADE_TAPE,
                key=key,
                value={
                    "price": str(price),
                    "size": str(size),
                    "side": side,
                    "symbol": symbol,
                    "venue": venue,
                },
                event_date=when,
                knowledge_date=when,
                confidence=1.0,
                unit=symbol,
            )
        )
    return drafts


async def _fetch_with_session(session: Any, venue: str, path: str, params: dict[str, Any]) -> Any:
    url = BASE_URLS[venue] + path
    response = await get_json(session, url, params=params)
    if response.status_code == 429:
        raise Unavailable(f"{venue} returned HTTP 429 for {path}")
    if response.status_code != 200:
        raise Unavailable(f"{venue} returned HTTP {response.status_code} for {path}")
    return response.json()


class MicrostructureAdapter:
    source = SOURCE

    def __init__(
        self,
        *,
        venue: str,
        fetch_fn: MicrostructureFetcher | None = None,
        depth: int = 20,
    ) -> None:
        self._venue = venue
        self._fetch_fn = fetch_fn
        self._depth = depth
        self.provider_key = venue

    async def fetch(self, key: str) -> list[ClaimDraft]:
        kind, sep, symbol = key.partition(":")
        if not sep or not symbol:
            raise Unavailable(f"microstructure key must be '<kind>:<symbol>', got {key!r}")
        if kind not in ("book", "tape"):
            raise Unavailable(f"unknown microstructure kind {kind!r}")

        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            fetch_fn = self._default_fetcher(kind, symbol)

        payload = await fetch_fn(key)
        if kind == "book":
            return parse_book(payload or {}, symbol=symbol, venue=self._venue, depth=self._depth)
        return parse_tape(payload or {}, symbol=symbol, venue=self._venue)

    def _default_fetcher(self, kind: str, symbol: str) -> MicrostructureFetcher:
        venue = self._venue
        if venue not in BASE_URLS:
            raise Unavailable(f"no base URL configured for venue {venue!r}")

        if kind == "book":
            path = BOOK_PATH
            params: dict[str, Any] = {"instId": symbol, "sz": str(self._depth)}
        else:
            path = TRADES_PATH
            params = {"instId": symbol}

        async def fetch(_key: str) -> Any:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                return await _fetch_with_session(client, venue, path, params)

        return fetch
