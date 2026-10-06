"""Scheduler singleton ownership in PostgreSQL (audit finding 7).

``replicas: 1`` is orchestration intent, not a distributed lock. A second
scheduler started by hand must refuse to run, and a stopped one must hand
the lease back.
"""

from __future__ import annotations

from omni.scheduler.singleton import (
    SINGLETON_LOCK_KEY,
    acquire_scheduler_singleton,
)


async def test_second_instance_refuses_ownership_while_the_first_holds_it(db):
    first = await acquire_scheduler_singleton(db.pool)
    assert first is not None

    second = await acquire_scheduler_singleton(db.pool)
    assert second is None, "a second scheduler acquired the singleton lease"

    await first.release()

    successor = await acquire_scheduler_singleton(db.pool)
    assert successor is not None, "release did not hand the lease back"
    await successor.release()


async def test_the_lease_is_an_advisory_lock_any_connection_can_see(db):
    first = await acquire_scheduler_singleton(db.pool)
    assert first is not None
    try:
        async with db.pool.acquire() as conn:
            got = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", SINGLETON_LOCK_KEY
            )
            assert got is False, "the lease is not visible across connections"
    finally:
        await first.release()


async def test_release_without_a_prior_acquire_is_harmless(db):
    # The lock row above is per-session; a stale release must not error the
    # shutdown path (pg_advisory_unlock on an unheld key returns false, it
    # does not raise).
    conn = await db.pool.acquire()
    from omni.scheduler.singleton import SchedulerSingletonLock

    stale = SchedulerSingletonLock(db.pool, conn)
    await stale.release()
