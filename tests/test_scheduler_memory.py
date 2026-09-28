"""The scheduler sweep must classify large histories without loading them into Python."""

import asyncio
import resource
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.capability.registry import Registry
from omni.coverage.gaps import detect_gaps
from omni.scheduler.worker import Scheduler, SchedulerConfig


@pytest.mark.asyncio
async def test_large_visible_history_is_classified_with_bounded_python_memory(db):
    entity = await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('macro', 'TEST_MACRO', 'Test') RETURNING id"
    )
    await db.pool.execute(
        "INSERT INTO demand (entity_id, claim_type, key, channel, weight, max_staleness) "
        "VALUES ($1, 'macro_series_point', 'DGS10', 'test', 1, interval '1 day')",
        entity,
    )
    await db.pool.execute(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source, event_date,
                           knowledge_date, confidence, redistributable)
        SELECT $1, 'macro_series_point', 'DGS10', '{"value": 4.2}'::jsonb,
               'fred', $2::timestamptz - n * interval '1 minute',
               $2::timestamptz - n * interval '1 minute', 1.0, 'allowed'
        FROM generate_series(1, 500000) AS n
        """,
        entity,
        datetime.now(UTC) - timedelta(days=2),
    )

    rss_unit = 1 if sys.platform == "darwin" else 1024
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_unit
    gaps = await detect_gaps(db.pool)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_unit

    assert {g["gap_class"] for g in gaps} == {"stale", "unverified"}
    assert after - before < 128 * 1024 * 1024

    scheduler = Scheduler(
        db.pool,
        Registry(),
        SchedulerConfig(fill_workers=0, sweep_interval=3600, resolve_interval=3600,
                        predict_interval=3600, surface_interval=3600, alerts_interval=3600),
    )
    await asyncio.wait_for(scheduler.start(), timeout=30)
    assert scheduler.stats.sweeps == 1
    await asyncio.wait_for(scheduler.stop(), timeout=5)
    assert scheduler._tasks == []
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_unit < 1024 * 1024 * 1024


@pytest.mark.asyncio
async def test_summary_preserves_audience_and_contradiction_rules(db):
    entity = await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company', 'TEST', 'Test') RETURNING id"
    )
    owner = uuid4()
    other = uuid4()
    for audience in (owner, other):
        await db.pool.execute(
            "INSERT INTO demand (entity_id, claim_type, key, channel, requested_by, weight) "
            "VALUES ($1, 'fundamental_metric', 'Revenues', 'test', $2, 1)",
            entity,
            audience,
        )
    observed_at = datetime.now(UTC)
    for source, amount, audience, redistribution in (
        ("sec_edgar", 100, None, "allowed"),
        ("owner_private", 200, owner, "byo_only"),
    ):
        await db.pool.execute(
            """
            INSERT INTO claim (entity_id, claim_type, key, value, source, event_date,
                               knowledge_date, confidence, redistributable, audience_user_id)
            VALUES ($1, 'fundamental_metric', 'Revenues', $2::jsonb, $3, $4, $4, 0.9, $5, $6)
            """,
            entity,
            '{"amount": ' + str(amount) + '}',
            source,
            observed_at,
            redistribution,
            audience,
        )

    gaps = await detect_gaps(db.pool)
    owner_classes = {g["gap_class"] for g in gaps if g["audience_user_id"] == owner}
    other_classes = {g["gap_class"] for g in gaps if g["audience_user_id"] == other}
    assert owner_classes == {"contradictory"}
    assert other_classes == {"unverified"}
    conflict = next(
        g for g in gaps if g["gap_class"] == "contradictory" and g["entity_id"] == entity
    )
    assert conflict["detail"]["conflicts"][0]["sources"] == ["owner_private", "sec_edgar"]
    assert conflict["detail"]["conflicts"][0]["values"] == [
        {"amount": 100}, {"amount": 200}
    ]
    assert conflict["detail"]["conflicts_truncated"] is False


@pytest.mark.asyncio
async def test_conflict_details_are_bounded(db):
    entity = await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('macro', 'CONFLICT', 'Conflict') RETURNING id"
    )
    await db.pool.execute(
        "INSERT INTO demand (entity_id, claim_type, key, channel, weight) "
        "VALUES ($1, 'macro_series_point', 'DGS10', 'test', 1)",
        entity,
    )
    await db.pool.execute(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source, event_date,
                           knowledge_date, confidence, redistributable)
        SELECT $1, 'macro_series_point', 'DGS10',
               jsonb_build_object('value', source_number),
               'source_' || source_number,
               $2::timestamptz - day_number * interval '1 day',
               $2::timestamptz - day_number * interval '1 day', 1, 'allowed'
        FROM generate_series(1, 40) AS day_number
        CROSS JOIN generate_series(1, 2) AS source_number
        """,
        entity,
        datetime.now(UTC),
    )

    gaps = await detect_gaps(db.pool)
    conflict = next(
        g for g in gaps if g["gap_class"] == "contradictory" and g["entity_id"] == entity
    )
    assert len(conflict["detail"]["conflicts"]) == 32
    assert conflict["detail"]["conflicts_truncated"] is True
