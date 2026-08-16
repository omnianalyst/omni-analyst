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

# FRED discontinued its LBMA gold series (HTTP 400, series does not exist,
# measured 2026-08-16). The longest keyless public replacement is the World
# Bank Pink Sheet: monthly nominal gold in USD/oz from 1960M01, canonical and
# freely redistributable. Served as XLSX, so gold rows arrive through a
# different fetcher than the FRED pair and carry source="worldbank" per-draft
# -- the adapter's own label stays "fred" for the two FRED series.
PINK_SHEET_URL = (
    "https://thedocs.worldbank.org/en/doc/"
    "5d903e848db1d1b83e0ec8f744e55570-0350012021/related/"
    "CMO-Historical-Data-Monthly.xlsx"
)
PINK_GOLD_COLUMN = "Gold"
PINK_SOURCE = "worldbank"

BytesFetcher = Callable[[], Awaitable[bytes]]


def parse_pink_sheet_gold(data: bytes) -> list[ClaimDraft]:
    """The Pink Sheet's Monthly Prices sheet -> one draft per month.

    Layout (measured 2026-08-16): row 5 holds commodity labels, row 6 units,
    data from row 7, first column `YYYYMmm`. The `…` marker is the sheet's
    "not available" and is skipped like ALFRED's `.`.
    """
    import io

    import openpyxl

    book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = book["Monthly Prices"]
    rows = list(sheet.iter_rows(values_only=True))
    book.close()

    header = rows[4]
    gold_col = next(
        (i for i, cell in enumerate(header)
         if isinstance(cell, str) and cell.strip().lower() == PINK_GOLD_COLUMN.lower()),
        None,
    )
    if gold_col is None:
        raise ValueError("Pink Sheet has no Gold column; layout changed")

    drafts: list[ClaimDraft] = []
    for row in rows[6:]:
        period = row[0]
        raw = row[gold_col] if gold_col < len(row) else None
        if not isinstance(period, str) or len(period) != 7 or "M" not in period:
            continue
        try:
            year, month = int(period[:4]), int(period[5:7])
            value = float(raw)
        except (TypeError, ValueError):
            continue
        when = datetime(year, month, 1, tzinfo=UTC)
        drafts.append(
            ClaimDraft(
                claim_type=CLAIM_TYPE,
                key="sleeve_gold:WORLD_BANK_PINK",
                value={"value": value, "label": "Gold, World Bank Pink Sheet, monthly"},
                event_date=when,
                knowledge_date=when,
                confidence=1.0,
                unit="USD/oz",
                source=PINK_SOURCE,
            )
        )
    return drafts


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

    def __init__(
        self,
        *,
        api_key: str | None,
        fetch_fn: ObsFetcher | None = None,
        pink_fn: BytesFetcher | None = None,
    ):
        self._api_key = api_key
        self._fetch_fn = fetch_fn
        self._pink_fn = pink_fn
        self._vintages = False

    async def fetch(self, key: str) -> list[ClaimDraft]:
        if key not in SLEEVE_SERIES:
            raise Unavailable(f"{key} is not a sleeve history series")

        if key == "sleeve_gold":
            data = await self._fetch_pink()
            return parse_pink_sheet_gold(data)

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

    async def _fetch_pink(self) -> bytes:
        if self._pink_fn is not None:
            return await self._pink_fn()
        import httpx

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(PINK_SHEET_URL)
        if response.status_code != 200:
            raise Unavailable(
                f"World Bank Pink Sheet returned HTTP {response.status_code}"
            )
        return response.content


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
