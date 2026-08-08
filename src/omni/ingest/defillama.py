"""DefiLlama ingestion: protocol fees, revenue, stablecoin supply, chain TVL.

The sibling of `onchain.py`'s TVL route, extended to the rest of DefiLlama's
permissively-redistributable datasets. DefiLlama is `allowed` in the credential
catalog and keyless, so every claim here is shared network coverage -- the
crypto counterpart to EDGAR fundamentals, and unlike every price feed the
system can serve these to every user.

Field names below were copied from real responses, not memory. `parse_tvl` in
onchain.py records that its field names were once guessed and the fixture
repeated the guess; this adapter was written against captured payloads (see the
endpoint URLs on each constant and the fixtures in tests/test_defillama.py).

Fees and revenue are different numbers and are never conflated here. DefiLlama
serves them as the same `totalDataChart` shape but from separate responses
selected by the `dataType` query parameter (`dailyFees` vs `dailyRevenue`); a
P/F ratio computed from revenue would be wrong by whatever the protocol pays
out to LPs. Each gets its own claim type from its own response, and the value
dict key (`fees` vs `revenue`) so a swap fails loudly.

Bitemporal rule: DefiLlama publishes daily snapshots, so the snapshot's own
date is both `event_date` and `knowledge_date` -- exactly as `parse_tvl` does.
Never `now()`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable, get_json

SOURCE = "defillama"
PROVIDER_KEY = "defillama"

FEES = "protocol_fees"
REVENUE = "protocol_revenue"
STABLECOIN_SUPPLY = "stablecoin_supply"
CHAIN_TVL = "chain_tvl"

DEFILLAMA_API = "https://api.llama.fi"
SUMMARY_FEES_URL = DEFILLAMA_API + "/summary/fees/{slug}"
CHAIN_TVL_URL = DEFILLAMA_API + "/v2/historicalChainTvl/{chain}"
# The stablecoin endpoints moved off api.llama.fi (which 404s for them) to the
# stablecoins.llama.fi host; the per-coin historical series lives in `tokens`.
STABLECOIN_URL = "https://stablecoins.llama.fi/stablecoin/{asset_id}"

DefiLlamaFetcher = Callable[[str], Awaitable[Any]]


def _from_unix(seconds: Any) -> datetime | None:
    try:
        ts = int(seconds)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=UTC)


def _chart_drafts(
    payload: dict[str, Any],
    *,
    ident: str,
    claim_type: str,
    field: str,
) -> list[ClaimDraft]:
    drafts: list[ClaimDraft] = []
    name = payload.get("name")
    for point in payload.get("totalDataChart") or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        when = _from_unix(point[0])
        raw = point[1]
        if when is None or raw is None:
            continue
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            continue
        drafts.append(
            ClaimDraft(
                claim_type=claim_type,
                key=ident,
                value={field: amount},
                event_date=when,
                knowledge_date=when,
                confidence=1.0,
                unit="USD",
                evidence={"protocol": name} if name else None,
            )
        )
    return drafts


def parse_fees(payload: dict[str, Any], *, slug: str) -> list[ClaimDraft]:
    return _chart_drafts(payload, ident=slug, claim_type=FEES, field="fees")


def parse_revenue(payload: dict[str, Any], *, slug: str) -> list[ClaimDraft]:
    return _chart_drafts(payload, ident=slug, claim_type=REVENUE, field="revenue")


def parse_stablecoin(payload: dict[str, Any], *, asset_id: str) -> list[ClaimDraft]:
    peg_type = payload.get("pegType")
    name = payload.get("name")
    symbol = payload.get("symbol")
    drafts: list[ClaimDraft] = []
    for point in payload.get("tokens") or []:
        when = _from_unix(point.get("date"))
        circulating = point.get("circulating")
        if when is None or not isinstance(circulating, dict) or not peg_type:
            continue
        raw = circulating.get(peg_type)
        if raw is None:
            continue
        try:
            supply = float(raw)
        except (TypeError, ValueError):
            continue
        drafts.append(
            ClaimDraft(
                claim_type=STABLECOIN_SUPPLY,
                key=asset_id,
                value={"supply": supply},
                event_date=when,
                knowledge_date=when,
                confidence=1.0,
                unit="USD",
                evidence={"name": name, "symbol": symbol, "peg_type": peg_type},
            )
        )
    return drafts


def parse_chain_tvl(payload: Any, *, chain: str) -> list[ClaimDraft]:
    drafts: list[ClaimDraft] = []
    for point in payload or []:
        if not isinstance(point, dict):
            continue
        when = _from_unix(point.get("date"))
        tvl = point.get("tvl")
        if when is None or tvl is None:
            continue
        try:
            tvl_value = float(tvl)
        except (TypeError, ValueError):
            continue
        drafts.append(
            ClaimDraft(
                claim_type=CHAIN_TVL,
                key=chain,
                value={"tvl": tvl_value},
                event_date=when,
                knowledge_date=when,
                confidence=1.0,
                unit="USD",
            )
        )
    return drafts


async def _fetch_dimension(slug: str, data_type: str) -> dict[str, Any]:
    import httpx

    url = SUMMARY_FEES_URL.format(slug=slug)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await get_json(client, url, params={"dataType": data_type})
        if resp.status_code != 200:
            raise Unavailable(
                f"DefiLlama returned HTTP {resp.status_code} for fees/{slug} ({data_type})"
            )
        return resp.json()


async def _fetch_stablecoin(asset_id: str) -> dict[str, Any]:
    import httpx

    url = STABLECOIN_URL.format(asset_id=asset_id)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await get_json(client, url)
        if resp.status_code != 200:
            raise Unavailable(
                f"DefiLlama returned HTTP {resp.status_code} for stablecoin/{asset_id}"
            )
        return resp.json()


async def _fetch_chain_tvl(chain: str) -> list[dict[str, Any]]:
    import httpx

    url = CHAIN_TVL_URL.format(chain=chain)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await get_json(client, url)
        if resp.status_code != 200:
            raise Unavailable(f"DefiLlama returned HTTP {resp.status_code} for chain/{chain}")
        return resp.json()


class DefiLlamaAdapter:
    source = SOURCE
    provider_key = PROVIDER_KEY

    def __init__(self, *, fetch_fn: DefiLlamaFetcher | None = None) -> None:
        self._fetch_fn = fetch_fn

    async def fetch(self, key: str) -> list[ClaimDraft]:
        kind, sep, ident = key.partition(":")
        if not sep or not ident:
            raise Unavailable(f"defillama key must be '<kind>:<identifier>', got {key!r}")
        if kind not in ("fees", "revenue", "stablecoin", "chain"):
            raise Unavailable(f"unknown defillama kind {kind!r}")

        if self._fetch_fn is not None:
            payload = await self._fetch_fn(key)
        elif kind == "fees":
            payload = await _fetch_dimension(ident, "dailyFees")
        elif kind == "revenue":
            payload = await _fetch_dimension(ident, "dailyRevenue")
        elif kind == "stablecoin":
            payload = await _fetch_stablecoin(ident)
        else:
            payload = await _fetch_chain_tvl(ident)

        if kind == "fees":
            return parse_fees(payload or {}, slug=ident)
        if kind == "revenue":
            return parse_revenue(payload or {}, slug=ident)
        if kind == "stablecoin":
            return parse_stablecoin(payload or {}, asset_id=ident)
        return parse_chain_tvl(payload or [], chain=ident)
