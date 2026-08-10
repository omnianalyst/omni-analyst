"""Freeze token-launch cohorts forward, so a base rate can be measured.

Every claim anyone makes about trading new launches rests on one number: **what
fraction go to zero.** That number is not obtainable after the fact. Dead pools
leave every aggregator's index, so a survey taken later surveys survivors, and
the base rate it reports is wrong in the direction that makes the strategy look
profitable. Finding 42 caught the same shape in Bybit's open interest -- an
endpoint answering `retCode: 0` while silently omitting everything delisted.

So this records the cohort **before outcomes exist** and filters nothing. A pool
launching with $0 liquidity and four dollars of volume is not noise to be
skipped; it is the denominator. Screening at collection time would remove
exactly the population the measurement is about, and would do it invisibly.

Two operations, deliberately separate:

- `discover` reads the venue's new-pool feed and writes whatever it finds.
- `reobserve` re-reads pools already known and writes one row each, marking
  `present=False` for any the venue no longer serves. A death is a measurement
  and gets a row; a sweep that never ran leaves no rows at all, and
  `launch_sweep` is what keeps those two readable apart.

**Nothing here scores, ranks or selects.** A filter is the next phase and it has
to be measured against this population rather than baked into it -- a collector
that only keeps what its own filter likes can never falsify that filter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

logger = logging.getLogger("omni.research.launches")

API = "https://api.geckoterminal.com/api/v2"

# GeckoTerminal serves 20 pools a page on the new-pool feed and accepts up to 30
# addresses on the multi-pool read. Both are the venue's numbers, not ours.
PAGE_SIZE = 20
MULTI_BATCH = 30

# The free tier throttles around 30 calls a minute. Deliberately conservative:
# a collector that gets itself rate-limited returns short pages, and a short page
# is indistinguishable from a quiet day.
CALL_SPACING_SECONDS = 2.5

_SWEEP = """
INSERT INTO launch_sweep (network, kind, swept_at, pools_seen)
VALUES ($1, $2, $3, $4) RETURNING id
"""

_OBSERVE = """
INSERT INTO launch_observation (
    network, pool_address, observed_at, sweep_id, present,
    pool_created_at, name, base_token, quote_token,
    price_usd, liquidity_usd, volume_24h_usd, fdv_usd, market_cap_usd,
    buys_24h, sells_24h, buyers_24h, sellers_24h
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
ON CONFLICT (network, pool_address, observed_at) DO NOTHING
"""

_KNOWN = """
SELECT DISTINCT ON (pool_address) pool_address
FROM launch_observation
WHERE network = $1
  AND present
GROUP BY pool_address
HAVING max(observed_at) >= $2
"""


class FeedUnavailable(Exception):
    """The venue could not be read, so no cohort is recorded.

    Raised rather than returning an empty list. An empty cohort written to the
    store is indistinguishable from a day on which nothing launched, and the
    difference between those is the whole value of the table.
    """


@dataclass(frozen=True)
class Observation:
    """One pool as the venue described it at one instant."""

    pool_address: str
    present: bool
    pool_created_at: datetime | None = None
    name: str | None = None
    base_token: str | None = None
    quote_token: str | None = None
    price_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    fdv_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    buys_24h: int | None = None
    sells_24h: int | None = None
    buyers_24h: int | None = None
    sellers_24h: int | None = None


def _decimal(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() and value >= 0 else None


def _int(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _moment(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _token(entry: dict, side: str) -> str | None:
    rel = (entry.get("relationships") or {}).get(side) or {}
    data = rel.get("data") or {}
    ident = data.get("id")
    return str(ident) if ident else None


def parse_pool(entry: dict) -> Observation | None:
    """One feed entry into an observation, or None if it has no address.

    Every measured field is optional. The venue omits fields on brand-new pools
    all the time, and an entry that is mostly nulls is a real observation of a
    pool that has barely traded -- exactly the population being counted. Only
    the address is load-bearing, because without it the row cannot be followed.
    """
    attrs = entry.get("attributes") or {}
    address = attrs.get("address") or (entry.get("id") or "").split("_", 1)[-1]
    if not address:
        return None
    volume = attrs.get("volume_usd") or {}
    trades = (attrs.get("transactions") or {}).get("h24") or {}
    return Observation(
        pool_address=str(address),
        present=True,
        pool_created_at=_moment(attrs.get("pool_created_at")),
        name=attrs.get("name"),
        base_token=_token(entry, "base_token"),
        quote_token=_token(entry, "quote_token"),
        price_usd=_decimal(attrs.get("base_token_price_usd")),
        liquidity_usd=_decimal(attrs.get("reserve_in_usd")),
        volume_24h_usd=_decimal(volume.get("h24")),
        fdv_usd=_decimal(attrs.get("fdv_usd")),
        market_cap_usd=_decimal(attrs.get("market_cap_usd")),
        buys_24h=_int(trades.get("buys")),
        sells_24h=_int(trades.get("sells")),
        buyers_24h=_int(trades.get("buyers")),
        sellers_24h=_int(trades.get("sellers")),
    )


async def _get(url: str, *, fetch: Any = None) -> dict:
    if fetch is not None:
        return await fetch(url)

    def _blocking() -> dict:
        request = urllib.request.Request(url, headers={"User-Agent": "omni-research"})
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)

    try:
        return await asyncio.to_thread(_blocking)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FeedUnavailable(f"{url}: {exc}") from exc


async def discover(network: str, *, pages: int = 1, fetch: Any = None) -> list[Observation]:
    """Every pool the venue currently calls new, unfiltered.

    A page shorter than `PAGE_SIZE` before the last requested page raises. The
    venue serves full pages when more exist, so a short page mid-walk means the
    read was throttled or truncated -- and a truncated cohort silently
    understates how many launches happened, which is the one number this is for.
    """
    out: list[Observation] = []
    for page in range(1, pages + 1):
        payload = await _get(f"{API}/networks/{network}/new_pools?page={page}", fetch=fetch)
        entries = payload.get("data") or []
        parsed = [p for p in (parse_pool(e) for e in entries) if p is not None]
        out.extend(parsed)
        if len(entries) < PAGE_SIZE and page < pages:
            raise FeedUnavailable(
                f"{network} new_pools page {page} returned {len(entries)} of "
                f"{PAGE_SIZE} with {pages - page} pages still requested; a short "
                f"page mid-walk is a truncated read, not a quiet market"
            )
        if page < pages:
            await asyncio.sleep(CALL_SPACING_SECONDS)
    return out


async def reobserve(
    network: str, addresses: Sequence[str], *, fetch: Any = None
) -> list[Observation]:
    """Re-read known pools. Anything the venue no longer serves comes back absent.

    The absence is the point. A pool that has stopped being served has almost
    certainly died, and that is the outcome the cohort was frozen to measure --
    so it is recorded as `present=False` rather than skipped.
    """
    out: list[Observation] = []
    for i in range(0, len(addresses), MULTI_BATCH):
        batch = list(addresses[i : i + MULTI_BATCH])
        joined = ",".join(batch)
        payload = await _get(
            f"{API}/networks/{network}/pools/multi/{joined}", fetch=fetch
        )
        seen: dict[str, Observation] = {}
        for entry in payload.get("data") or []:
            parsed = parse_pool(entry)
            if parsed is not None:
                seen[parsed.pool_address.lower()] = parsed
        for address in batch:
            found = seen.get(address.lower())
            out.append(found or Observation(pool_address=address, present=False))
        if i + MULTI_BATCH < len(addresses):
            await asyncio.sleep(CALL_SPACING_SECONDS)
    return out


async def record(
    pool,
    *,
    network: str,
    kind: str,
    observations: Sequence[Observation],
    observed_at: datetime,
) -> UUID:
    """Write one sweep and its observations in a single transaction.

    All or nothing: a half-written sweep would leave part of a cohort recorded
    and the rest looking like it never launched.
    """
    if observed_at.tzinfo is None:
        raise ValueError(
            f"observed_at is naive ({observed_at}); a cohort is defined by when it "
            f"was frozen and a naive instant is whatever the host's timezone is"
        )
    if kind not in ("discover", "reobserve"):
        raise ValueError(f"kind must be discover or reobserve, got {kind!r}")

    async with pool.acquire() as conn, conn.transaction():
        sweep_id = await conn.fetchval(
            _SWEEP, network, kind, observed_at, len(observations)
        )
        for o in observations:
            await conn.execute(
                _OBSERVE, network, o.pool_address, observed_at, sweep_id, o.present,
                o.pool_created_at, o.name, o.base_token, o.quote_token,
                o.price_usd, o.liquidity_usd, o.volume_24h_usd, o.fdv_usd,
                o.market_cap_usd, o.buys_24h, o.sells_24h, o.buyers_24h, o.sellers_24h,
            )
    return sweep_id


async def known_pools(pool, network: str, *, since: datetime) -> list[str]:
    """Pools last seen alive at or after `since`, which is the follow-up set."""
    rows = await pool.fetch(_KNOWN, network, since)
    return [r["pool_address"] for r in rows]
