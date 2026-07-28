"""Audience visibility — the read-side half of the redistribution rule.

The schema stops a byo_only claim being written into shared coverage. This
stops one being read out of it. Both are needed: the write rule alone would
still let a careless SELECT serve one user's licensed data to another.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.coverage.visibility import visible_claims

NOW = datetime(2026, 7, 27, tzinfo=UTC)


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company', $1, $1) "
        "RETURNING id",
        symbol,
    )


async def _claim(db, entity_id, key, *, owner=None, claim_type="fundamental_metric"):
    shared = owner is None
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1,$2,$3,'{}'::jsonb,$4,$5,$6,0.9,$7,$8)
        RETURNING id
        """,
        entity_id,
        claim_type,
        key,
        "sec_edgar" if shared else "polygon",
        NOW - timedelta(days=1),
        NOW,
        "allowed" if shared else "byo_only",
        owner,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def test_a_user_sees_shared_coverage(db):
    entity_id = await _entity(db)
    await _claim(db, entity_id, "Revenues")
    rows = await visible_claims(db.pool, audience=uuid4())
    assert [r["key"] for r in rows] == ["Revenues"]


async def test_a_user_sees_their_own_private_claims(db):
    entity_id = await _entity(db)
    owner = uuid4()
    await _claim(db, entity_id, "bars", owner=owner, claim_type="price_snapshot")
    rows = await visible_claims(db.pool, audience=owner)
    assert [r["key"] for r in rows] == ["bars"]


async def test_a_user_never_sees_another_users_private_claims(db):
    """The leak this module exists to prevent."""
    entity_id = await _entity(db)
    await _claim(db, entity_id, "bars", owner=uuid4(), claim_type="price_snapshot")
    rows = await visible_claims(db.pool, audience=uuid4())
    assert rows == []


async def test_the_anonymous_audience_sees_only_shared_coverage(db):
    entity_id = await _entity(db)
    await _claim(db, entity_id, "Revenues")
    await _claim(db, entity_id, "bars", owner=uuid4(), claim_type="price_snapshot")
    rows = await visible_claims(db.pool, audience=None)
    assert [r["key"] for r in rows] == ["Revenues"]


async def test_private_and_shared_coexist_for_the_owner(db):
    entity_id = await _entity(db)
    owner = uuid4()
    await _claim(db, entity_id, "Revenues")
    await _claim(db, entity_id, "bars", owner=owner, claim_type="price_snapshot")
    rows = await visible_claims(db.pool, audience=owner)
    assert sorted(r["key"] for r in rows) == ["Revenues", "bars"]


async def test_superseded_claims_are_not_visible(db):
    entity_id = await _entity(db)
    old = await _claim(db, entity_id, "v1")
    new = await _claim(db, entity_id, "v2")
    await db.pool.execute(
        "UPDATE claim SET superseded_by = $1 WHERE id = $2", new, old
    )
    rows = await visible_claims(db.pool, audience=None)
    assert [r["key"] for r in rows] == ["v2"]


async def test_filters_compose_with_the_visibility_rule(db):
    """A filter must narrow the visible set, never widen it."""
    entity_id = await _entity(db)
    await _claim(db, entity_id, "Revenues")
    await _claim(db, entity_id, "bars", owner=uuid4(), claim_type="price_snapshot")

    rows = await visible_claims(
        db.pool, audience=None, claim_type="price_snapshot"
    )
    assert rows == [], "a claim_type filter must not bypass the audience rule"

    rows = await visible_claims(
        db.pool, audience=None, entity_id=entity_id, claim_type="fundamental_metric"
    )
    assert [r["key"] for r in rows] == ["Revenues"]


async def test_results_are_ordered_by_what_was_knowable_most_recently(db):
    entity_id = await _entity(db)
    await _claim(db, entity_id, "old")
    await db.pool.execute(
        "UPDATE claim SET event_date = $1, knowledge_date = $2 WHERE key = 'old'",
        NOW - timedelta(days=11),
        NOW - timedelta(days=10),
    )
    await _claim(db, entity_id, "new")
    rows = await visible_claims(db.pool, audience=None)
    assert [r["key"] for r in rows] == ["new", "old"]
