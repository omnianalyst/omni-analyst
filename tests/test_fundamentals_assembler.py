"""The fundamentals assembler: EDGAR claims -> the dict dcf_valuation consumes.

Every assertion is on an exact hand-seeded value, and every essential has a test
that its absence raises Unavailable (naming it) rather than padding a zero -- the
failure mode v1 ran on. Point-in-time correctness (no lookahead past the filing
date) is tested explicitly, because a backtest that sees a future filing is the
silent fabrication this module exists to prevent.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from omni.coverage.fundamentals import assemble_fundamentals
from omni.ingest.protocol import Unavailable

NOW = datetime.now(UTC)
CLAIM_TYPE = "fundamental_metric"


async def _fclaim(
    db,
    entity_id,
    concept,
    value,
    *,
    event_date,
    knowledge_date,
    fp="FY",
    form="10-K",
    start=None,
):
    await db.pool.execute(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id, evidence)
        VALUES ($1,'fundamental_metric',$2,$3::jsonb,'sec_edgar',$4,$5,1.0,
                'allowed',NULL,$6::jsonb)
        """,
        entity_id,
        concept,
        json.dumps({"value": value}),
        event_date,
        knowledge_date,
        json.dumps({"cik": "0000320193", "form": form, "fp": fp, "start": start}),
    )


def _fy(year, month=12, day=31):
    return datetime(year, month, day, tzinfo=UTC)


def _iso(dt):
    return dt.date().isoformat()


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _seed_essentials(
    db,
    e,
    *,
    fiscal_year=2024,
    ocf=1_200_000,
    capex=200_000,
    shares=100_000,
    cash=500_000,
    equity=4_000_000,
    long_term_debt=2_000_000,
    current_debt=100_000,
):
    """The non-revenue essentials the DCF refuses without. Balance-sheet items
    are instant (no period `start`); cash-flow items are duration and carry one
    to mirror real EDGAR shape."""
    end = _fy(fiscal_year)
    filed = end + timedelta(days=46)
    year_start = _iso(end - timedelta(days=365))
    pairs = [
        ("NetCashProvidedByUsedInOperatingActivities", ocf, year_start),
        ("PaymentsToAcquirePropertyPlantAndEquipment", capex, year_start),
        ("CommonStockSharesOutstanding", shares, None),
        ("CashAndCashEquivalentsAtCarryingValue", cash, None),
        ("StockholdersEquity", equity, None),
        ("LongTermDebt", long_term_debt, None),
        ("LongTermDebtCurrent", current_debt, None),
    ]
    for concept, val, start in pairs:
        await _fclaim(db, e, concept, val, event_date=end, knowledge_date=filed, start=start)


async def _full_set(
    db,
    e,
    *,
    fiscal_year=2024,
    ocf=1_200_000,
    capex=200_000,
    shares=100_000,
    cash=500_000,
    equity=4_000_000,
    long_term_debt=2_000_000,
    current_debt=100_000,
    revenue=5_000_000,
    prior_revenue=4_000_000,
):
    """Seed one fiscal year of the concepts the DCF needs, plus the prior year's
    Revenues so revenue_growth_rate is derivable. Filed ~7 weeks after year end.
    Revenue and cash-flow facts carry a period `start` (annual = end-365d) so
    the assembler's duration-based annual filter classifies them correctly."""
    end = _fy(fiscal_year)
    filed = end + timedelta(days=46)
    prior_end = _fy(fiscal_year - 1)
    prior_filed = prior_end + timedelta(days=46)
    await _seed_essentials(
        db, e, fiscal_year=fiscal_year, ocf=ocf, capex=capex, shares=shares,
        cash=cash, equity=equity, long_term_debt=long_term_debt,
        current_debt=current_debt,
    )
    await _fclaim(
        db, e, "Revenues", revenue,
        event_date=end, knowledge_date=filed, start=_iso(end - timedelta(days=365)),
    )
    await _fclaim(
        db, e, "Revenues", prior_revenue,
        event_date=prior_end, knowledge_date=prior_filed,
        start=_iso(prior_end - timedelta(days=365)),
    )


class TestAssembleFundamentals:
    async def test_the_full_set_maps_each_concept_to_its_field(self, db):
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','AAPL') RETURNING id"
        )
        await _full_set(db, e)
        as_of = _fy(2025, 6, 1)

        f = await assemble_fundamentals(
            db.pool, entity_id=e, as_of=as_of, current_price=100.0
        )

        assert f["cash_flow"]["operating_cash_flow"] == pytest.approx(1_200_000)
        assert f["cash_flow"]["capital_expenditures"] == pytest.approx(200_000)
        assert f["balance_sheet"]["shares_outstanding"] == pytest.approx(100_000)
        assert f["balance_sheet"]["cash_and_equivalents"] == pytest.approx(500_000)
        assert f["balance_sheet"]["total_equity"] == pytest.approx(4_000_000)
        # total_debt is the composite of long-term + current portion.
        assert f["balance_sheet"]["total_debt"] == pytest.approx(2_100_000)
        # revenue_growth_rate from two consecutive annual Revenues: 5/4 - 1.
        assert f["income_statement"]["revenue_growth_rate"] == pytest.approx(0.25)
        # market_cap is derived from shares * the supplied price.
        assert f["balance_sheet"]["market_cap"] == pytest.approx(10_000_000)

    async def test_a_filing_not_yet_known_is_invisible_point_in_time(self, db):
        """A fact whose knowledge_date is after as_of must not be read. Lookahead
        would hand a backtest a filing that did not yet exist -- the silent
        fabrication this module exists to prevent."""
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','AAPL') RETURNING id"
        )
        await _full_set(db, e)  # FY2024 filed 2025-02-15

        as_of = _fy(2024, 6, 1)  # before the FY2024 filing; nothing knowable
        with pytest.raises(Unavailable, match="fundamentals incomplete"):
            await assemble_fundamentals(db.pool, entity_id=e, as_of=as_of)

    async def test_a_restatement_at_a_later_knowledge_date_wins(self, db):
        """A 10-K/A restatement files at a later knowledge_date for the same
        period. The latest-knowable value is the point-in-time-correct one."""
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','AAPL') RETURNING id"
        )
        await _full_set(db, e, ocf=1_200_000)
        # Restate operating cash flow upward, filed a month later for FY2024.
        await _fclaim(
            db, e, "NetCashProvidedByUsedInOperatingActivities", 1_500_000,
            event_date=_fy(2024), knowledge_date=_fy(2025, 3, 15),
        )
        f = await assemble_fundamentals(
            db.pool, entity_id=e, as_of=_fy(2025, 6, 1), current_price=100.0
        )
        assert f["cash_flow"]["operating_cash_flow"] == pytest.approx(1_500_000)

    async def test_a_missing_essential_is_refused_not_zeroed(self, db):
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','AAPL') RETURNING id"
        )
        await _full_set(db, e, ocf=0)  # ocf=0 still seeds the claim; clear it instead
        # Remove operating cash flow entirely.
        await db.pool.execute(
            "DELETE FROM claim WHERE entity_id=$1 AND key='NetCashProvidedByUsedInOperatingActivities'",
            e,
        )
        with pytest.raises(Unavailable, match="operating_cash_flow"):
            await assemble_fundamentals(
                db.pool, entity_id=e, as_of=_fy(2025, 6, 1), current_price=100.0
            )

    async def test_no_debt_concept_is_refused_not_read_as_zero_debt(self, db):
        """A debt-free firm and a firm whose debt data is simply missing look the
        same from the facts (no LongTermDebt concept). The assembler refuses
        rather than reading either as zero-debt, because zero-debt understates
        the equity-bridge deduction and biases the fair value upward."""
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','AAPL') RETURNING id"
        )
        await _full_set(db, e)
        await db.pool.execute(
            "DELETE FROM claim WHERE entity_id=$1 AND key IN ('LongTermDebt','LongTermDebtCurrent')",
            e,
        )
        with pytest.raises(Unavailable, match="total_debt"):
            await assemble_fundamentals(
                db.pool, entity_id=e, as_of=_fy(2025, 6, 1), current_price=100.0
            )

    async def test_growth_is_unset_with_under_a_year_of_revenue_history(self, db):
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','AAPL') RETURNING id"
        )
        await _full_set(db, e)
        # Drop the prior-year Revenues, leaving only one annual period.
        await db.pool.execute(
            "DELETE FROM claim WHERE entity_id=$1 AND key='Revenues' "
            "AND event_date < '2024-01-01'",
            e,
        )
        f = await assemble_fundamentals(
            db.pool, entity_id=e, as_of=_fy(2025, 6, 1), current_price=100.0
        )
        assert "revenue_growth_rate" not in f["income_statement"]
        # The dict still assembles; the caller passes growth_rate or the DCF refuses.
        assert f["cash_flow"]["operating_cash_flow"] == pytest.approx(1_200_000)

    async def test_no_price_leaves_market_cap_out(self, db):
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','AAPL') RETURNING id"
        )
        await _full_set(db, e)
        f = await assemble_fundamentals(db.pool, entity_id=e, as_of=_fy(2025, 6, 1))
        assert "market_cap" not in f["balance_sheet"]

    async def test_capex_fallback_concept_is_used(self, db):
        """If the canonical capex concept is absent, the fallback
        (PaymentsToAcquireProductiveAssets) supplies it."""
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','AAPL') RETURNING id"
        )
        await _full_set(db, e)
        await db.pool.execute(
            "DELETE FROM claim WHERE entity_id=$1 "
            "AND key='PaymentsToAcquirePropertyPlantAndEquipment'",
            e,
        )
        await _fclaim(
            db, e, "PaymentsToAcquireProductiveAssets", 250_000,
            event_date=_fy(2024), knowledge_date=_fy(2025, 2, 15),
        )
        f = await assemble_fundamentals(
            db.pool, entity_id=e, as_of=_fy(2025, 6, 1), current_price=100.0
        )
        assert f["cash_flow"]["capital_expenditures"] == pytest.approx(250_000)

    async def test_a_mis_tagged_quarter_does_not_inflate_growth(self, db):
        """The annual filter is period duration, not the `fp` tag. EDGAR tags a
        quarterly fact fp='FY' when it is filed inside a 10-K, so the old
        fp-based filter admitted two adjacent quarters as 'consecutive years'
        and computed growth as a quarter-over-quarter ratio (~4x on AAPL). The
        duration filter excludes the 91-day quarter and reads the real annuals."""
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','AAPL') RETURNING id"
        )
        await _full_set(db, e, revenue=5_000_000, prior_revenue=4_000_000)
        # A quarter filed inside the 10-K: fp='FY' but a 91-day period.
        q_end = _fy(2024, 9, 30)
        await _fclaim(
            db, e, "Revenues", 1_200_000,
            event_date=q_end, knowledge_date=_fy(2025, 2, 15),
            start=_iso(q_end - timedelta(days=91)),
        )
        f = await assemble_fundamentals(
            db.pool, entity_id=e, as_of=_fy(2025, 6, 1), current_price=100.0
        )
        # 5M/4M - 1, NOT 5M/1.2M - 1: the quarter is excluded by duration.
        assert f["income_statement"]["revenue_growth_rate"] == pytest.approx(0.25)

    async def test_revenue_growth_spans_a_concept_rename(self, db):
        """A filer that moved from `Revenues` (legacy) to the ASC 606 concept
        still gets a YoY rate: the prior year under the legacy name and the
        current year under the modern name are both total annual revenue."""
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','AAPL') RETURNING id"
        )
        await _seed_essentials(db, e, fiscal_year=2024)
        await _fclaim(
            db, e, "RevenueFromContractWithCustomerExcludingAssessedTax", 5_000_000,
            event_date=_fy(2024), knowledge_date=_fy(2025, 2, 15),
            start=_iso(_fy(2024) - timedelta(days=365)),
        )
        await _fclaim(
            db, e, "Revenues", 4_000_000,
            event_date=_fy(2023), knowledge_date=_fy(2024, 2, 15),
            start=_iso(_fy(2023) - timedelta(days=365)),
        )
        f = await assemble_fundamentals(
            db.pool, entity_id=e, as_of=_fy(2025, 6, 1), current_price=100.0
        )
        assert f["income_statement"]["revenue_growth_rate"] == pytest.approx(0.25)

    async def test_a_revenue_fact_without_start_is_skipped(self, db):
        """A revenue fact with no period start cannot be confirmed annual and is
        skipped rather than guessed -- so it neither counts toward the two-
        period growth minimum nor distorts the ratio."""
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','AAPL') RETURNING id"
        )
        await _full_set(db, e, revenue=5_000_000, prior_revenue=4_000_000)
        # An extra revenue fact with no start (unconfirmable duration).
        await _fclaim(
            db, e, "Revenues", 999_999_999,
            event_date=_fy(2024, 6, 30), knowledge_date=_fy(2025, 2, 15), start=None,
        )
        f = await assemble_fundamentals(
            db.pool, entity_id=e, as_of=_fy(2025, 6, 1), current_price=100.0
        )
        assert f["income_statement"]["revenue_growth_rate"] == pytest.approx(0.25)
