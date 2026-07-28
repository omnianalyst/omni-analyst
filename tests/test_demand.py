"""The demand ledger — what has been asked for, by whom, how urgently.

Demand is the input to the gap engine. These tests cover the behaviours the
ledger must guarantee: demand is recorded and read back; withdrawal
deactivates rather than deletes; rank aggregates across users and reports how
many distinct users asked; inactive demand drops out of every read path; and
the schema's positivity constraint on weight surfaces rather than being
swallowed.
"""

from uuid import uuid4

import pytest
from asyncpg.exceptions import CheckViolationError

from omni.demand.ledger import active_demand, direct_attention, rank, withdraw


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company', $1, $1) "
        "RETURNING id",
        symbol,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def test_recording_demand_reads_it_back(db):
    entity_id = await _entity(db)
    user = uuid4()

    demand_id = await direct_attention(
        db.pool,
        entity_id=entity_id,
        claim_type="price_snapshot",
        key="bars",
        requested_by=user,
        weight=2.5,
    )
    assert demand_id is not None

    rows = await active_demand(db.pool, entity_id=entity_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == demand_id
    assert row["entity_id"] == entity_id
    assert row["claim_type"] == "price_snapshot"
    assert row["key"] == "bars"
    assert row["requested_by"] == user
    assert row["weight"] == 2.5
    assert row["active"] is True
    assert row["channel"] == "direct"


async def test_withdraw_deactivates_without_deleting(db):
    entity_id = await _entity(db)
    demand_id = await direct_attention(
        db.pool, entity_id=entity_id, claim_type="price_snapshot", key="bars"
    )

    await withdraw(db.pool, demand_id)

    # Withdrawn demand drops out of the active view...
    assert await active_demand(db.pool, entity_id=entity_id) == []

    # ...but the row still exists in the ledger, now inactive.
    row = await db.pool.fetchrow(
        "SELECT active FROM demand WHERE id = $1", demand_id
    )
    assert row is not None, "withdrawn demand must remain in the ledger"
    assert row["active"] is False


async def test_rank_sums_weight_across_users_and_reports_requester_count(db):
    entity_id = await _entity(db)
    user_a, user_b = uuid4(), uuid4()
    await direct_attention(
        db.pool, entity_id=entity_id, claim_type="price_snapshot",
        key="bars", requested_by=user_a, weight=1.0,
    )
    await direct_attention(
        db.pool, entity_id=entity_id, claim_type="price_snapshot",
        key="bars", requested_by=user_b, weight=1.5,
    )

    rows = await rank(db.pool)
    assert len(rows) == 1
    row = rows[0]
    assert row["entity_id"] == entity_id
    assert row["claim_type"] == "price_snapshot"
    assert row["key"] == "bars"
    assert row["total_weight"] == 2.5
    assert row["requester_count"] == 2


async def test_rank_orders_a_heavily_demanded_target_above_a_light_one(db):
    heavy = await _entity(db, symbol="HEAVY")
    light = await _entity(db, symbol="LIGHT")
    await direct_attention(
        db.pool, entity_id=heavy, claim_type="price_snapshot",
        key="bars", weight=5.0,
    )
    await direct_attention(
        db.pool, entity_id=light, claim_type="price_snapshot",
        key="bars", weight=1.0,
    )

    rows = await rank(db.pool)
    assert [r["entity_id"] for r in rows] == [heavy, light]


async def test_inactive_demand_is_excluded_from_active_demand_and_rank(db):
    entity_id = await _entity(db)
    demand_id = await direct_attention(
        db.pool, entity_id=entity_id, claim_type="price_snapshot",
        key="bars", weight=3.0,
    )

    await withdraw(db.pool, demand_id)

    assert await active_demand(db.pool, entity_id=entity_id) == []
    assert await rank(db.pool) == []


async def test_a_non_positive_weight_is_rejected(db):
    """The schema enforces weight > 0; the violation must surface, not vanish."""
    entity_id = await _entity(db)
    with pytest.raises(CheckViolationError):
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="price_snapshot",
            key="bars", weight=0.0,
        )
    assert await db.pool.fetchval("SELECT count(*) FROM demand") == 0
