"""A derived claim must not launder restricted data into shared coverage.

The ingestion rule in 001 is not enough on its own: nothing there stops an
agent reading a byo_only price series, computing a signal from it, and writing
the signal as 'allowed'.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

NOW = datetime(2026, 7, 27, tzinfo=UTC)


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company', $1, $1) "
        "RETURNING id",
        symbol,
    )


async def _claim(db, entity_id, *, key="k", conn=None, **overrides):
    fields = {
        "claim_type": "fundamental_metric",
        "source": "sec_edgar",
        "confidence": 0.9,
        "redistributable": "allowed",
        "audience_user_id": None,
        "derivation": "ingested",
    }
    fields.update(overrides)
    executor = conn or db.pool
    return await executor.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id, derivation)
        VALUES ($1,$2,$3,'{}'::jsonb,$4,$5,$6,$7,$8,$9,$10)
        RETURNING id
        """,
        entity_id,
        fields["claim_type"],
        key,
        fields["source"],
        NOW - timedelta(days=1),
        NOW,
        fields["confidence"],
        fields["redistributable"],
        fields["audience_user_id"],
        fields["derivation"],
    )


BYO = {
    "source": "polygon",
    "redistributable": "byo_only",
    "claim_type": "price_snapshot",
}


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestPropagation:
    async def test_a_signal_derived_from_byo_data_cannot_be_shared(self, db):
        """The laundering path: private input, public output."""
        entity_id = await _entity(db)
        user = uuid4()
        private = await _claim(
            db, entity_id, key="bars", audience_user_id=user, **BYO
        )
        async with db.pool.acquire() as conn, conn.transaction():
            signal = await _claim(
                db,
                entity_id,
                key="manipulation",
                conn=conn,
                claim_type="manipulation_signal",
                source="internal",
                redistributable="allowed",
                derivation="derived",
            )
            with pytest.raises(asyncpg.CheckViolationError, match="byo_only"):
                await conn.execute(
                    "INSERT INTO claim_input VALUES ($1, $2)", signal, private
                )

    async def test_a_derived_claim_inherits_the_private_audience(self, db):
        entity_id = await _entity(db)
        owner, other = uuid4(), uuid4()
        private = await _claim(
            db, entity_id, key="bars", audience_user_id=owner, **BYO
        )
        async with db.pool.acquire() as conn, conn.transaction():
            wrong = await _claim(
                db,
                entity_id,
                key="signal",
                conn=conn,
                claim_type="manipulation_signal",
                source="internal",
                redistributable="byo_only",
                audience_user_id=other,
                derivation="derived",
            )
            with pytest.raises(asyncpg.CheckViolationError, match="private to"):
                await conn.execute(
                    "INSERT INTO claim_input VALUES ($1, $2)", wrong, private
                )

    async def test_the_correctly_scoped_derivation_is_accepted(self, db):
        entity_id = await _entity(db)
        owner = uuid4()
        private = await _claim(
            db, entity_id, key="bars", audience_user_id=owner, **BYO
        )
        async with db.pool.acquire() as conn, conn.transaction():
            signal = await _claim(
                db,
                entity_id,
                key="signal",
                conn=conn,
                claim_type="manipulation_signal",
                source="internal",
                redistributable="byo_only",
                audience_user_id=owner,
                derivation="derived",
            )
            await conn.execute(
                "INSERT INTO claim_input VALUES ($1, $2)", signal, private
            )
        assert await db.pool.fetchval("SELECT count(*) FROM claim_input") == 1

    async def test_a_derivation_from_public_inputs_stays_shareable(self, db):
        entity_id = await _entity(db)
        async with db.pool.acquire() as conn, conn.transaction():
            revenue = await _claim(db, entity_id, key="Revenues", conn=conn)
            growth = await _claim(
                db, entity_id, key="RevenueGrowth", conn=conn,
                source="internal", derivation="derived",
            )
            await conn.execute(
                "INSERT INTO claim_input VALUES ($1, $2)", growth, revenue
            )
        assert await db.pool.fetchval(
            "SELECT count(*) FROM shared_coverage"
        ) == 2

    async def test_the_most_restrictive_input_wins(self, db):
        """One private input among several public ones still restricts."""
        entity_id = await _entity(db)
        owner = uuid4()
        public = await _claim(db, entity_id, key="Revenues")
        private = await _claim(
            db, entity_id, key="bars", audience_user_id=owner, **BYO
        )
        async with db.pool.acquire() as conn, conn.transaction():
            blended = await _claim(
                db, entity_id, key="blend", conn=conn,
                source="internal", derivation="derived",
            )
            await conn.execute(
                "INSERT INTO claim_input VALUES ($1, $2)", blended, public
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO claim_input VALUES ($1, $2)", blended, private
                )


class TestUndeclaredProvenance:
    async def test_a_derived_claim_must_declare_its_inputs(self, db):
        """Otherwise the propagation rule is evaded by staying silent."""
        entity_id = await _entity(db)
        with pytest.raises(asyncpg.CheckViolationError, match="declares no inputs"):
            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    await _claim(
                        db, entity_id, key="orphan", conn=conn,
                        source="internal", derivation="derived",
                    )

    async def test_an_ingested_claim_needs_no_inputs(self, db):
        entity_id = await _entity(db)
        assert await _claim(db, entity_id, key="plain") is not None


class TestSharedCoverageView:
    async def test_private_claims_are_absent(self, db):
        entity_id = await _entity(db)
        await _claim(db, entity_id, key="public")
        await _claim(db, entity_id, key="bars", audience_user_id=uuid4(), **BYO)
        rows = await db.pool.fetch("SELECT key FROM shared_coverage")
        assert [r["key"] for r in rows] == ["public"]

    async def test_superseded_claims_are_absent(self, db):
        entity_id = await _entity(db)
        old = await _claim(db, entity_id, key="v1")
        new = await _claim(db, entity_id, key="v2")
        await db.pool.execute(
            "UPDATE claim SET superseded_by = $1 WHERE id = $2", new, old
        )
        rows = await db.pool.fetch("SELECT key FROM shared_coverage")
        assert [r["key"] for r in rows] == ["v2"]
