"""CoinGecko market-chart ingestion.

CoinGecko is crypto's Polygon: a `byo_only` price feed. Its terms forbid
serving the data on to third parties, so claims produced here are pinned to the
user whose key fetched them and never enter shared coverage. **The adapter does
not enforce that rule** — the writer does, from `provider_key`. This adapter
declares `provider_key = "coingecko"` and produces drafts; `ClaimDraft` has no
audience or licence field, so there is nowhere for an adapter to make that
decision even if it tried.

Endpoint knowledge was harvested from v1
`app/data/.../providers/coingecko.py` but that file is not ported: it inherits
a base class whose fetch path raises `AttributeError` on `self.timeout` and
keeps a Redis cache that silently never works, and its network/refresh path
does not fit this bitemporal protocol. Only the URL shape, the
`/coins/{id}/market_chart` response layout, and the symbol-to-id map carry
over.

Unlike Polygon, CoinGecko's free tier is **keyless** (rate-limited to ~10
calls/minute; a key raises the limit). A missing key is therefore not an
`Unavailable` here — but a throttle response (HTTP 429, or a 200 body of
`{"status": {"error_code": 429}}`) is.

`/coins/{id}/market_chart` returns three parallel arrays — `prices`,
`market_caps`, `total_volumes` — each a list of `[ms_timestamp, value]` pairs.
They are joined on timestamp: lengths and ordering are not assumed to match
(see `parse_market_chart`).

`knowledge_date` equals `event_date` for a crypto tick. Crypto trades
continuously with no session close, so unlike Polygon's daily bars there is no
settlement lag to account for — the tick was knowable the moment it printed.
Deriving it from `now()` would let a backtest peek at a price that had not
happened yet; deriving it from a fictional close would push knowledge into the
future for no reason. The tick's own timestamp is the honest bound in both
directions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable, get_json

SOURCE = "coingecko"
PROVIDER_KEY = "coingecko"
CLAIM_TYPE = "price_snapshot"

MARKET_CHART_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"

ChartFetcher = Callable[[str], Awaitable[dict[str, Any]]]

# Harvested verbatim from v1 `providers/coingecko.py` `direct_mappings`. A
# missing symbol raises `Unavailable` rather than guessing by lowercasing the
# ticker: CoinGecko ids and ticker symbols collide across unrelated assets, so
# `symbol.lower()` confidently returns prices for a different coin. 100 entries.
SYMBOL_TO_ID: dict[str, str] = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "usdt": "tether",
    "bnb": "binancecoin",
    "usdc": "usd-coin",
    "xrp": "ripple",
    "ada": "cardano",
    "doge": "dogecoin",
    "sol": "solana",
    "dot": "polkadot",
    "matic": "matic-network",
    "pol": "matic-network",
    "dai": "dai",
    "trx": "tron",
    "avax": "avalanche-2",
    "shib": "shiba-inu",
    "link": "chainlink",
    "atom": "cosmos",
    "ltc": "litecoin",
    "uni": "uniswap",
    "xlm": "stellar",
    "etc": "ethereum-classic",
    "xmr": "monero",
    "bch": "bitcoin-cash",
    "algo": "algorand",
    "fil": "filecoin",
    "ftm": "fantom",
    "near": "near",
    "apt": "aptos",
    "arb": "arbitrum",
    "op": "optimism",
    "ape": "apecoin",
    "sand": "the-sandbox",
    "mana": "decentraland",
    "gala": "gala",
    "axs": "axie-infinity",
    "mkr": "maker",
    "aave": "aave",
    "snx": "havven",
    "crv": "curve-dao-token",
    "rdt": "reddcoin",
    "grt": "the-graph",
    "1inch": "1inch",
    "sushi": "sushi",
    "comp": "compound-governance-token",
    "wbtc": "wrapped-bitcoin",
    "leo": "leo-token",
    "ht": "huobi-token",
    "okb": "okb",
    "cusdc": "compound-usd-coin",
    "bat": "basic-attention-token",
    "zet": "zeta",
    "icp": "internet-computer",
    "rndr": "render-token",
    "imx": "immutable-x",
    "inj": "injection",
    "stx": "blockstack",
    "kava": "kava",
    "runes": "rune",
    "mina": "mina-protocol",
    "flow": "flow",
    "egld": "elrond-erd-2",
    "xtz": "tezos",
    "hbar": "hedera-hashgraph",
    "chz": "chiliz",
    "ens": "ethereum-name-service",
    "gmt": "stepn",
    "theta": "theta-token",
    "ksm": "kusama",
    "dash": "dash",
    "zil": "zilliqa",
    "neo": "neo",
    "waves": "waves",
    "qtum": "qtum",
    "iota": "iota",
    "vet": "vechain",
    "icx": "icon",
    "ong": "ontology",
    "ont": "ontology",
    "zec": "zcash",
    "sc": "siacoin",
    "nem": "nem",
    "bts": "bitshares",
    "steem": "steem",
    "golos": "golos",
    "lsk": "lisk",
    "strat": "stratis",
    "ark": "ark",
    "lrc": "loopring",
    "bal": "balancer",
    "weth": "weth",
    "pepe": "pepe",
    "wif": "dogwifhat",
    "bonk": "bonk",
    "floki": "floki",
    "jup": "jupiter-exchange-solana",
    "pyth": "pyth-network",
    "jto": "jito-governance-token",
    "tiao": "tiao",
    "bome": "book-of-meme",
}


def _event_date(ts_ms: Any) -> datetime | None:
    if ts_ms is None:
        return None
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def parse_market_chart(
    payload: dict[str, Any],
    *,
    asset_id: str,
) -> list[ClaimDraft]:
    """Flatten a CoinGecko `/coins/{id}/market_chart` response into drafts.

    The response carries three parallel arrays — `prices`, `market_caps`,
    `total_volumes` — each of `[ms_timestamp, value]` pairs. They are joined on
    timestamp, not on index: CoinGecko does not guarantee the three arrays share
    length or ordering, so positional join (`volumes[i]`) would pair a price
    with the wrong day's volume. Indexing by timestamp means a missing entry is
    honestly absent (`None`) rather than silently paired with a neighbour's
    value.

    Throttling arrives as `{"status": {"error_code": 429, ...}}` over HTTP 200;
    that is the source refusing to answer, so it raises `Unavailable`. A valid
    empty `prices` array is an honest "nothing in this window", so it returns
    `[]`. Treating them the same would either swallow a rate-limit or crash on
    every quiet range.
    """
    status = payload.get("status")
    if isinstance(status, dict) and status.get("error_code") == 429:
        raise Unavailable(
            f"CoinGecko returned error {status.get('error_code')} for "
            f"{asset_id}: {status.get('error_message', 'throttled')}"
        )

    prices = payload.get("prices") or []
    if not prices:
        return []

    market_caps = {
        ts: v for ts, v in (payload.get("market_caps") or [])
        if ts is not None
    }
    total_volumes = {
        ts: v for ts, v in (payload.get("total_volumes") or [])
        if ts is not None
    }

    drafts: list[ClaimDraft] = []
    for point in prices:
        try:
            ts_ms, price = point
        except (TypeError, ValueError):
            continue
        event_date = _event_date(ts_ms)
        if event_date is None:
            continue
        drafts.append(
            ClaimDraft(
                claim_type=CLAIM_TYPE,
                key=asset_id,
                value={
                    "price": price,
                    "market_cap": market_caps.get(ts_ms),
                    "volume": total_volumes.get(ts_ms),
                },
                event_date=event_date,
                knowledge_date=event_date,
                confidence=1.0,
            )
        )
    return drafts


async def _fetch_market_chart(
    coin_id: str,
    *,
    api_key: str | None,
    vs_currency: str,
    days: Any,
) -> dict[str, Any]:
    import httpx

    url = MARKET_CHART_URL.format(coin_id=coin_id)
    params = {"vs_currency": vs_currency, "days": days}
    headers = {"x-cg-demo-api-key": api_key} if api_key else None
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await get_json(client, url, params=params, headers=headers)
        if response.status_code == 429:
            raise Unavailable(
                f"CoinGecko returned HTTP 429 for {coin_id}"
            )
        if response.status_code != 200:
            raise Unavailable(
                f"CoinGecko returned HTTP {response.status_code} for {coin_id}"
            )
        return response.json()


class CoinGeckoAdapter:
    source = SOURCE
    provider_key = PROVIDER_KEY

    def __init__(
        self,
        *,
        api_key: str | None = None,
        fetch_fn: ChartFetcher | None = None,
        vs_currency: str = "usd",
        days: Any = 30,
    ) -> None:
        self._api_key = api_key
        self._fetch_fn = fetch_fn
        self._vs_currency = vs_currency
        self._days = days

    async def fetch(self, key: str) -> list[ClaimDraft]:
        coin_id = SYMBOL_TO_ID.get(key.lower())
        if coin_id is None:
            raise Unavailable(
                f"no CoinGecko id mapped for symbol {key!r}; "
                f"refusing to guess"
            )

        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            async def fetch_fn(cid: str) -> dict[str, Any]:
                return await _fetch_market_chart(
                    cid,
                    api_key=self._api_key,
                    vs_currency=self._vs_currency,
                    days=self._days,
                )

        payload = await fetch_fn(coin_id)
        return parse_market_chart(payload or {}, asset_id=key)
