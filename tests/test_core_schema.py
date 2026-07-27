from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

NOW = datetime(2026, 7, 27, tzinfo=UTC)


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company', $1, $2) "
        "RETURNING id",
        symbol,
        f"{symbol} Inc.",
    )


async def _insert_claim(db, entity_id, **overrides):
    fields = {
        "claim_type": "fundamental_metric",
        "key": "Revenues",
        "value": '{"amount": 1000}',
        "source": "sec_edgar",
        "event_date": NOW - timedelta(days=30),
        "knowledge_date": NOW,
        "confidence": 0.9,
        "credential_owner": None,
        "redistributable": "allowed",
        "audience_user_id": None,
    }
    fields.update(overrides)
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           credential_owner, redistributable, audience_user_id)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        entity_id,
        fields["claim_type"],
        fields["key"],
        fields["value"],
        fields["source"],
        fields["event_date"],
        fields["knowledge_date"],
        fields["confidence"],
        fields["credential_owner"],
        fields["redistributable"],
        fields["audience_user_id"],
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, demand CASCADE")
    yield


class TestRedistribution:
    """The rule that makes the shared-coverage economics legal."""

    async def test_shared_claim_has_no_audience(self, db):
        entity_id = await _entity(db)
        claim_id = await _insert_claim(db, entity_id, redistributable="allowed")
        assert claim_id is not None

    async def test_byo_claim_scoped_to_its_credential_owner(self, db):
        entity_id = await _entity(db)
        user = uuid4()
        claim_id = await _insert_claim(
            db,
            entity_id,
            source="polygon",
            redistributable="byo_only",
            credential_owner="user",
            audience_user_id=user,
        )
        assert claim_id is not None

    async def test_byo_claim_cannot_enter_shared_coverage(self, db):
        entity_id = await _entity(db)
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_claim(
                db,
                entity_id,
                source="polygon",
                redistributable="byo_only",
                audience_user_id=None,
            )

    async def test_shared_claim_cannot_be_privately_scoped(self, db):
        entity_id = await _entity(db)
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_claim(
                db, entity_id, redistributable="allowed", audience_user_id=uuid4()
            )

    async def test_prohibited_sources_are_never_stored(self, db):
        entity_id = await _entity(db)
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_claim(
                db,
                entity_id,
                source="yahoo",
                redistributable="prohibited",
                audience_user_id=uuid4(),
            )


class TestClaimIdentity:
    async def test_reingesting_the_same_observation_is_rejected(self, db):
        entity_id = await _entity(db)
        await _insert_claim(db, entity_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert_claim(db, entity_id)

    async def test_a_later_vintage_of_the_same_fact_coexists(self, db):
        """A restatement shares event_date but has a new knowledge_date."""
        entity_id = await _entity(db)
        first = await _insert_claim(db, entity_id, knowledge_date=NOW)
        revised = await _insert_claim(
            db, entity_id, knowledge_date=NOW + timedelta(days=90)
        )
        assert first != revised

    async def test_two_users_may_hold_the_same_private_observation(self, db):
        entity_id = await _entity(db)
        common = {
            "source": "polygon",
            "redistributable": "byo_only",
            "credential_owner": "user",
        }
        a = await _insert_claim(db, entity_id, audience_user_id=uuid4(), **common)
        b = await _insert_claim(db, entity_id, audience_user_id=uuid4(), **common)
        assert a != b

    async def test_knowledge_cannot_precede_the_event(self, db):
        entity_id = await _entity(db)
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_claim(
                db, entity_id, event_date=NOW, knowledge_date=NOW - timedelta(days=1)
            )


class TestFillAttempt:
    async def _gap(self, db, entity_id):
        return await db.pool.fetchval(
            "INSERT INTO gap (entity_id, claim_type, gap_class, score) "
            "VALUES ($1, 'fundamental_metric', 'missing', 1.0) RETURNING id",
            entity_id,
        )

    async def test_unfillable_must_state_why(self, db):
        entity_id = await _entity(db)
        gap_id = await self._gap(db, entity_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await db.pool.execute(
                "INSERT INTO fill_attempt (gap_id, capability, outcome) "
                "VALUES ($1, 'edgar', 'unfillable')",
                gap_id,
            )

    async def test_unfillable_with_a_reason_is_accepted(self, db):
        entity_id = await _entity(db)
        gap_id = await self._gap(db, entity_id)
        await db.pool.execute(
            "INSERT INTO fill_attempt (gap_id, capability, outcome, reason) "
            "VALUES ($1, 'edgar', 'unfillable', 'no 10-K filed for this period')",
            gap_id,
        )
        assert await db.pool.fetchval("SELECT count(*) FROM fill_attempt") == 1

    async def test_a_filled_gap_must_point_at_a_claim(self, db):
        entity_id = await _entity(db)
        gap_id = await self._gap(db, entity_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await db.pool.execute(
                "INSERT INTO fill_attempt (gap_id, capability, outcome) "
                "VALUES ($1, 'edgar', 'filled')",
                gap_id,
            )


class TestGapQueue:
    async def test_only_one_open_gap_per_identity(self, db):
        entity_id = await _entity(db)
        sql = (
            "INSERT INTO gap (entity_id, claim_type, gap_class, score) "
            "VALUES ($1, 'price_snapshot', 'stale', 1.0)"
        )
        await db.pool.execute(sql, entity_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await db.pool.execute(sql, entity_id)

    async def test_a_resolved_gap_may_reopen(self, db):
        entity_id = await _entity(db)
        sql = (
            "INSERT INTO gap (entity_id, claim_type, gap_class, score) "
            "VALUES ($1, 'price_snapshot', 'stale', 1.0)"
        )
        await db.pool.execute(sql, entity_id)
        await db.pool.execute("UPDATE gap SET resolved_at = now()")
        await db.pool.execute(sql, entity_id)
        assert await db.pool.fetchval("SELECT count(*) FROM gap") == 2

    async def test_a_lease_needs_both_owner_and_expiry(self, db):
        entity_id = await _entity(db)
        gap_id = await db.pool.fetchval(
            "INSERT INTO gap (entity_id, claim_type, gap_class, score) "
            "VALUES ($1, 'price_snapshot', 'missing', 1.0) RETURNING id",
            entity_id,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await db.pool.execute(
                "UPDATE gap SET lease_owner = 'worker-1' WHERE id = $1", gap_id
            )


class TestPrediction:
    async def _predict(self, db, entity_id, **overrides):
        fields = {
            "method": "manipulation_signal",
            "direction": "up",
            "confidence": 0.7,
            "entry_price": 100,
            "upper_barrier": 110,
            "lower_barrier": 90,
            "horizon_ends_at": NOW + timedelta(days=5),
        }
        fields.update(overrides)
        return await db.pool.fetchval(
            """
            INSERT INTO prediction (entity_id, method, direction, confidence,
                                    entry_price, upper_barrier, lower_barrier,
                                    horizon_ends_at, provenance)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'{}'::jsonb) RETURNING id
            """,
            entity_id,
            fields["method"],
            fields["direction"],
            fields["confidence"],
            fields["entry_price"],
            fields["upper_barrier"],
            fields["lower_barrier"],
            fields["horizon_ends_at"],
        )

    async def test_barriers_must_straddle_entry(self, db):
        entity_id = await _entity(db)
        with pytest.raises(asyncpg.CheckViolationError):
            await self._predict(db, entity_id, upper_barrier=95)

    async def test_a_new_prediction_starts_pending_and_unresolved(self, db):
        entity_id = await _entity(db)
        await self._predict(db, entity_id)
        row = await db.pool.fetchrow("SELECT outcome, resolved_at FROM prediction")
        assert row["outcome"] == "pending"
        assert row["resolved_at"] is None

    async def test_resolving_without_a_timestamp_is_rejected(self, db):
        entity_id = await _entity(db)
        await self._predict(db, entity_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await db.pool.execute("UPDATE prediction SET outcome = 'upper'")

    async def test_calibration_counts_only_resolved_predictions(self, db):
        entity_id = await _entity(db)
        await self._predict(db, entity_id, confidence=0.75)
        assert await db.pool.fetchval("SELECT count(*) FROM calibration_bucket") == 0

        await db.pool.execute(
            "UPDATE prediction SET outcome = 'upper', resolved_at = now()"
        )
        row = await db.pool.fetchrow("SELECT * FROM calibration_bucket")
        assert row["n"] == 1
        assert row["hits"] == 1
        assert float(row["bucket_low"]) == pytest.approx(0.7)

    async def test_a_wrong_direction_does_not_count_as_a_hit(self, db):
        entity_id = await _entity(db)
        await self._predict(db, entity_id, direction="down")
        await db.pool.execute(
            "UPDATE prediction SET outcome = 'upper', resolved_at = now()"
        )
        row = await db.pool.fetchrow("SELECT n, hits FROM calibration_bucket")
        assert row["n"] == 1
        assert row["hits"] == 0
