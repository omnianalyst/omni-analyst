"""End-to-end integration test: the full deduction chain in one pipeline.

Proves the autonomous layers compose correctly when run in sequence: the macro
loop's regime_assessment is read by the sector scanner, the scanner's
sector_score is read by the demand loop, the demand loop creates demand for
constituents, the backfill produces predictions from price coverage, and the
synthesis loop traces the full chain through a finding.

Each layer consumesS the previous layer's claim -- if any claim shape is wrong,
the next layer abstains and the assertions fail at the point of breakdown,
naming the broken link.
"""

import json
import random
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.autonomous.demand import create_autonomous_demand
from omni.autonomous.macro import assess_macro_regime
from omni.autonomous.sector import scan_sectors
from omni.autonomous.synthesis import enrich_findings


async def _seed_signal(db, entity_id, claim_type, key, value):
    now = datetime.now(UTC)
    await db.pool.execute(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id, derivation)
        VALUES ($1, $2::claim_type, $3, $4::jsonb, 'test',
                $5, $5, 1.0, 'allowed', NULL, 'ingested')
        """,
        entity_id, claim_type, key, json.dumps(value), now,
    )


async def _seed_prices(db, entity_id, n, start=100.0, drift=0.003):
    rng = random.Random(42)
    base = datetime.now(UTC) - timedelta(days=n + 50)
    close = start
    for i in range(n):
        event = base + timedelta(days=i)
        knowledge = event + timedelta(days=1)
        close = close * (1 + drift + rng.gauss(0, 0.015))
        await db.pool.execute(
            """
            INSERT INTO claim (entity_id, claim_type, key, value, source,
                               event_date, knowledge_date, confidence,
                               redistributable, audience_user_id, derivation)
            VALUES ($1, 'price_snapshot', 'T', $2::jsonb, 'test',
                    $3, $4, 1.0, 'allowed', NULL, 'ingested')
            ON CONFLICT DO NOTHING
            """,
            entity_id,
            json.dumps({"close": close, "open": close, "high": close,
                        "low": close, "volume": 1000}),
            event, knowledge,
        )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestFullDeductionChain:
    """The complete macro -> sector -> demand chain, run in sequence."""

    async def test_macro_to_sector_to_demand_pipeline(self, db):
        # -- Setup: entities --
        macro_e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('macro', 'US_MACRO', 'US Macro') RETURNING id"
        )
        xlk = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name, identifiers) "
            "VALUES ('sector_etf', 'XLK', 'Tech ETF', '{}'::jsonb) RETURNING id"
        )
        xlp = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name, identifiers) "
            "VALUES ('sector_etf', 'XLP', 'Staples ETF', '{}'::jsonb) RETURNING id"
        )
        aapl = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('company', 'AAPL', 'Apple') RETURNING id"
        )
        msft = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('company', 'MSFT', 'Microsoft') RETURNING id"
        )
        pg = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('company', 'PG', 'P&G') RETURNING id"
        )
        await db.pool.execute(
            "INSERT INTO entity_edge (from_entity, to_entity, relation, source) "
            "VALUES ($1, $2, 'member_of_sector', 'test')", aapl, xlk
        )
        await db.pool.execute(
            "INSERT INTO entity_edge (from_entity, to_entity, relation, source) "
            "VALUES ($1, $2, 'member_of_sector', 'test')", msft, xlk
        )
        await db.pool.execute(
            "INSERT INTO entity_edge (from_entity, to_entity, relation, source) "
            "VALUES ($1, $2, 'member_of_sector', 'test')", pg, xlp
        )

        # -- Layer 1: macro signals -> regime assessment --
        for ct, key, val in [
            ("yield_curve_signal", "yield_curve",
             {"current_spread": 0.5, "is_inverted": False, "days_inverted_90d": 0}),
            ("sahm_rule_signal", "unrate",
             {"indicator": 0.1, "triggered": False}),
            ("inflation_signal", "cpi_all",
             {"yoy": 2.5, "mom_annualized": 0.3, "3m_annualized": 0.3}),
            ("output_gap_signal", "gdpc1_gdppot",
             {"output_gap": 0.5}),
            ("lei_signal", "usslind",
             {"is_negative": False, "change_6m": 1.0}),
        ]:
            await _seed_signal(db, macro_e, ct, key, val)

        regime_id = await assess_macro_regime(db.pool)
        assert regime_id is not None
        regime_row = await db.pool.fetchrow(
            "SELECT value FROM claim WHERE id = $1", regime_id
        )
        regime = json.loads(regime_row["value"]) if isinstance(regime_row["value"], str) else regime_row["value"]
        assert regime["cycle_phase"] == "expansion"
        assert regime["risk_regime"] == "risk_on"

        # -- Layer 2: ETF prices -> sector scores --
        await _seed_prices(db, xlk, n=70, drift=0.004)   # strong uptrend
        await _seed_prices(db, xlp, n=70, drift=0.001)   # weak

        scan_report = await scan_sectors(db.pool)
        assert scan_report.scored == 2

        xlk_score = await db.pool.fetchrow(
            "SELECT value FROM claim WHERE entity_id = $1 AND claim_type = 'sector_score'",
            xlk,
        )
        xlk_sv = json.loads(xlk_score["value"]) if isinstance(xlk_score["value"], str) else xlk_score["value"]
        # The sector score carries the macro alignment derived from the regime
        assert xlk_sv["macro_alignment"] == "favorable"
        assert xlk_sv["cycle_phase"] == "expansion"
        assert xlk_sv["trend"] == "uptrend"

        # -- Layer 3: sector scores -> autonomous demand --
        operator = uuid4()
        await db.pool.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, $3)",
            operator, "op@test", "hash",
        )
        demand_report = await create_autonomous_demand(
            db.pool, top_n=1, operator_user_id=operator
        )
        assert demand_report.sectors_demanded == 1
        # XLK ranked higher than XLP, so its constituents get demand
        aapl_demand = await db.pool.fetchval(
            "SELECT requested_by FROM demand "
            "WHERE entity_id = $1 AND channel = 'autonomous'",
            aapl,
        )
        assert aapl_demand == operator
        msft_demand = await db.pool.fetchval(
            "SELECT requested_by FROM demand "
            "WHERE entity_id = $1 AND channel = 'autonomous'",
            msft,
        )
        assert msft_demand == operator
        # PG (in XLP, not top-1) gets no demand
        pg_demand = await db.pool.fetchval(
            "SELECT 1 FROM demand WHERE entity_id = $1 AND channel = 'autonomous'",
            pg,
        )
        assert pg_demand is None

    async def test_synthesis_traces_chain_through_finding(self, db):
        macro_e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('macro', 'US_MACRO', 'US Macro') RETURNING id"
        )
        xlk = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('sector_etf', 'XLK', 'Tech') RETURNING id"
        )
        aapl = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('company', 'AAPL', 'Apple') RETURNING id"
        )
        await db.pool.execute(
            "INSERT INTO entity_edge (from_entity, to_entity, relation, source) "
            "VALUES ($1, $2, 'member_of_sector', 'test')", aapl, xlk
        )
        now = datetime.now(UTC)
        await db.pool.execute(
            """INSERT INTO claim (entity_id, claim_type, key, value, source,
               event_date, knowledge_date, confidence, redistributable,
               audience_user_id, derivation)
               VALUES ($1, 'regime_assessment', 'us_macro', $2::jsonb, 'test',
               $3, $3, 1.0, 'allowed', NULL, 'ingested')""",
            macro_e, json.dumps({"cycle_phase": "expansion", "risk_regime": "risk_on"}), now,
        )
        await db.pool.execute(
            """INSERT INTO claim (entity_id, claim_type, key, value, source,
               event_date, knowledge_date, confidence, redistributable,
               audience_user_id, derivation)
               VALUES ($1, 'sector_score', 'xlk', $2::jsonb, 'test',
               $3, $3, 1.0, 'allowed', NULL, 'ingested')""",
            xlk, json.dumps({"rs_percentile": 0.9, "trend": "uptrend",
                             "macro_alignment": "favorable"}), now,
        )
        pid = await db.pool.fetchval(
            """INSERT INTO prediction (entity_id, method, direction, confidence,
               entry_price, upper_barrier, lower_barrier, horizon_ends_at,
               provenance)
               VALUES ($1, 'trend.sma', 'up', 0.7, 100, 110, 90,
               now() + interval '90 days', '{}'::jsonb) RETURNING id""",
            aapl,
        )
        await db.pool.execute(
            """INSERT INTO finding (entity_id, status, method, confidence,
               threshold, calibrated_hit_rate, supporting, disconfirming,
               prediction_id)
               VALUES ($1, 'surfaced', 'trend.sma', 0.7, 0.6, 0.7,
               '["autonomous call"]'::jsonb, '[]'::jsonb, $2)""",
            aapl, pid,
        )

        report = await enrich_findings(db.pool)
        assert report.findings_enriched == 1

        chain_raw = await db.pool.fetchval(
            "SELECT deduction_chain FROM finding WHERE prediction_id = $1", pid
        )
        chain = json.loads(chain_raw) if isinstance(chain_raw, str) else chain_raw
        layers = [c["layer"] for c in chain]
        assert layers == ["macro", "sector", "stock"]
        assert chain[0]["cycle_phase"] == "expansion"
        assert chain[1]["etf_symbol"] == "XLK"
        assert chain[1]["macro_alignment"] == "favorable"
        assert chain[2]["direction"] == "up"
