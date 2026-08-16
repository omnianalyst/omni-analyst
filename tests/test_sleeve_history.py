"""Sleeve history ingestion: the long monthly series behind disaster context.

What these tests defend:

* only the three declared sleeves are fetchable, and an unknown key is an
  Unavailable (a named refusal), never a 500;
* ALFRED's "." placeholders are dropped, not stored as coverage;
* every draft carries the sleeve's key, unit, and provenance, so a reader of
  the claim store can tell gold from a yield by the row itself;
* current-values fetching (no vintages) is what the adapter requests -- the
  module docstring's descriptive-not-point-in-time contract.
"""

from datetime import UTC, datetime

import pytest

from omni.ingest.protocol import Unavailable
from omni.ingest.sleeve_history import (
    SLEEVE_SERIES,
    SleeveHistoryAdapter,
    parse_sleeve_observations,
)


def _obs(date: str, value) -> dict:
    return {"date": date, "value": value}


def test_only_declared_sleeves_are_series() -> None:
    assert set(SLEEVE_SERIES) == {
        "sleeve_gold",
        "sleeve_cash",
        "sleeve_long_bond_yield",
    }
    for meta in SLEEVE_SERIES.values():
        assert meta["series"] and meta["label"] and meta["unit"]


def test_observations_become_monthly_claims_with_provenance() -> None:
    drafts = parse_sleeve_observations(
        [_obs("1971-08-01", "40.50"), _obs("1971-09-01", "43.00")],
        sleeve="sleeve_gold",
    )

    assert len(drafts) == 2
    assert drafts[0].key == "sleeve_gold:GOLDAMGBD228NLBM"
    assert drafts[0].value["value"] == 40.5
    assert drafts[0].unit == "USD/oz"
    assert drafts[0].event_date == datetime(1971, 8, 1, tzinfo=UTC)
    assert drafts[0].knowledge_date == drafts[0].event_date
    assert drafts[0].confidence == 1.0


def test_alfred_placeholder_dots_are_dropped_not_stored() -> None:
    drafts = parse_sleeve_observations(
        [_obs("1971-08-01", "."), _obs("1971-09-01", "43.00"), _obs("", "1.0")],
        sleeve="sleeve_cash",
    )

    assert len(drafts) == 1
    assert drafts[0].value["value"] == 43.0


async def test_unknown_sleeve_is_a_named_refusal() -> None:
    adapter = SleeveHistoryAdapter(api_key="k", fetch_fn=lambda sid: [])
    with pytest.raises(Unavailable):
        await adapter.fetch("sleeve_tulips")


async def test_adapter_parses_through_the_injected_fetch() -> None:
    calls: list[str] = []

    async def fetch_fn(series_id: str) -> list[dict]:
        calls.append(series_id)
        return [_obs("1971-08-01", "0.52")]

    adapter = SleeveHistoryAdapter(api_key="k", fetch_fn=fetch_fn)
    drafts = await adapter.fetch("sleeve_cash")

    assert calls == ["TB3MS"]
    assert len(drafts) == 1
    assert drafts[0].key.startswith("sleeve_cash:")


async def test_missing_api_key_without_fetch_fn_is_unavailable() -> None:
    adapter = SleeveHistoryAdapter(api_key=None)
    with pytest.raises(Unavailable):
        await adapter.fetch("sleeve_cash")  # pink_fn also unset -> Unavailable


def _pink_fixture() -> bytes:
    """A minimal Pink Sheet: header row 5, units row 6, three data rows,
    one `…` (not available) and one malformed period -- everything the parser
    must handle."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Monthly Prices"
    ws.append(["World Bank Commodities Price Data"])
    ws.append([])
    ws.append([])
    ws.append(["Updated 2026-07"])
    ws.append(["", "Crude oil, avg", "Gold"])
    ws.append(["", "($/bbl)", "($/oz)"])
    ws.append(["1960M01", 1.63, 35.27])
    ws.append(["1960M02", 1.63, "…"])
    ws.append(["1960M03", 1.63, 35.10])
    ws.append(["not a period", 1.63, 35.10])
    ws.append(["1961M01", 1.63, 35.42])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_pink_sheet_gold_parses_with_worldbank_provenance() -> None:
    from omni.ingest.sleeve_history import parse_pink_sheet_gold

    drafts = parse_pink_sheet_gold(_pink_fixture())

    assert len(drafts) == 3  # the … row and the malformed period are skipped
    assert drafts[0].key == "sleeve_gold:WORLD_BANK_PINK"
    assert drafts[0].value["value"] == 35.27
    assert drafts[0].unit == "USD/oz"
    assert drafts[0].source == "worldbank"  # per-draft provenance, not "fred"
    assert drafts[-1].event_date == datetime(1961, 1, 1, tzinfo=UTC)


def test_pink_sheet_without_a_gold_column_is_a_named_error() -> None:
    import io

    import openpyxl

    from omni.ingest.sleeve_history import parse_pink_sheet_gold

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Monthly Prices"
    ws.append([])
    ws.append([])
    ws.append([])
    ws.append([])
    ws.append(["", "Crude oil, avg"])
    out = io.BytesIO()
    wb.save(out)

    with pytest.raises(ValueError, match="Gold"):
        parse_pink_sheet_gold(out.getvalue())


async def test_gold_fetches_through_the_injected_pink_fn() -> None:
    async def pink() -> bytes:
        return _pink_fixture()

    adapter = SleeveHistoryAdapter(api_key=None, pink_fn=pink)
    drafts = await adapter.fetch("sleeve_gold")

    assert len(drafts) == 3
    assert all(d.source == "worldbank" for d in drafts)


async def test_fred_series_still_flow_through_their_own_fetch() -> None:
    async def fetch_fn(series_id: str) -> list[dict]:
        assert series_id == "TB3MS"
        return [{"date": "1934-01-01", "value": "0.52"}]

    adapter = SleeveHistoryAdapter(api_key="k", fetch_fn=fetch_fn)
    drafts = await adapter.fetch("sleeve_cash")

    assert len(drafts) == 1
    assert drafts[0].source is None  # falls through to the adapter's "fred"


async def test_ensure_sleeve_demand_places_one_row_per_series(db):
    from omni.ingest.sleeve_history import ensure_sleeve_demand

    await db.pool.execute("TRUNCATE demand CASCADE")
    await db.pool.execute("DELETE FROM entity WHERE kind = 'macro'")
    macro = await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('macro','US_MACRO','US Macroeconomy') RETURNING id"
    )
    created = await ensure_sleeve_demand(db.pool)

    assert created == 3
    rows = await db.pool.fetch(
        "SELECT key FROM demand WHERE entity_id = $1 "
        "AND claim_type = 'sleeve_history_point' AND active",
        macro,
    )
    assert {r["key"] for r in rows} == set(SLEEVE_SERIES)


async def test_ensure_sleeve_demand_is_idempotent(db):
    from omni.ingest.sleeve_history import ensure_sleeve_demand

    await db.pool.execute("TRUNCATE demand CASCADE")
    await db.pool.execute("DELETE FROM entity WHERE kind = 'macro'")
    await db.pool.execute(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('macro','US_MACRO','US Macroeconomy')"
    )
    first = await ensure_sleeve_demand(db.pool)
    second = await ensure_sleeve_demand(db.pool)

    assert first == 3
    assert second == 0
    count = await db.pool.fetchval(
        "SELECT count(*) FROM demand WHERE claim_type = 'sleeve_history_point'"
    )
    assert count == 3


async def test_ensure_sleeve_demand_refuses_without_the_macro_entity(db):
    from omni.ingest.sleeve_history import ensure_sleeve_demand

    await db.pool.execute("TRUNCATE demand CASCADE")
    await db.pool.execute("DELETE FROM entity WHERE kind = 'macro'")

    with pytest.raises(RuntimeError, match="US_MACRO"):
        await ensure_sleeve_demand(db.pool)


async def test_sleeve_demand_reopens_monthly_until_filled(db):
    """The staleness window is the refresh heartbeat: a gap detected on stale
    demand is the mechanism that keeps 55 years of monthly data current, so
    it must be on the order of a publication cycle, not a scrape interval."""
    from datetime import UTC, datetime, timedelta

    from omni.ingest.sleeve_history import ensure_sleeve_demand

    await db.pool.execute("TRUNCATE demand CASCADE")
    await db.pool.execute("DELETE FROM entity WHERE kind = 'macro'")
    await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('macro','US_MACRO','US Macroeconomy') RETURNING id"
    )
    await ensure_sleeve_demand(db.pool)
    stale_days = int(
        await db.pool.fetchval(
            "SELECT extract(days FROM max_staleness) FROM demand "
            "WHERE claim_type = 'sleeve_history_point' LIMIT 1"
        )
    )

    assert 28 <= stale_days <= 40
    # and the demand is old enough to be stale right now, so a gap exists
    age = datetime.now(UTC) - timedelta(days=stale_days + 1)
    await db.pool.execute(
        "UPDATE demand SET created_at = $1 WHERE claim_type = 'sleeve_history_point'",
        age,
    )
    from omni.coverage.gaps import detect_gaps

    gaps = await detect_gaps(db.pool)
    assert any(g["claim_type"] == "sleeve_history_point" for g in gaps)
