"""Phases E + F: synthesis findings and meta-calibration."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from omni.autonomous.meta import meta_hit_rate, resolve_meta
from omni.autonomous.synthesis import enrich_findings


async def _seed_entity(db, kind, symbol, name=None):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ($1, $2, $3) RETURNING id",
        kind, symbol, name or symbol,
    )


async def _seed_claim(db, *, entity_id, claim_type, value, key="k", days_ago=0, source="test"):
    now = datetime.now(UTC)
    event = now - timedelta(days=days_ago + 1)
    knowledge = now - timedelta(days=days_ago)
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id, derivation)
        VALUES ($1, $2::claim_type, $3, $4::jsonb, $5, $6, $7, 1.0,
                'allowed', NULL, 'ingested')
        RETURNING id
        """,
        entity_id, claim_type, key, json.dumps(value), source, event, knowledge,
    )


async def _seed_prices(db, entity_id, closes, *, start_days_ago=None):
    """Seed prices ending NOW so they cover the assessment-to-now window."""
    if start_days_ago is None:
        start_days_ago = len(closes) + 40
    base = datetime.now(UTC) - timedelta(days=start_days_ago)
    for i, close in enumerate(closes):
        event = base + timedelta(days=i)
        knowledge = event + timedelta(days=1)
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
            json.dumps({"close": close, "open": close, "high": close, "low": close, "volume": 1000}),
            event, knowledge,
        )


async def _seed_price_path(db, entity_id, *, start_price, end_price, start_days_ago, days):
    """Seed a linear price path from start_price to end_price over `days` days.

    Used by meta-calibration tests: the path spans [now - start_days_ago, now],
    so it covers the assessment-to-now window the resolver reads.
    """
    base = datetime.now(UTC) - timedelta(days=start_days_ago)
    for i in range(days):
        frac = i / max(days - 1, 1)
        close = start_price + (end_price - start_price) * frac
        event = base + timedelta(days=i)
        knowledge = event + timedelta(days=1)
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
            json.dumps({"close": close, "open": close, "high": close, "low": close, "volume": 1000}),
            event, knowledge,
        )


async def _seed_finding(db, *, entity_id, prediction_id):
    await db.pool.execute(
        """
        INSERT INTO finding (claim_id, entity_id, status, method, confidence,
                             threshold, calibrated_hit_rate, supporting,
                             disconfirming, prediction_id)
        VALUES (NULL, $1, 'surfaced', 'trend.sma', 0.7, 0.6, 0.7,
                '["autonomous directional call"]'::jsonb, '[]'::jsonb, $2)
        """,
        entity_id, prediction_id,
    )


async def _seed_prediction(db, *, entity_id):
    return await db.pool.fetchval(
        """
        INSERT INTO prediction (entity_id, method, direction, confidence,
                                entry_price, upper_barrier, lower_barrier,
                                horizon_ends_at, provenance)
        VALUES ($1, 'trend.sma', 'up', 0.7, 100, 110, 90,
                now() + interval '90 days', '{}'::jsonb)
        RETURNING id
        """,
        entity_id,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


# -- Phase E: synthesis -------------------------------------------------------

class TestSynthesis:
    async def test_enriches_finding_with_full_chain(self, db):
        company = await _seed_entity(db, "company", "AAPL")
        etf = await _seed_entity(db, "sector_etf", "XLK")
        macro = await _seed_entity(db, "macro", "US_MACRO")
        await db.pool.execute(
            "INSERT INTO entity_edge (from_entity, to_entity, relation, source) "
            "VALUES ($1, $2, 'member_of_sector', 'test')",
            company, etf,
        )
        await _seed_claim(db, entity_id=etf, claim_type="sector_score",
                          value={"rs_percentile": 0.85, "trend": "uptrend", "macro_alignment": "favorable"},
                          key="xlk")
        await _seed_claim(db, entity_id=macro, claim_type="regime_assessment",
                          value={"cycle_phase": "expansion", "risk_regime": "risk_on"},
                          key="us_macro")
        pid = await _seed_prediction(db, entity_id=company)
        await _seed_finding(db, entity_id=company, prediction_id=pid)

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
        assert chain[2]["direction"] == "up"

    async def test_partial_chain_when_no_regime(self, db):
        company = await _seed_entity(db, "company", "AAPL")
        etf = await _seed_entity(db, "sector_etf", "XLK")
        await db.pool.execute(
            "INSERT INTO entity_edge (from_entity, to_entity, relation, source) "
            "VALUES ($1, $2, 'member_of_sector', 'test')",
            company, etf,
        )
        await _seed_claim(db, entity_id=etf, claim_type="sector_score",
                          value={"rs_percentile": 0.7, "trend": "uptrend", "macro_alignment": "favorable"},
                          key="xlk")
        pid = await _seed_prediction(db, entity_id=company)
        await _seed_finding(db, entity_id=company, prediction_id=pid)

        report = await enrich_findings(db.pool)
        chain_raw = await db.pool.fetchval(
            "SELECT deduction_chain FROM finding WHERE prediction_id = $1", pid
        )
        chain = json.loads(chain_raw) if isinstance(chain_raw, str) else chain_raw
        layers = [c["layer"] for c in chain]
        assert layers == ["sector", "stock"]

    async def test_idempotent(self, db):
        company = await _seed_entity(db, "company", "AAPL")
        pid = await _seed_prediction(db, entity_id=company)
        await _seed_finding(db, entity_id=company, prediction_id=pid)

        await enrich_findings(db.pool)
        report2 = await enrich_findings(db.pool)
        assert report2.findings_enriched == 0

    async def test_byo_only_sector_score_does_not_leak_into_shared_finding(self, db):
        # The licensing invariant: a byo_only claim (audience = operator X,
        # redistributable = byo_only) must never land in a SHARED finding's
        # deduction_chain -- a shared finding is served to anonymous callers on
        # the public domain via /briefing. The synthesis query must scope the
        # sector_score to the finding's own audience. (Before the fix the sector
        # query had no audience filter and the byo_only value leaked.)
        company = await _seed_entity(db, "company", "AAPL")
        etf = await _seed_entity(db, "sector_etf", "XLK")
        await db.pool.execute(
            "INSERT INTO entity_edge (from_entity, to_entity, relation, source) "
            "VALUES ($1, $2, 'member_of_sector', 'test')",
            company, etf,
        )
        from uuid import uuid4
        operator = await db.pool.fetchval(
            "INSERT INTO users (email, password_hash) "
            "VALUES ($1, 'x') RETURNING id",
            f"synth-op-{uuid4().hex}@example.com",
        )
        now = datetime.now(UTC)
        # A byo_only sector_score attributed to the operator -- private.
        await db.pool.execute(
            """
            INSERT INTO claim (entity_id, claim_type, key, value, source,
                               event_date, knowledge_date, confidence,
                               redistributable, audience_user_id, derivation)
            VALUES ($1, 'sector_score', 'xlk', $2::jsonb, 'polygon',
                    $3, $4, 1.0, 'byo_only', $5, 'ingested')
            """,
            etf,
            json.dumps({"rs_percentile": 0.85, "trend": "uptrend",
                        "macro_alignment": "favorable"}),
            now - timedelta(days=2), now - timedelta(days=1), operator,
        )
        # A SHARED finding (audience_user_id NULL) on the company.
        pid = await _seed_prediction(db, entity_id=company)
        await _seed_finding(db, entity_id=company, prediction_id=pid)

        await enrich_findings(db.pool)

        chain_raw = await db.pool.fetchval(
            "SELECT deduction_chain FROM finding WHERE prediction_id = $1", pid
        )
        chain = json.loads(chain_raw) if isinstance(chain_raw, str) else chain_raw
        layers = [c["layer"] for c in chain]
        # The byo_only sector_score is invisible to a shared finding, so the
        # sector layer is absent -- the chain carries only stock.
        assert "sector" not in layers
        assert layers == ["stock"]


# -- Phase F: meta-calibration ------------------------------------------------

class TestMetaCalibration:
    async def test_regime_risk_on_correct_when_market_rises(self, db):
        macro = await _seed_entity(db, "macro", "US_MACRO")
        spy = await _seed_entity(db, "index", "SPY")
        await _seed_claim(db, entity_id=macro, claim_type="regime_assessment",
                          value={"risk_regime": "risk_on"}, days_ago=40)
        # price path from 100 to 105 over 45 days ending now -- covers [now-40, now-10]
        await _seed_price_path(db, spy, start_price=100.0, end_price=105.0,
                               start_days_ago=45, days=45)

        report = await resolve_meta(db.pool, horizon_days=30)
        assert report.regimes_resolved == 1

        correct = await db.pool.fetchval(
            "SELECT correct FROM meta_resolution WHERE claim_type = 'regime_assessment'"
        )
        assert correct is True

    async def test_regime_risk_on_wrong_when_market_falls(self, db):
        macro = await _seed_entity(db, "macro", "US_MACRO")
        spy = await _seed_entity(db, "index", "SPY")
        await _seed_claim(db, entity_id=macro, claim_type="regime_assessment",
                          value={"risk_regime": "risk_on"}, days_ago=40)
        await _seed_price_path(db, spy, start_price=105.0, end_price=100.0,
                               start_days_ago=45, days=45)

        await resolve_meta(db.pool, horizon_days=30)
        correct = await db.pool.fetchval(
            "SELECT correct FROM meta_resolution WHERE claim_type = 'regime_assessment'"
        )
        assert correct is False

    async def test_sector_resolved(self, db):
        etf = await _seed_entity(db, "sector_etf", "XLK")
        # Peers: priced for the cross-section but carrying no sector_score -- only
        # XLK is scored here. XLK leads them over the window.
        xle = await _seed_entity(db, "sector_etf", "XLE")
        xlf = await _seed_entity(db, "sector_etf", "XLF")
        await _seed_claim(db, entity_id=etf, claim_type="sector_score",
                          value={"rs_percentile": 0.8, "trend": "uptrend", "macro_alignment": "favorable"},
                          days_ago=40)
        await _seed_price_path(db, etf, start_price=100.0, end_price=108.0,
                               start_days_ago=45, days=45)
        await _seed_price_path(db, xle, start_price=100.0, end_price=101.0,
                               start_days_ago=45, days=45)
        await _seed_price_path(db, xlf, start_price=100.0, end_price=104.0,
                               start_days_ago=45, days=45)

        report = await resolve_meta(db.pool, horizon_days=30)
        assert report.sectors_resolved == 1
        correct = await db.pool.fetchval(
            "SELECT correct FROM meta_resolution WHERE claim_type = 'sector_score'"
        )
        assert correct is True  # top-rs sector that genuinely led its peers

    async def test_top_sector_that_only_rose_with_the_market_is_wrong(self, db):
        # The defect this guards: a top-ranked sector that rose but lagged its
        # peers must score WRONG, not correct. The old absolute-return logic
        # scored it correct (return > 0) -- measuring market beta, not
        # leadership, so every sector in a bull market scored a hit. XLK rises
        # 2% (positive) but XLE leads at 10% and XLF at 5%; median ~5%; XLK
        # (rs=0.8, top) underperformed the median -> wrong.
        xlk = await _seed_entity(db, "sector_etf", "XLK")
        xle = await _seed_entity(db, "sector_etf", "XLE")
        xlf = await _seed_entity(db, "sector_etf", "XLF")
        await _seed_claim(db, entity_id=xlk, claim_type="sector_score",
                          value={"rs_percentile": 0.8, "trend": "uptrend", "macro_alignment": "favorable"},
                          days_ago=40)
        await _seed_price_path(db, xlk, start_price=100.0, end_price=102.0,
                               start_days_ago=45, days=45)
        await _seed_price_path(db, xle, start_price=100.0, end_price=110.0,
                               start_days_ago=45, days=45)
        await _seed_price_path(db, xlf, start_price=100.0, end_price=105.0,
                               start_days_ago=45, days=45)

        await resolve_meta(db.pool, horizon_days=30)
        correct = await db.pool.fetchval(
            "SELECT correct FROM meta_resolution WHERE claim_type = 'sector_score'"
        )
        assert correct is False  # rose with the market but did not lead

    async def test_meta_hit_rate_returns_none_below_floor(self, db):
        etf = await _seed_entity(db, "sector_etf", "XLK")
        for i in range(3):
            await _seed_claim(db, entity_id=etf, claim_type="sector_score",
                              value={"rs_percentile": 0.8}, days_ago=40+i)

        await _seed_price_path(db, etf, start_price=100.0, end_price=103.0,
                               start_days_ago=45, days=45)
        await resolve_meta(db.pool, horizon_days=30)
        rate = await meta_hit_rate(db.pool, claim_type="sector_score")
        assert rate is None  # < 5 resolutions

    async def test_idempotent(self, db):
        macro = await _seed_entity(db, "macro", "US_MACRO")
        spy = await _seed_entity(db, "index", "SPY")
        await _seed_claim(db, entity_id=macro, claim_type="regime_assessment",
                          value={"risk_regime": "risk_on"}, days_ago=40)
        await _seed_price_path(db, spy, start_price=100.0, end_price=103.0,
                               start_days_ago=45, days=45)

        await resolve_meta(db.pool, horizon_days=30)
        report2 = await resolve_meta(db.pool, horizon_days=30)
        assert report2.regimes_resolved == 0
