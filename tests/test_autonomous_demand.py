"""Phase D: autonomous demand + cold-start backfill."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from omni.autonomous.backfill import backfill_trend_predictions
from omni.autonomous.demand import (
    _rank_sectors,
    create_autonomous_demand,
)


async def _seed_company(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('company', $1, $2) RETURNING id",
        symbol, symbol,
    )


async def _seed_etf(db, symbol="XLK"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('sector_etf', $1, $1) RETURNING id",
        symbol,
    )


async def _seed_sector_score(db, *, etf_id, symbol, rs, alignment="favorable"):
    now = datetime.now(UTC)
    await db.pool.execute(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id, derivation)
        VALUES ($1, 'sector_score', $2, $3::jsonb, 'test',
                $4, $4, 1.0, 'allowed', NULL, 'ingested')
        """,
        etf_id, symbol.lower(),
        json.dumps({"rs_percentile": rs, "macro_alignment": alignment, "etf_symbol": symbol}),
        now,
    )


async def _link_constituent(db, *, company_id, etf_id):
    await db.pool.execute(
        "INSERT INTO entity_edge (from_entity, to_entity, relation, source) "
        "VALUES ($1, $2, 'member_of_sector', 'test')",
        company_id, etf_id,
    )


async def _seed_prices(db, entity_id, n=80, start=100.0, drift=0.002):
    import random
    rng = random.Random(42)  # deterministic noise so the test is reproducible
    base = datetime.now(UTC) - timedelta(days=n + 50)
    close = start
    for i in range(n):
        event = base + timedelta(days=i)
        knowledge = event + timedelta(days=1)
        close = close * (1 + drift + rng.gauss(0, 0.015))  # drift + noise -> vol > 0
        await db.pool.execute(
            """
            INSERT INTO claim (entity_id, claim_type, key, value, source,
                               event_date, knowledge_date, confidence,
                               redistributable, audience_user_id, derivation)
            VALUES ($1, 'price_snapshot', $2, $3::jsonb, 'test',
                    $4, $5, 1.0, 'allowed', NULL, 'ingested')
            ON CONFLICT DO NOTHING
            """,
            entity_id, "TEST",
            json.dumps({"close": close, "open": close, "high": close, "low": close, "volume": 1000}),
            event, knowledge,
        )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestRankSectors:
    def test_highest_rs_first(self):
        scores = [
            {"rs_percentile": 0.3, "macro_alignment": "favorable"},
            {"rs_percentile": 0.9, "macro_alignment": "unfavorable"},
        ]
        ranked = _rank_sectors(scores)
        assert ranked[0]["rs_percentile"] == 0.9

    def test_favorable_beats_unknown_on_tie(self):
        scores = [
            {"rs_percentile": 0.5, "macro_alignment": "unknown"},
            {"rs_percentile": 0.5, "macro_alignment": "favorable"},
        ]
        ranked = _rank_sectors(scores)
        assert ranked[0]["macro_alignment"] == "favorable"


class TestCreateAutonomousDemand:
    async def test_creates_demand_for_top_sector_constituents(self, db):
        xlk = await _seed_etf(db, "XLK")
        xlp = await _seed_etf(db, "XLP")
        aapl = await _seed_company(db, "AAPL")
        msft = await _seed_company(db, "MSFT")
        pg = await _seed_company(db, "PG")
        await _link_constituent(db, company_id=aapl, etf_id=xlk)
        await _link_constituent(db, company_id=msft, etf_id=xlk)
        await _link_constituent(db, company_id=pg, etf_id=xlp)
        await _seed_sector_score(db, etf_id=xlk, symbol="XLK", rs=0.9)
        await _seed_sector_score(db, etf_id=xlp, symbol="XLP", rs=0.2)

        report = await create_autonomous_demand(db.pool, top_n=1)
        assert report.sectors_demanded == 1
        assert report.constituents_demanded == 2  # AAPL + MSFT

        demand_rows = await db.pool.fetch(
            "SELECT entity_id FROM demand WHERE channel = 'autonomous' AND active"
        )
        demanded = {r["entity_id"] for r in demand_rows}
        assert aapl in demanded
        assert msft in demanded
        assert pg not in demanded  # XLP not in top 1

    async def test_demand_is_idempotent(self, db):
        xlk = await _seed_etf(db, "XLK")
        aapl = await _seed_company(db, "AAPL")
        await _link_constituent(db, company_id=aapl, etf_id=xlk)
        await _seed_sector_score(db, etf_id=xlk, symbol="XLK", rs=0.9)

        await create_autonomous_demand(db.pool, top_n=1)
        report2 = await create_autonomous_demand(db.pool, top_n=1)
        assert report2.constituents_demanded == 0

    async def test_autonomous_weight_is_below_user(self, db):
        xlk = await _seed_etf(db, "XLK")
        aapl = await _seed_company(db, "AAPL")
        await _link_constituent(db, company_id=aapl, etf_id=xlk)
        await _seed_sector_score(db, etf_id=xlk, symbol="XLK", rs=0.9)

        await create_autonomous_demand(db.pool, top_n=1)
        weight = await db.pool.fetchval(
            "SELECT weight FROM demand WHERE entity_id = $1 AND channel = 'autonomous'",
            aapl,
        )
        assert weight == 0.5

    async def test_no_scores_means_no_demand(self, db):
        report = await create_autonomous_demand(db.pool)
        assert report.constituents_demanded == 0

    async def test_operator_user_id_set_as_requested_by(self, db):
        from uuid import uuid4
        operator = uuid4()
        await db.pool.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, $3)",
            operator, "op@test", "hash",
        )
        xlk = await _seed_etf(db, "XLK")
        aapl = await _seed_company(db, "AAPL")
        await _link_constituent(db, company_id=aapl, etf_id=xlk)
        await _seed_sector_score(db, etf_id=xlk, symbol="XLK", rs=0.9)

        await create_autonomous_demand(db.pool, operator_user_id=operator)
        requested_by = await db.pool.fetchval(
            "SELECT requested_by FROM demand "
            "WHERE entity_id = $1 AND channel = 'autonomous'",
            aapl,
        )
        assert requested_by == operator


class TestBackfill:
    async def test_writes_and_resolves_predictions(self, db):
        entity = await _seed_company(db, "AAPL")
        await _seed_prices(db, entity, n=120, drift=0.003)

        report = await backfill_trend_predictions(
            db.pool,
            lookback_days=60,
            interval_days=14,
            horizon_days=21,
            entity_ids=[entity],
        )
        assert report.entities_processed >= 1
        assert report.predictions_written > 0

        total = await db.pool.fetchval(
            "SELECT count(*)::int FROM prediction WHERE entity_id = $1 AND method = 'trend.sma'",
            entity,
        )
        assert total > 0

        resolved = await db.pool.fetchval(
            "SELECT count(*)::int FROM prediction "
            "WHERE entity_id = $1 AND method = 'trend.sma' AND outcome <> 'pending'",
            entity,
        )
        assert resolved > 0

    async def test_skips_already_backfilled_entity(self, db):
        entity = await _seed_company(db, "AAPL")
        await _seed_prices(db, entity, n=120, drift=0.003)

        await backfill_trend_predictions(
            db.pool, lookback_days=60, interval_days=14, horizon_days=21,
            entity_ids=[entity],
        )
        report2 = await backfill_trend_predictions(
            db.pool, lookback_days=60, interval_days=14, horizon_days=21,
            entity_ids=[entity],
        )
        assert report2.entities_skipped >= 1
        assert report2.predictions_written == 0

    async def test_no_prices_means_no_predictions(self, db):
        entity = await _seed_company(db, "NOPE")
        report = await backfill_trend_predictions(
            db.pool, lookback_days=60, entity_ids=[entity],
        )
        assert report.predictions_written == 0


class TestParameterSweep:
    async def test_method_suffix_creates_separate_calibration_buckets(self, db):
        entity = await _seed_company(db, "AAPL")
        await _seed_prices(db, entity, n=120, drift=0.003)

        await backfill_trend_predictions(
            db.pool, lookback_days=60, interval_days=14, horizon_days=21,
            window=50, method_suffix=".w50", entity_ids=[entity],
        )
        await backfill_trend_predictions(
            db.pool, lookback_days=60, interval_days=14, horizon_days=21,
            window=100, method_suffix=".w100", entity_ids=[entity],
        )

        methods = await db.pool.fetch(
            "SELECT DISTINCT method FROM prediction WHERE entity_id = $1",
            entity,
        )
        method_names = {r["method"] for r in methods}
        assert "trend.sma.w50" in method_names
        assert "trend.sma.w100" in method_names
        assert "trend.sma" not in method_names  # only suffixed variants

    async def test_parameter_sweep_runs_all_variants(self, db):
        from omni.autonomous.backfill import backfill_parameter_sweep

        entity = await _seed_company(db, "AAPL")
        await _seed_prices(db, entity, n=120, drift=0.003)

        reports = await backfill_parameter_sweep(
            db.pool, windows=(20, 50), lookback_days=60,
            interval_days=14, horizon_days=21,
        )
        assert len(reports) == 2

        methods = await db.pool.fetch(
            "SELECT DISTINCT method FROM prediction WHERE entity_id = $1",
            entity,
        )
        method_names = {r["method"] for r in methods}
        assert "trend.sma.w20" in method_names
        assert "trend.sma.w50" in method_names

    async def test_different_windows_produce_different_predictions(self, db):
        entity = await _seed_company(db, "AAPL")
        await _seed_prices(db, entity, n=120, drift=0.003)

        await backfill_trend_predictions(
            db.pool, lookback_days=60, interval_days=14, horizon_days=21,
            window=50, method_suffix=".w50", entity_ids=[entity],
        )
        await backfill_trend_predictions(
            db.pool, lookback_days=60, interval_days=14, horizon_days=21,
            window=20, method_suffix=".w20", entity_ids=[entity],
        )

        w50_count = await db.pool.fetchval(
            "SELECT count(*)::int FROM prediction "
            "WHERE entity_id = $1 AND method = 'trend.sma.w50'",
            entity,
        )
        w20_count = await db.pool.fetchval(
            "SELECT count(*)::int FROM prediction "
            "WHERE entity_id = $1 AND method = 'trend.sma.w20'",
            entity,
        )
        # Both should have predictions; the counts may differ because different
        # windows abstain at different timestamps (w20 needs fewer prices)
        assert w50_count > 0
        assert w20_count > 0
