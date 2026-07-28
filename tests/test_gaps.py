"""The gap engine — demand minus coverage, scoped to an audience.

The redistribution rule is the one that must not break. These tests hold both
halves of it against the engine: a private claim owned by A must close A's gap
and must not even be visible to B's. The other classes are tested for honest
detection and honest absence.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.coverage.gaps import (
    GAP_CLASS_WEIGHTS,
    detect_gaps,
    persist_gaps,
    resolve_gap,
)

NOW = datetime(2026, 7, 27, tzinfo=UTC)
DAWN = datetime(2000, 1, 1, tzinfo=UTC)


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company', $1, $1) "
        "RETURNING id",
        symbol,
    )


async def _demand(
    db,
    entity_id,
    *,
    key="Revenues",
    requested_by=None,
    weight=1.0,
    max_staleness=None,
    min_confidence=None,
    claim_type="fundamental_metric",
    channel="test",
):
    return await db.pool.fetchval(
        "INSERT INTO demand (entity_id, claim_type, key, channel, requested_by, "
        "weight, max_staleness, min_confidence) "
        "VALUES ($1, $2::claim_type, $3, $4, $5, $6, $7, $8) RETURNING id",
        entity_id,
        claim_type,
        key,
        channel,
        requested_by,
        weight,
        max_staleness,
        min_confidence,
    )


async def _claim(
    db,
    entity_id,
    key="Revenues",
    *,
    value='{"amount": 1000}',
    source="sec_edgar",
    confidence=0.9,
    knowledge_date=None,
    event_date=None,
    audience=None,
    claim_type="fundamental_metric",
):
    redistributable = "allowed" if audience is None else "byo_only"
    kd = knowledge_date or NOW
    ed = event_date or (kd - timedelta(days=1))
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10)
        RETURNING id
        """,
        entity_id,
        claim_type,
        key,
        value,
        source,
        ed,
        kd,
        confidence,
        redistributable,
        audience,
    )


def _classes(gaps, *, audience=None):
    return {
        g["gap_class"]
        for g in gaps
        if audience is None or g["audience_user_id"] == audience
    }


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestMissing:
    async def test_no_visible_claim_is_a_missing_gap(self, db):
        entity_id = await _entity(db)
        await _demand(db, entity_id)

        gaps = await detect_gaps(db.pool)
        assert "missing" in _classes(gaps)
        assert len(gaps) == 1
        assert gaps[0]["detail"]["reason"] == "no visible claim"

    async def test_a_null_valued_claim_is_coverage_not_absence(self, db):
        """FRED records 'no figure published yet' as a JSON null. Treating that
        as missing would make the engine re-request a known hole forever."""
        entity_id = await _entity(db)
        await _demand(db, entity_id)
        await _claim(db, entity_id, value="null")

        gaps = await detect_gaps(db.pool)
        assert "missing" not in _classes(gaps)


class TestStale:
    async def test_a_claim_older_than_max_staleness_is_stale(self, db):
        entity_id = await _entity(db)
        await _demand(
            db, entity_id, max_staleness=timedelta(days=1)
        )
        await _claim(db, entity_id, knowledge_date=DAWN)

        gaps = await detect_gaps(db.pool)
        assert "stale" in _classes(gaps)

    async def test_a_demand_without_a_staleness_budget_never_goes_stale(
        self, db
    ):
        entity_id = await _entity(db)
        await _demand(db, entity_id)
        await _claim(db, entity_id, knowledge_date=DAWN)

        gaps = await detect_gaps(db.pool)
        assert "stale" not in _classes(gaps)

    async def test_a_fresh_claim_is_not_stale(self, db):
        entity_id = await _entity(db)
        await _demand(
            db, entity_id, max_staleness=timedelta(days=365 * 100)
        )
        await _claim(db, entity_id, knowledge_date=NOW)

        gaps = await detect_gaps(db.pool)
        assert "stale" not in _classes(gaps)


class TestLowConfidence:
    async def test_a_claim_below_min_confidence_is_low_confidence(self, db):
        entity_id = await _entity(db)
        await _demand(db, entity_id, min_confidence=0.8)
        await _claim(db, entity_id, confidence=0.3)

        gaps = await detect_gaps(db.pool)
        assert "low_confidence" in _classes(gaps)

    async def test_a_confident_claim_is_not_low_confidence(self, db):
        entity_id = await _entity(db)
        await _demand(db, entity_id, min_confidence=0.8)
        await _claim(db, entity_id, confidence=0.95)

        gaps = await detect_gaps(db.pool)
        assert "low_confidence" not in _classes(gaps)

    async def test_best_confidence_is_used_when_claims_differ(self, db):
        """One strong claim should rescue the demand from low_confidence."""
        entity_id = await _entity(db)
        await _demand(db, entity_id, min_confidence=0.8)
        await _claim(
            db, entity_id, key="Revenues", confidence=0.3, source="alpha"
        )
        await _claim(
            db,
            entity_id,
            key="Revenues",
            confidence=0.95,
            source="beta",
            event_date=NOW,
            knowledge_date=NOW,
        )

        gaps = await detect_gaps(db.pool)
        assert "low_confidence" not in _classes(gaps)


class TestUnverified:
    async def test_a_single_source_is_unverified(self, db):
        entity_id = await _entity(db)
        await _demand(db, entity_id)
        await _claim(db, entity_id, source="sec_edgar")

        gaps = await detect_gaps(db.pool)
        assert "unverified" in _classes(gaps)

    async def test_two_claims_from_the_same_source_stay_unverified(self, db):
        """Two vintages from one source are one voice, not corroboration."""
        entity_id = await _entity(db)
        await _demand(db, entity_id)
        await _claim(
            db,
            entity_id,
            source="sec_edgar",
            event_date=NOW - timedelta(days=2),
            knowledge_date=NOW - timedelta(days=1),
        )
        await _claim(
            db, entity_id, source="sec_edgar", event_date=NOW, knowledge_date=NOW
        )

        gaps = await detect_gaps(db.pool)
        assert "unverified" in _classes(gaps)

    async def test_two_distinct_sources_clear_unverified(self, db):
        entity_id = await _entity(db)
        await _demand(db, entity_id)
        await _claim(db, entity_id, source="sec_edgar")
        await _claim(
            db,
            entity_id,
            source="fred",
            event_date=NOW,
            knowledge_date=NOW,
        )

        gaps = await detect_gaps(db.pool)
        assert "unverified" not in _classes(gaps)


class TestContradictory:
    async def test_two_sources_disagreeing_is_contradictory(self, db):
        """The most valuable class; it must surface even when coverage is full."""
        entity_id = await _entity(db)
        await _demand(db, entity_id)
        await _claim(
            db,
            entity_id,
            value='{"amount": 1000}',
            source="sec_edgar",
            event_date=NOW,
            knowledge_date=NOW,
        )
        await _claim(
            db,
            entity_id,
            value='{"amount": 2000}',
            source="fred",
            event_date=NOW,
            knowledge_date=NOW,
        )

        gaps = await detect_gaps(db.pool)
        assert "contradictory" in _classes(gaps)
        [con] = [g for g in gaps if g["gap_class"] == "contradictory"]
        assert con["detail"]["conflicts"][0]["sources"] == ["fred", "sec_edgar"]

    async def test_two_sources_agreeing_is_not_contradictory(self, db):
        entity_id = await _entity(db)
        await _demand(db, entity_id)
        await _claim(
            db,
            entity_id,
            value='{"amount": 1000}',
            source="sec_edgar",
            event_date=NOW,
            knowledge_date=NOW,
        )
        await _claim(
            db,
            entity_id,
            value='{"amount": 1000}',
            source="fred",
            event_date=NOW,
            knowledge_date=NOW,
        )

        gaps = await detect_gaps(db.pool)
        assert "contradictory" not in _classes(gaps)

    async def test_different_event_dates_are_not_a_contradiction(self, db):
        """A disagreement across time is the world moving, not a conflict."""
        entity_id = await _entity(db)
        await _demand(db, entity_id)
        await _claim(
            db,
            entity_id,
            value='{"amount": 1000}',
            source="sec_edgar",
            event_date=NOW - timedelta(days=1),
            knowledge_date=NOW - timedelta(days=1),
        )
        await _claim(
            db,
            entity_id,
            value='{"amount": 2000}',
            source="fred",
            event_date=NOW,
            knowledge_date=NOW,
        )

        gaps = await detect_gaps(db.pool)
        assert "contradictory" not in _classes(gaps)


class TestAudienceScoping:
    """The redistribution rule, exercised end-to-end through the engine."""

    async def test_a_private_claim_does_not_close_another_users_gap(self, db):
        """The leak this module exists to prevent."""
        entity_id = await _entity(db)
        owner_a = uuid4()
        owner_b = uuid4()
        await _demand(db, entity_id, requested_by=owner_a)
        await _demand(db, entity_id, requested_by=owner_b)
        await _claim(db, entity_id, source="polygon", audience=owner_a)

        gaps = await detect_gaps(db.pool)

        # A can see its own private claim: no missing gap.
        assert "missing" not in _classes(gaps, audience=owner_a)
        # B cannot see A's private claim: the gap is still open for B.
        assert "missing" in _classes(gaps, audience=owner_b)

    async def test_a_private_claim_closes_the_owners_own_gap(self, db):
        entity_id = await _entity(db)
        owner = uuid4()
        await _demand(db, entity_id, requested_by=owner)
        await _claim(db, entity_id, source="polygon", audience=owner)

        gaps = await detect_gaps(db.pool)
        assert "missing" not in _classes(gaps, audience=owner)

    async def test_a_network_demand_is_evaluated_against_shared_coverage(self, db):
        """requested_by IS NULL means the demand is for the shared network."""
        entity_id = await _entity(db)
        await _demand(db, entity_id, requested_by=None)
        await _claim(
            db,
            entity_id,
            source="polygon",
            audience=uuid4(),
            claim_type="price_snapshot",
            key="bars",
        )

        gaps = await detect_gaps(db.pool)
        assert "missing" in _classes(gaps, audience=None)

    async def test_the_audience_filter_restricts_which_demands_run(self, db):
        entity_id = await _entity(db)
        owner = uuid4()
        await _demand(db, entity_id, requested_by=owner)
        await _demand(db, entity_id, key="EPS", requested_by=None)

        gaps = await detect_gaps(db.pool, audience=owner)
        assert {g["key"] for g in gaps} == {"Revenues"}


class TestScoring:
    async def test_each_class_multiplies_the_demand_weight(self, db):
        entity_id = await _entity(db)
        await _demand(
            db,
            entity_id,
            weight=2.0,
            max_staleness=timedelta(days=1),
            min_confidence=0.95,
        )
        await _claim(
            db,
            entity_id,
            source="sec_edgar",
            confidence=0.1,
            knowledge_date=DAWN,
        )

        gaps = {g["gap_class"]: g["score"] for g in await detect_gaps(db.pool)}
        # One weak, stale, single-source claim fires these three. missing cannot
        # (a claim exists) and contradictory cannot (one source, nothing to
        # disagree with).
        for klass in ("stale", "low_confidence", "unverified"):
            assert gaps[klass] == pytest.approx(2.0 * GAP_CLASS_WEIGHTS[klass])

    async def test_contradictory_score_uses_the_demand_weight(self, db):
        entity_id = await _entity(db)
        await _demand(db, entity_id, weight=2.0)
        await _claim(
            db,
            entity_id,
            value='{"amount": 1000}',
            source="sec_edgar",
            event_date=NOW,
            knowledge_date=NOW,
        )
        await _claim(
            db,
            entity_id,
            value='{"amount": 2000}',
            source="fred",
            event_date=NOW,
            knowledge_date=NOW,
        )

        gaps = {g["gap_class"]: g["score"] for g in await detect_gaps(db.pool)}
        assert gaps["contradictory"] == pytest.approx(
            2.0 * GAP_CLASS_WEIGHTS["contradictory"]
        )

    async def test_weights_sum_when_two_demands_target_the_same_fact(self, db):
        entity_id = await _entity(db)
        await _demand(db, entity_id, weight=2.0)
        await _demand(db, entity_id, weight=3.0)

        gaps = await detect_gaps(db.pool)
        [missing] = [g for g in gaps if g["gap_class"] == "missing"]
        assert missing["score"] == pytest.approx(
            5.0 * GAP_CLASS_WEIGHTS["missing"]
        )

    async def test_contradictory_outranks_missing_for_the_same_weight(self, db):
        assert (
            GAP_CLASS_WEIGHTS["contradictory"] > GAP_CLASS_WEIGHTS["missing"]
            > GAP_CLASS_WEIGHTS["stale"]
            > GAP_CLASS_WEIGHTS["low_confidence"]
            > GAP_CLASS_WEIGHTS["unverified"]
        )


class TestPersist:
    async def test_persist_writes_a_gap_row(self, db):
        entity_id = await _entity(db)
        await _demand(db, entity_id)

        gaps = await detect_gaps(db.pool)
        written = await persist_gaps(db.pool, gaps)
        assert written == 1
        row = await db.pool.fetchrow(
            "SELECT gap_class, score, resolved_at FROM gap"
        )
        assert row["gap_class"] == "missing"
        assert row["resolved_at"] is None
        assert row["score"] == pytest.approx(GAP_CLASS_WEIGHTS["missing"])

    async def test_persisting_twice_updates_rather_than_duplicating(self, db):
        entity_id = await _entity(db)
        await _demand(db, entity_id, weight=1.0)

        first = await detect_gaps(db.pool)
        await persist_gaps(db.pool, first)
        assert await db.pool.fetchval("SELECT count(*) FROM gap") == 1
        first_detected = await db.pool.fetchval("SELECT detected_at FROM gap")

        # The demand's weight rises; the re-run must refresh score and
        # detected_at on the existing row, not insert a second one.
        await db.pool.execute("UPDATE demand SET weight = 5.0")
        second = await detect_gaps(db.pool)
        await persist_gaps(db.pool, second)

        assert await db.pool.fetchval("SELECT count(*) FROM gap") == 1
        row = await db.pool.fetchrow("SELECT score, detected_at FROM gap")
        assert row["score"] == pytest.approx(5.0 * GAP_CLASS_WEIGHTS["missing"])
        assert row["detected_at"] >= first_detected

    async def test_persisting_nothing_is_a_noop(self, db):
        assert await persist_gaps(db.pool, []) == 0


class TestResolve:
    async def test_resolve_sets_resolved_at(self, db):
        entity_id = await _entity(db)
        await _demand(db, entity_id)
        gaps = await detect_gaps(db.pool)
        await persist_gaps(db.pool, gaps)
        gap_id = await db.pool.fetchval(
            "SELECT id FROM gap WHERE resolved_at IS NULL"
        )

        assert await resolve_gap(db.pool, gap_id) is True
        assert await db.pool.fetchval(
            "SELECT resolved_at IS NOT NULL FROM gap WHERE id = $1", gap_id
        )

    async def test_a_resolved_gap_may_reopen(self, db):
        """The index permits it: detection writes a fresh open row."""
        entity_id = await _entity(db)
        await _demand(db, entity_id)
        await persist_gaps(db.pool, await detect_gaps(db.pool))
        gap_id = await db.pool.fetchval("SELECT id FROM gap")
        await resolve_gap(db.pool, gap_id)

        await persist_gaps(db.pool, await detect_gaps(db.pool))
        assert await db.pool.fetchval("SELECT count(*) FROM gap") == 2
        open_rows = await db.pool.fetchval(
            "SELECT count(*) FROM gap WHERE resolved_at IS NULL"
        )
        assert open_rows == 1

    async def test_resolve_is_idempotent(self, db):
        entity_id = await _entity(db)
        await _demand(db, entity_id)
        await persist_gaps(db.pool, await detect_gaps(db.pool))
        gap_id = await db.pool.fetchval("SELECT id FROM gap")

        assert await resolve_gap(db.pool, gap_id) is True
        assert await resolve_gap(db.pool, gap_id) is False
