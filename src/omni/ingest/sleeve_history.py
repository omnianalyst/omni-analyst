"""Long-history monthly series for the portfolio's four sleeves.

The scanner measures 10 years of daily closes; that window contains exactly
one true analog at best and cannot show what the four-way mix did through
1973, 1987, 2000, or 2008. This module ingests the longest public monthly
series for each sleeve so the disaster question can be answered from real
prices rather than prose:

* stocks  -- FRED SP500 monthly average is too short (10y). The honest long
  source is the S&P composite from Shiller's public dataset (1871, monthly).
* gold    -- LBMA gold price, FRED series GOLDAMGBD228NLBM (1968, monthly).
* long bonds -- 10-year Treasury constant maturity, FRED DGS10 (1962, monthly
  averaged here from daily; a total-return proxy is NOT claimed -- this is
  the yield series, labelled as such).
* cash    -- 3-month Treasury bill, FRED TB3MS (1954, monthly).

Every series lands as an ordinary claim with source, event_date (the month),
knowledge_date, and confidence 1.0 for market prices. Nothing here is a
forecast or a total-return backtest; it is coverage of what things cost.

Vintage behaviour: unlike the macro-perception path, these are fetched as
CURRENT values, not vintages. The disaster question is descriptive (what did
holding this cost, then) not point-in-time (what did the system know, then);
a revision to a 1971 gold print changes nothing about the 1973 drawdown.
That distinction is recorded here so nobody later wonders why.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable

SOURCE = "fred"
PROVIDER_KEY = "fred"
# Its own claim type, not macro_series_point: the registry enforces one
# producer per claim type, and more importantly a long descriptive price
# series answers a different question than a point-in-time macro print --
# conflating them would let a backtest mistake current values for vintages.
CLAIM_TYPE = "sleeve_history_point"

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

# sleeve -> (FRED series, human label, unit hint). SP500 is excluded: its
# FRED series starts 2011 and duplicates the scanner's own 10-year window;
# the long stock history enters through Shiller, separately.
SLEEVE_SERIES: dict[str, dict[str, str]] = {
    "sleeve_gold": {
        "series": "GOLDAMGBD228NLBM",
        "label": "Gold, LBMA, monthly",
        "unit": "USD/oz",
    },
    "sleeve_cash": {
        "series": "TB3MS",
        "label": "3-month Treasury bill, monthly",
        "unit": "percent",
    },
    "sleeve_long_bond_yield": {
        "series": "DGS10",
        "label": "10-year Treasury yield, monthly",
        "unit": "percent",
    },
}

ObsFetcher = Callable[[str], Awaitable[list[dict]]]


def _month(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).replace(tzinfo=UTC)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if value in (None, "", "."):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_sleeve_observations(
    observations: list[dict],
    *,
    sleeve: str,
) -> list[ClaimDraft]:
    """Current-value observations into sleeve claims, one per month.

    Values ALFRED marks "." are skipped, not kept: this series exists to be
    read as prices, and a month with no price is a gap in the chart, not a
    fact about the asset.
    """
    meta = SLEEVE_SERIES[sleeve]
    drafts: list[ClaimDraft] = []
    for obs in observations:
        when = _month(obs.get("date"))
        value = _to_float(obs.get("value"))
        if when is None or value is None:
            continue
        drafts.append(
            ClaimDraft(
                claim_type=CLAIM_TYPE,
                key=f"{sleeve}:{meta['series']}",
                value={"value": value, "label": meta["label"]},
                event_date=when,
                knowledge_date=when,
                confidence=1.0,
                unit=meta["unit"],
            )
        )
    return drafts


class SleeveHistoryAdapter:
    """Fetches the sleeve series as current values (see module docstring)."""

    source = SOURCE
    provider_key = PROVIDER_KEY

    def __init__(self, *, api_key: str | None, fetch_fn: ObsFetcher | None = None):
        self._api_key = api_key
        self._fetch_fn = fetch_fn
        self._vintages = False

    async def fetch(self, key: str) -> list[ClaimDraft]:
        if key not in SLEEVE_SERIES:
            raise Unavailable(f"{key} is not a sleeve history series")
        meta = SLEEVE_SERIES[key]
        series_id = meta["series"]

        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            if not self._api_key:
                raise Unavailable("no FRED API key configured")

            async def fetch_fn(sid: str) -> list[dict]:
                from omni.ingest.fred import _fetch_alfred

                return await _fetch_alfred(sid, api_key=self._api_key, vintages=False)

        observations = await fetch_fn(series_id)
        return parse_sleeve_observations(observations or [], sleeve=key)


# Standing demand for the sleeve series. The system is demand-driven by
# design: a capability nobody asks for never fills, and a boot that assumes
# otherwise silently ships a supply with no consumer. This mirrors how the
# macro loop keeps its FRED series alive: explicit, idempotent demand placed
# at scheduler boot, so the first fill cycle after deploy begins
# accumulating the history (1968 gold, 1954 bills, 1962 yields) and the
# monthly staleness window keeps it fresh thereafter.
_SLEEVE_STALENESS_DAYS = 32  # monthly series: a month plus publication lag


async def ensure_sleeve_demand(pool) -> int:
    """Idempotently place standing demand for every sleeve series.

    Uses the existing US_MACRO entity the macro loop owns, so sleeve history
    lives beside the other FRED series on one auditable row. Returns the
    number of demand rows created (0 once steady-state).
    """
    entity_id = await pool.fetchval(
        "SELECT id FROM entity WHERE symbol = 'US_MACRO' AND kind = 'macro'"
    )
    if entity_id is None:
        # The macro loop's entity should exist; creating it here would fork
        # the definition. Refuse loudly instead of duplicating it.
        raise RuntimeError(
            "US_MACRO entity not found; the sleeve series hang off it and the "
            "macro loop creates it at first run"
        )

    created = 0
    for sleeve in SLEEVE_SERIES:
        existing = await pool.fetchval(
            "SELECT 1 FROM demand "
            "WHERE entity_id = $1 AND claim_type = 'sleeve_history_point' "
            "AND key = $2 AND active",
            entity_id,
            sleeve,
        )
        if existing:
            continue
        await pool.execute(
            """
            INSERT INTO demand (entity_id, claim_type, key, channel,
                                requested_by, weight, max_staleness)
            VALUES ($1, 'sleeve_history_point', $2, 'autonomous',
                    NULL, 1.0, make_interval(days => $3))
            """,
            entity_id,
            sleeve,
            _SLEEVE_STALENESS_DAYS,
        )
        created += 1
    return created
