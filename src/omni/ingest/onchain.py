"""On-chain ingestion: Etherscan (ETH flows/supply) and DefiLlama (TVL).

Ported from v1 `app/services/blockchain/on_chain_service.py` -- the one clean
module in that area. v1 scanned blocks for whale transfers and exchange
flows with real httpx and an honest `OnChainDataUnavailable`. What it could
not do was emit a claim: it returned hydrated dicts keyed by `now()` and
priced everything through a CoinGecko ETH/USD rate that fell back to `0.0`
on failure, silently zeroing the USD columns. Neither survives the port: a
claim needs bitemporal dates, and `0.0` is a fabricated price.

Etherscan, Alchemy and DefiLlama are all `allowed` in the credential
catalog, so every claim here is redistributable and accumulates as shared
network coverage -- the only part of the crypto domain that does. Prices do
not, which is why this adapter never produces one. v1 also carried a
hardcoded `2000` USD/ETH in sibling modules; should a USD figure ever be
needed here it must come from a fetched claim, never an inlined constant.

Bitemporal rule: a confirmed block is public the moment it is mined, so for
every claim type `event_date` is the block (or daily snapshot) timestamp and
`knowledge_date` equals it. That equality is the cleanest bitemporal case in
the system and is asserted in the tests rather than left to coincidence.

Routing: the adapter declares `provider_key = "etherscan"` (the primary
source and the licence class for everything it emits -- DefiLlama is
`allowed` too) and dispatches on a `"<kind>:<identifier>"` key, because
unlike FRED/EDGAR/Polygon it serves three claim types from two sources.
DefiLlama is keyless, so the TVL route never gates on an Etherscan key; the
Etherscan routes (flow, supply) do, and raise `Unavailable` without one
rather than attempting a rate-limited keyless call or returning `[]`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable, get_json

SOURCE = "etherscan"
PROVIDER_KEY = "etherscan"

FLOW = "onchain_flow"
TVL = "onchain_tvl"
SUPPLY = "onchain_supply"

# V2, not V1. Etherscan sunset the V1 endpoint, which now answers EVERY request
# with {"status":"0","message":"NOTOK","result":"You are using a deprecated V1
# endpoint..."}. This adapter could therefore fetch nothing at all, and the
# failure surfaced as `Unavailable: Etherscan returned no latest block` -- an
# honest refusal that named the wrong cause, which is why it survived unnoticed.
#
# V2 requires an explicit `chainid`. That is also what makes the multi-chain
# path expressible through one base URL rather than a second client.
ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
ETHEREUM_CHAIN_ID = 1


def _params(api_key: str, **rest: Any) -> dict[str, Any]:
    """Every V2 call carries a chainid, built in one place.

    A route that forgets it does not fail -- it queries whichever chain the API
    defaults to, and returns real-looking data for the wrong network.
    """
    return {"chainid": ETHEREUM_CHAIN_ID, "apikey": api_key, **rest}


def _require_ok(payload: Any, *, what: str) -> dict[str, Any]:
    """Refuse an error-shaped response instead of indexing into it.

    On an error V2 returns `result` as a STRING, so the supply path handed a
    `str` to code expecting a mapping and died with `AttributeError: 'str'
    object has no attribute 'get'`. A crash is not a refusal: the fill pipeline
    records `Unavailable` with a reason and treats anything else as a defect.
    """
    if not isinstance(payload, dict):
        raise Unavailable(f"Etherscan returned a non-object for {what}")
    if str(payload.get("status")) == "0":
        detail = payload.get("result") or payload.get("message") or "no reason given"
        raise Unavailable(f"Etherscan refused {what}: {detail}")
    return payload


DEFILLAMA_PROTOCOL_URL = "https://api.llama.fi/protocol/{slug}"

# Lifted verbatim from v1 `_KNOWN_EXCHANGE_ADDRESSES`. On-chain identity is
# case-insensitive at the EVM level; keys are lowercased once here so lookups
# against a payload's mixed-case addresses are a single `.lower()`.
KNOWN_EXCHANGES: dict[str, str] = {
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance 14",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance 15",
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase 1",
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0": "Kraken 4",
    "0x1151314c646ce4e0efd76d1af4760ae66a9fe30f": "Bitfinex",
    "0xc098b2a3aa256d2140208c3de6543aaef5cd3a94": "Gemini",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
}

OnchainFetcher = Callable[[str], Awaitable[dict[str, Any]]]


def _hex_to_int(value: Any) -> int:
    # Ported verbatim from v1 `OnChainService._hex_to_int`: Etherscan's proxy
    # module returns every quantity as a hex string; a bad value parses as 0.
    try:
        if isinstance(value, str) and value.startswith("0x"):
            return int(value, 16)
        return int(value)
    except (ValueError, TypeError):
        return 0


def _from_unix(seconds: Any) -> datetime | None:
    ts = seconds
    if isinstance(ts, str):
        ts = _hex_to_int(ts)
    try:
        if not ts or ts <= 0:
            return None
        return datetime.fromtimestamp(int(ts), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_flows(
    block: dict[str, Any],
    *,
    whale_min_eth: float = 100.0,
    chain: str = "eth",
) -> list[ClaimDraft]:
    """Flatten an Etherscan `eth_getBlockByNumber` response into flow drafts.

    `event_date`/`knowledge_date` are the block's timestamp -- a confirmed
    block is public the instant it is mined. Each transaction touching a
    known exchange becomes an `exchange_inflow`/`exchange_outflow` claim
    regardless of size; any other transfer at or above `whale_min_eth`
    becomes a `whale` claim. The claim `key` is the transaction hash, so
    refetching the same block dedupes rather than duplicating, and two flows
    into the same exchange in one block do not collapse into one another.
    """
    result = block.get("result") or {}
    mined_at = _from_unix(result.get("timestamp"))
    if mined_at is None:
        # No block timestamp means no event_date, and without it the
        # bitemporal guarantee cannot be made. Skip the whole block rather
        # than substitute `now()`.
        return []

    block_number = result.get("number")
    drafts: list[ClaimDraft] = []
    for tx in result.get("transactions") or []:
        value_eth = _hex_to_int(tx.get("value", "0x0")) / 1e18
        to_addr = (tx.get("to") or "").lower()
        from_addr = (tx.get("from") or "").lower()
        to_exchange = KNOWN_EXCHANGES.get(to_addr)
        from_exchange = KNOWN_EXCHANGES.get(from_addr)

        if to_exchange:
            exchange, direction = to_exchange, "inflow"
        elif from_exchange:
            exchange, direction = from_exchange, "outflow"
        elif value_eth >= whale_min_eth:
            exchange, direction = None, "whale"
        else:
            continue

        drafts.append(
            ClaimDraft(
                claim_type=FLOW,
                key=tx.get("hash") or "",
                value={
                    "kind": f"exchange_{direction}" if exchange else "whale",
                    "exchange": exchange,
                    "direction": direction,
                    "amount_eth": value_eth,
                    "from": from_addr,
                    "to": to_addr,
                    "chain": chain,
                },
                event_date=mined_at,
                knowledge_date=mined_at,
                confidence=1.0,
                unit="ETH",
                evidence={"block": block_number},
            )
        )
    return drafts


def parse_tvl(payload: dict[str, Any], *, slug: str) -> list[ClaimDraft]:
    """Flatten a DefiLlama `/protocol/{slug}` response into TVL drafts.

    DefiLlama publishes daily protocol snapshots under `tvl`, each a unix
    `date` plus `totalLiquidityUSD`. The snapshot date is both `event_date`
    (the on-chain state it aggregates) and `knowledge_date` (public daily
    data). A point missing its date or value is skipped rather than guessed.

    The field names here were originally `tvlHistory[].tvl`, taken from memory
    rather than from the API, and the test fixture repeated the same guess --
    so code and test agreed with each other and neither matched DefiLlama.
    The fixture below is now copied from a real response.
    """
    drafts: list[ClaimDraft] = []
    name = payload.get("name")
    for point in payload.get("tvl") or []:
        when = _from_unix(point.get("date"))
        tvl = point.get("totalLiquidityUSD")
        if when is None or tvl is None:
            continue
        try:
            tvl_value = float(tvl)
        except (TypeError, ValueError):
            continue
        drafts.append(
            ClaimDraft(
                claim_type=TVL,
                key=slug,
                value={"tvl": tvl_value},
                event_date=when,
                knowledge_date=when,
                confidence=1.0,
                unit="USD",
                evidence={"protocol": name} if name else None,
            )
        )
    return drafts


def parse_supply(
    payload: dict[str, Any], *, token: str, decimals: int = 18
) -> list[ClaimDraft]:
    """Turn a measured supply into a single supply draft.

    Supply is read at a block; the timestamp of that block is the claim's
    `event_date`/`knowledge_date`. The payload therefore carries both the
    block timestamp and the raw integer supply (smallest unit, as Etherscan
    returns it). `decimals` is the token's own decimal precision -- a
    structural property of the contract, not a price -- used only to render a
    human-readable `supply` alongside the faithful `supply_raw`. No price is
    introduced: a USD market cap would require one, and inventing it is the
    failure mode this layer exists to refuse.
    """
    when = _from_unix(payload.get("block_timestamp"))
    raw = payload.get("supply")
    if when is None or raw is None:
        return []
    try:
        supply_raw = int(raw)
    except (TypeError, ValueError):
        return []
    if supply_raw < 0:
        return []
    return [
        ClaimDraft(
            claim_type=SUPPLY,
            key=token,
            value={
                "supply": supply_raw / (10**decimals),
                "supply_raw": supply_raw,
                "decimals": decimals,
            },
            event_date=when,
            knowledge_date=when,
            confidence=1.0,
            unit=token,
        )
    ]


async def _fetch_latest_block(api_key: str) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        num_resp = await get_json(client,
            ETHERSCAN_URL,
            params=_params(api_key, module="proxy", action="eth_blockNumber"),
        )
        if num_resp.status_code != 200:
            raise Unavailable(
                f"Etherscan blockNumber returned HTTP {num_resp.status_code}"
            )
        latest = _hex_to_int(
            _require_ok(num_resp.json(), what="eth_blockNumber").get("result", "0x0")
        )
        if latest <= 0:
            raise Unavailable("Etherscan returned no latest block")
        blk_resp = await get_json(client,
            ETHERSCAN_URL,
            params=_params(api_key, module="proxy", action="eth_getBlockByNumber",
                            tag=hex(latest), boolean="true"),
        )
        if blk_resp.status_code != 200:
            raise Unavailable(
                f"Etherscan getBlockByNumber returned HTTP {blk_resp.status_code}"
            )
        return blk_resp.json()


async def _fetch_tvl(slug: str) -> dict[str, Any]:
    import httpx

    url = DEFILLAMA_PROTOCOL_URL.format(slug=slug)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await get_json(client,url)
        if resp.status_code != 200:
            raise Unavailable(
                f"DefiLlama returned HTTP {resp.status_code} for {slug}"
            )
        return resp.json()


async def _fetch_supply(api_key: str, token: str, decimals: int) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        num_resp = await get_json(client,
            ETHERSCAN_URL,
            params=_params(api_key, module="proxy", action="eth_blockNumber"),
        )
        if num_resp.status_code != 200:
            raise Unavailable(
                f"Etherscan blockNumber returned HTTP {num_resp.status_code}"
            )
        tag = _require_ok(num_resp.json(), what="eth_blockNumber").get("result", "0x0")
        blk_resp = await get_json(client,
            ETHERSCAN_URL,
            params=_params(api_key, module="proxy", action="eth_getBlockByNumber",
                            tag=tag, boolean="false"),
        )
        if blk_resp.status_code != 200:
            raise Unavailable(
                f"Etherscan getBlockByNumber returned HTTP {blk_resp.status_code}"
            )
        # `result` is a mapping on success and a STRING on error, so this
        # cannot index blindly. `_require_ok` catches the error-shaped response;
        # the isinstance guard catches a success-shaped one whose result is not
        # the block object this line assumes -- a crash here is a defect, while
        # `Unavailable` is a recorded refusal the fill pipeline understands.
        block = _require_ok(blk_resp.json(), what="eth_getBlockByNumber").get("result")
        if not isinstance(block, dict):
            raise Unavailable(
                f"Etherscan returned no block object for {token}; got {type(block).__name__}"
            )
        block_timestamp = block.get("timestamp")

        if token.upper() == "ETH":
            supply_resp = await get_json(client,
                ETHERSCAN_URL,
                params=_params(api_key, module="stats", action="ethsupply"),
            )
            supply = _require_ok(supply_resp.json(), what=f"supply for {token}").get("result")
        else:
            supply_resp = await get_json(client,
                ETHERSCAN_URL,
                params=_params(api_key, module="stats", action="tokensupply",
                                contractaddress=token),
            )
            supply = (supply_resp.json() or {}).get("result")
        if supply is None:
            raise Unavailable(f"Etherscan returned no supply for {token}")
        return {"block_timestamp": block_timestamp, "supply": supply, "decimals": decimals}


class OnChainAdapter:
    source = SOURCE
    provider_key = PROVIDER_KEY

    def __init__(
        self,
        *,
        api_key: str | None = None,
        fetch_fn: OnchainFetcher | None = None,
        whale_min_eth: float = 100.0,
        decimals: int = 18,
    ) -> None:
        self._api_key = api_key
        self._fetch_fn = fetch_fn
        self._whale_min_eth = whale_min_eth
        self._decimals = decimals

    async def fetch(self, key: str) -> list[ClaimDraft]:
        kind, sep, ident = key.partition(":")
        if not sep or not ident:
            raise Unavailable(
                f"onchain key must be '<kind>:<identifier>', got {key!r}"
            )
        if kind not in ("flow", "tvl", "supply"):
            raise Unavailable(f"unknown onchain kind {kind!r}")

        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            fetch_fn = self._default_fetcher(kind, ident)

        payload = await fetch_fn(key)
        if kind == "flow":
            return parse_flows(
                payload or {}, whale_min_eth=self._whale_min_eth, chain=ident
            )
        if kind == "tvl":
            return parse_tvl(payload or {}, slug=ident)
        return parse_supply(payload or {}, token=ident, decimals=self._decimals)

    def _default_fetcher(
        self, kind: str, ident: str
    ) -> OnchainFetcher:
        if kind == "tvl":
            # DefiLlama is keyless: TVL must still work with no Etherscan key.
            async def fetch(key: str) -> dict[str, Any]:
                return await _fetch_tvl(ident)

            return fetch

        if kind in ("flow", "supply"):
            if not self._api_key:
                raise Unavailable("no Etherscan API key configured")
            if kind == "flow":
                async def fetch(key: str) -> dict[str, Any]:
                    return await _fetch_latest_block(self._api_key or "")

                return fetch
            if kind == "supply":
                async def fetch(key: str) -> dict[str, Any]:
                    return await _fetch_supply(
                        self._api_key or "", ident, self._decimals
                    )

                return fetch

        raise Unavailable(f"unknown onchain kind {kind!r}")
