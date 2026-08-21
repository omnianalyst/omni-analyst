"""Phases E + F: synthesis findings and meta-calibration."""

import json
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from omni.autonomous.meta import meta_hit_rate, resolve_meta
from omni.autonomous.synthesis import enrich_findings
from omni.conviction.publish import briefing


async def _seed_entity(db, kind, symbol, name=None):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ($1, $2, $3) RETURNING id",
        kind, symbol, name or symbol,
    )


async def _seed_claim(
    db,
    *,
    entity_id,
    claim_type,
    value,
    key="k",
    days_ago=0,
    source="test",
    event_date=None,
    knowledge_date=None,
):
    now = datetime.now(UTC)
    event = event_date or now - timedelta(days=days_ago + 1)
    knowledge = knowledge_date or now - timedelta(days=days_ago)
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


async def _seed_finding(db, *, entity_id, prediction_id, audience_user_id=None):
    return await db.pool.fetchval(
        """
        INSERT INTO finding (claim_id, entity_id, audience_user_id, status, method, confidence,
                             threshold, calibrated_hit_rate, supporting,
                             disconfirming, prediction_id)
        VALUES (NULL, $1, $3, 'surfaced', 'trend.sma', 0.7, 0.6, 0.7,
                '["autonomous directional call"]'::jsonb, '[]'::jsonb, $2)
        RETURNING id
        """,
        entity_id, prediction_id, audience_user_id,
    )


async def _latest_chain(db, finding_id):
    raw = await db.pool.fetchval(
        """
        SELECT deduction_chain
        FROM finding_enrichment_revision
        WHERE finding_id = $1
        ORDER BY evidence_as_of DESC, created_at DESC, id DESC
        LIMIT 1
        """,
        finding_id,
    )
    return json.loads(raw) if isinstance(raw, str) else raw


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
        evidence_event = datetime.now(UTC) - timedelta(days=2)
        evidence_knowledge = evidence_event + timedelta(days=1)
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
                          key="us_macro", event_date=evidence_event,
                          knowledge_date=evidence_knowledge)
        pid = await _seed_prediction(db, entity_id=company)
        finding_id = await _seed_finding(db, entity_id=company, prediction_id=pid)

        report = await enrich_findings(db.pool)
        assert report.findings_enriched == 1

        chain = await _latest_chain(db, finding_id)
        layers = [c["layer"] for c in chain]
        assert layers == ["macro", "sector", "stock"]
        assert chain[0]["cycle_phase"] == "expansion"
        assert chain[1]["etf_symbol"] == "XLK"
        assert chain[2]["direction"] == "up"
        assert chain[0]["evidence"]["source"] == "test"
        assert chain[0]["evidence"]["redistributable"] == "allowed"
        assert chain[0]["evidence"]["audience_user_id"] is None
        assert chain[0]["evidence"]["event_date"] == evidence_event.isoformat()
        assert chain[0]["evidence"]["knowledge_date"] == evidence_knowledge.isoformat()

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
        finding_id = await _seed_finding(db, entity_id=company, prediction_id=pid)

        await enrich_findings(db.pool)
        chain = await _latest_chain(db, finding_id)
        layers = [c["layer"] for c in chain]
        assert layers == ["sector", "stock"]

    async def test_idempotent(self, db):
        company = await _seed_entity(db, "company", "AAPL")
        pid = await _seed_prediction(db, entity_id=company)
        finding_id = await _seed_finding(db, entity_id=company, prediction_id=pid)

        await enrich_findings(db.pool)
        report2 = await enrich_findings(db.pool)
        assert report2.findings_enriched == 0
        assert report2.findings_skipped == 1
        assert await db.pool.fetchval(
            "SELECT count(*) FROM finding_enrichment_revision WHERE finding_id = $1",
            finding_id,
        ) == 1

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
                               credential_owner, redistributable,
                               audience_user_id, derivation)
            VALUES ($1, 'sector_score', 'xlk', $2::jsonb, 'polygon',
                    $3, $4, 1.0, 'operator-x', 'byo_only', $5, 'ingested')
            """,
            etf,
            json.dumps({"rs_percentile": 0.85, "trend": "uptrend",
                        "macro_alignment": "favorable"}),
            now - timedelta(days=2), now - timedelta(days=1), operator,
        )
        # A SHARED finding (audience_user_id NULL) on the company.
        pid = await _seed_prediction(db, entity_id=company)
        finding_id = await _seed_finding(db, entity_id=company, prediction_id=pid)

        await enrich_findings(db.pool)

        chain = await _latest_chain(db, finding_id)
        layers = [c["layer"] for c in chain]
        # The byo_only sector_score is invisible to a shared finding, so the
        # sector layer is absent -- the chain carries only stock.
        assert "sector" not in layers
        assert layers == ["stock"]

        private_pid = await _seed_prediction(db, entity_id=company)
        private_finding_id = await _seed_finding(
            db,
            entity_id=company,
            prediction_id=private_pid,
            audience_user_id=operator,
        )
        await enrich_findings(db.pool)
        private_chain = await _latest_chain(db, private_finding_id)
        assert private_chain[0]["layer"] == "sector"
        assert private_chain[0]["evidence"]["credential_owner"] == "operator-x"
        assert private_chain[0]["evidence"]["redistributable"] == "byo_only"
        assert private_chain[0]["evidence"]["audience_user_id"] == str(operator)

    async def test_future_evidence_creates_a_later_revision_without_mutating_history(
        self, db
    ):
        company = await _seed_entity(db, "company", "AAPL")
        etf = await _seed_entity(db, "sector_etf", "XLK")
        await db.pool.execute(
            "INSERT INTO entity_edge (from_entity, to_entity, relation, source) "
            "VALUES ($1, $2, 'member_of_sector', 'test')",
            company, etf,
        )
        base = datetime.now(UTC)
        old_claim = await _seed_claim(
            db,
            entity_id=etf,
            claim_type="sector_score",
            value={"rs_percentile": 0.6, "trend": "flat"},
            key="old",
            event_date=base - timedelta(days=2),
            knowledge_date=base - timedelta(days=1),
        )
        future_claim = await _seed_claim(
            db,
            entity_id=etf,
            claim_type="sector_score",
            value={"rs_percentile": 0.95, "trend": "uptrend"},
            key="future",
            event_date=base + timedelta(minutes=30),
            knowledge_date=base + timedelta(hours=2),
        )
        pid = await _seed_prediction(db, entity_id=company)
        finding_id = await _seed_finding(db, entity_id=company, prediction_id=pid)
        finding_before = dict(
            await db.pool.fetchrow("SELECT * FROM finding WHERE id = $1", finding_id)
        )

        first_as_of = base + timedelta(hours=1)
        await enrich_findings(db.pool, as_of=first_as_of)
        first_revision = await db.pool.fetchrow(
            "SELECT id, deduction_chain FROM finding_enrichment_revision "
            "WHERE finding_id = $1",
            finding_id,
        )
        first_chain = json.loads(first_revision["deduction_chain"])
        assert first_chain[0]["claim_id"] == str(old_claim)
        assert str(future_claim) not in first_revision["deduction_chain"]

        second_as_of = base + timedelta(hours=3)
        report = await enrich_findings(db.pool, as_of=second_as_of)
        assert report.findings_enriched == 1
        revisions = await db.pool.fetch(
            "SELECT id, deduction_chain FROM finding_enrichment_revision "
            "WHERE finding_id = $1 ORDER BY evidence_as_of",
            finding_id,
        )
        assert len(revisions) == 2
        assert revisions[0]["id"] == first_revision["id"]
        assert revisions[0]["deduction_chain"] == first_revision["deduction_chain"]
        second_chain = json.loads(revisions[1]["deduction_chain"])
        assert second_chain[0]["claim_id"] == str(future_claim)
        assert dict(await db.pool.fetchrow(
            "SELECT * FROM finding WHERE id = $1", finding_id
        )) == finding_before
        assert (await briefing(db.pool))[0]["deduction_chain"] == revisions[1]["deduction_chain"]

    async def test_covered_findings_are_not_re_read(self, db):
        # A revision whose evidence_as_of is newer than all existing evidence
        # already holds the chain this run would build. Re-deriving it re-reads
        # sector/regime claims for every surfaced finding on every cycle --
        # hundreds of thousands of reads on a grown store -- to produce a
        # result the content compare then discards. The covered path must skip
        # without issuing a single per-finding query (fetch/fetchrow); the
        # counting proxy fails the test if the skip is only content-deep.
        from datetime import UTC as _UTC

        class CountingPool:
            def __init__(self, pool):
                self._pool = pool
                self.list_reads = 0
                self.row_reads = 0

            def __getattr__(self, name):
                attr = getattr(self._pool, name)
                if name == "fetchrow":
                    async def counted_row(*args, **kwargs):
                        self.row_reads += 1
                        return await attr(*args, **kwargs)
                    return counted_row
                if name == "fetch":
                    async def counted_list(*args, **kwargs):
                        self.list_reads += 1
                        return await attr(*args, **kwargs)
                    return counted_list
                return attr

        company = await _seed_entity(db, "company", "AAPL")
        macro = await _seed_entity(db, "macro", "US_MACRO")
        base = datetime.now(_UTC)
        await _seed_claim(
            db, entity_id=macro, claim_type="regime_assessment",
            value={"cycle_phase": "expansion", "risk_regime": "risk_on"},
            key="us_macro", event_date=base - timedelta(days=2),
            knowledge_date=base - timedelta(days=1),
        )
        pid = await _seed_prediction(db, entity_id=company)
        finding_id = await _seed_finding(db, entity_id=company, prediction_id=pid)

        # Comfortably after seeding: the finding's created_at comes from the
        # DB clock, which can sit a few ms ahead of the app clock.
        first = datetime.now(_UTC) + timedelta(seconds=5)
        await enrich_findings(db.pool, as_of=first)
        assert await db.pool.fetchval(
            "SELECT count(*) FROM finding_enrichment_revision WHERE finding_id = $1",
            finding_id,
        ) == 1

        # No new evidence arrived: first revision's evidence_as_of covers the
        # horizon, so the second run must not touch the finding tables again.
        proxy = CountingPool(db.pool)
        report = await enrich_findings(proxy, as_of=first + timedelta(minutes=5))
        assert report.findings_enriched == 0
        assert report.findings_skipped == 1
        assert proxy.list_reads == 1  # the to-enrich select, returning empty
        assert proxy.row_reads == 0   # zero per-finding claim reads
        assert await db.pool.fetchval(
            "SELECT count(*) FROM finding_enrichment_revision WHERE finding_id = $1",
            finding_id,
        ) == 1

    async def test_new_evidence_uncovers_the_finding(self, db):
        # The covered skip must yield the moment new evidence lands: a later
        # regime claim re-opens the finding and writes the updated chain.
        from datetime import UTC as _UTC

        company = await _seed_entity(db, "company", "AAPL")
        macro = await _seed_entity(db, "macro", "US_MACRO")
        base = datetime.now(_UTC)
        await _seed_claim(
            db, entity_id=macro, claim_type="regime_assessment",
            value={"risk_regime": "risk_on"}, key="r1",
            event_date=base - timedelta(days=3),
            knowledge_date=base - timedelta(days=2),
        )
        pid = await _seed_prediction(db, entity_id=company)
        finding_id = await _seed_finding(db, entity_id=company, prediction_id=pid)

        first = base + timedelta(hours=1)
        await enrich_findings(db.pool, as_of=first)
        assert await db.pool.fetchval(
            "SELECT count(*) FROM finding_enrichment_revision WHERE finding_id = $1",
            finding_id,
        ) == 1

        await _seed_claim(
            db, entity_id=macro, claim_type="regime_assessment",
            value={"risk_regime": "risk_off"}, key="r2",
            event_date=base + timedelta(hours=2),
            knowledge_date=base + timedelta(hours=3),
        )
        report = await enrich_findings(db.pool, as_of=base + timedelta(hours=4))
        assert report.findings_enriched == 1
        chain = await _latest_chain(db, finding_id)
        assert chain[0]["risk_regime"] == "risk_off"

    async def test_finding_and_enrichment_revisions_refuse_mutation(self, db):
        company = await _seed_entity(db, "company", "AAPL")
        pid = await _seed_prediction(db, entity_id=company)
        finding_id = await _seed_finding(db, entity_id=company, prediction_id=pid)
        await enrich_findings(db.pool)

        with pytest.raises(asyncpg.RestrictViolationError):
            await db.pool.execute(
                "UPDATE finding SET confidence = 0.1 WHERE id = $1", finding_id
            )
        with pytest.raises(asyncpg.RestrictViolationError):
            await db.pool.execute(
                "UPDATE finding_enrichment_revision "
                "SET deduction_chain = '[]'::jsonb WHERE finding_id = $1",
                finding_id,
            )


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


class TestTheNightlyCatchUpIsNotAReadStorm:
    """2026-08-21, live: the nightly regime claim advances the evidence
    horizon, every surfaced finding becomes uncovered at once, and the
    catch-up pass re-read the SAME regime row once per finding -- 160K
    identical reads, ~30 minutes, "no recent success" on the System page
    while it churned. The evidence reads are pure in (keys, evidence_as_of)
    within a pass; they must be memoized. The counting proxy asserts the
    regime query runs exactly once no matter how many findings are uncovered.
    """

    async def test_the_regime_row_is_read_once_per_pass(self, db):
        class RegimeCounter:
            def __init__(self, pool):
                self._pool = pool
                self.regime_reads = 0

            def __getattr__(self, name):
                attr = getattr(self._pool, name)
                if name == "fetchrow":
                    async def counted(*args, **kwargs):
                        if args and "regime_assessment" in str(args[0]):
                            self.regime_reads += 1
                        return await attr(*args, **kwargs)
                    return counted
                return attr

        macro = await _seed_entity(db, "macro", "US_MACRO")
        base = datetime.now(UTC) - timedelta(days=1)
        await _seed_claim(
            db, entity_id=macro, claim_type="regime_assessment",
            value={"risk_regime": "risk_on"}, key="us_macro",
            event_date=base - timedelta(days=1), knowledge_date=base,
        )
        # Two companies, two findings, neither covered at this horizon.
        ids = []
        for sym in ("AAA", "BBB"):
            company = await _seed_entity(db, "company", sym)
            pid = await _seed_prediction(db, entity_id=company)
            await _seed_finding(db, entity_id=company, prediction_id=pid)
            ids.append(company)

        proxy = RegimeCounter(db.pool)
        # Comfortably after seeding: created_at comes from the DB clock.
        report = await enrich_findings(
            proxy, as_of=datetime.now(UTC) + timedelta(seconds=5)
        )
        assert report.findings_enriched == 2
        assert proxy.regime_reads == 1, (
            "the regime row is constant for a whole pass; reading it per "
            "finding is the storm"
        )
