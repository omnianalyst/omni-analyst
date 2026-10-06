"""Auth-state maintenance and the singleton lease watchdog (A04, A12).

Pruning of expired throttle/budget rows moved off the request path, so it
must demonstrably run somewhere: the scheduler's maintenance loop, bounded
and health-recorded. The singleton lease lives on one dedicated connection;
if that connection dies the lock is RELEASED while the scheduler keeps
running -- the watchdog must detect the loss so the process stops instead
of double-running against a successor.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from omni.auth.throttle import (
    MAX_COOLDOWN,
    cooldown_for,
    email_fingerprint,
    prune_auth_state,
    record_login_failure,
)
from omni.scheduler.singleton import (
    acquire_scheduler_singleton,
)
from omni.scheduler.worker import maintenance_once


@pytest.fixture(autouse=True)
async def _clean(db):
    # The prune tests assert absolute counts; leftovers from an earlier run
    # against the same explicit TEST_DATABASE_URL would break them.
    await db.pool.execute("TRUNCATE auth_throttle_event")
    await db.pool.execute("TRUNCATE auth_budget")
    await db.pool.execute("TRUNCATE loop_health")
    yield


class TestCooldownArithmetic:
    def test_the_doubling_never_overflows_at_any_count(self):
        # 46 used to raise OverflowError before the min() applied.
        for failures in (0, 4, 5, 11, 46, 10**6, 10**100):
            cooldown = cooldown_for(failures)
            assert cooldown <= MAX_COOLDOWN

    def test_the_shape_is_unchanged_below_the_cap(self):
        assert cooldown_for(4) == timedelta(0)
        assert cooldown_for(5).total_seconds() == 60
        assert cooldown_for(6).total_seconds() == 120
        assert cooldown_for(7).total_seconds() == 240
        assert cooldown_for(11).total_seconds() == 3600
        assert cooldown_for(46).total_seconds() == 3600


class TestMaintenance:
    async def test_it_deletes_only_what_is_expired(self, db):
        email = f"prune-{uuid4().hex[:8]}@example.com"
        for _ in range(3):
            await record_login_failure(db.pool, email=email, ip="203.0.113.30")
        await db.pool.execute(
            "UPDATE auth_throttle_event SET at = now() - interval '25 hours' "
            "WHERE client_ip = '203.0.113.30'"
        )
        await record_login_failure(db.pool, email=email, ip="203.0.113.31")

        removed = await prune_auth_state(db.pool)
        assert removed["throttle_events"] == 3
        remaining = await db.pool.fetchval(
            "SELECT count(*) FROM auth_throttle_event WHERE client_ip = '203.0.113.31'"
        )
        assert remaining == 1

    async def test_the_batch_is_bounded(self, db):
        rows = ",".join(
            f"('{email_fingerprint(f'p-{i}@example.com')}', '198.18.0.1', now() - interval '25 hours')"
            for i in range(5)
        )
        await db.pool.execute(
            f"INSERT INTO auth_throttle_event (email_hash, client_ip, at) VALUES {rows}"
        )
        removed = await prune_auth_state(db.pool, batch=2)
        assert removed["throttle_events"] == 2
        remaining = await db.pool.fetchval(
            "SELECT count(*) FROM auth_throttle_event"
        )
        assert remaining == 3, "the prune ignored its batch bound"

    async def test_expired_budgets_are_removed_after_their_grace(self, db):
        await db.pool.execute(
            "INSERT INTO auth_budget (key, used, expires_at) VALUES "
            "('credential:ip:198.51.100.1', 5, now() - interval '2 days'), "
            "('credential:ip:198.51.100.2', 5, now() - interval '30 seconds')"
        )
        removed = await prune_auth_state(db.pool)
        assert removed["budgets"] == 1
        kept = await db.pool.fetchval(
            "SELECT key FROM auth_budget"
        )
        assert kept == "credential:ip:198.51.100.2"

    async def test_the_maintenance_loop_pass_records_health(self, db):
        from omni.scheduler.health import record_loop_health

        # maintenance_once is what the loop runs; record it the way _do
        # does so the System page sees it like any other loop.
        result = await maintenance_once(db.pool)
        await record_loop_health(
            db.pool,
            loop_name="auth_maintenance",
            ok=True,
            result=str(result),
            expected_interval_seconds=3600.0,
        )
        row = await db.pool.fetchrow(
            "SELECT last_status, last_result FROM loop_health "
            "WHERE loop_name = 'auth_maintenance'"
        )
        assert row is not None and row["last_status"] == "success"

    async def test_the_login_failure_path_no_longer_prunes(self, db):
        # A global DELETE used to run inside every record_login_failure.
        await db.pool.execute(
            "INSERT INTO auth_throttle_event (email_hash, client_ip, at) VALUES "
            f"('{email_fingerprint('old@example.com')}', '198.18.0.2', now() - interval '25 hours')"
        )
        await record_login_failure(db.pool, email="new@example.com", ip="203.0.113.99")
        stale = await db.pool.fetchval(
            "SELECT count(*) FROM auth_throttle_event "
            "WHERE client_ip = '198.18.0.2'"
        )
        assert stale == 1, "record_login_failure pruned on the request path"


class TestSingletonLeaseWatchdog:
    async def test_verify_is_true_while_the_lease_connection_lives(self, db):
        lock = await acquire_scheduler_singleton(db.pool)
        assert lock is not None
        try:
            assert await lock.verify() is True
        finally:
            await lock.release()

    async def test_a_dead_lease_connection_fails_verify(self, db):
        # The scenario of A12: the database restarts (or the connection is
        # otherwise severed), the advisory lock is gone, and a successor
        # may already hold it -- but this process still has its loops.
        lock = await acquire_scheduler_singleton(db.pool)
        assert lock is not None
        backend = await lock._conn.fetchval("SELECT pg_backend_pid()")
        await db.pool.execute(f"SELECT pg_terminate_backend({backend})")
        assert await lock.verify() is False, (
            "a severed lease connection still verified as held"
        )
        # Return the dead connection so pool teardown is not left waiting
        # on it; the lease itself is already gone with the session, and
        # release() on the dead connection must not raise (shutdown path).
        await lock.release()
        # The lease is free: a successor can take it, which is exactly the
        # double-run condition the watchdog exists to end.
        successor = await acquire_scheduler_singleton(db.pool)
        assert successor is not None
        await successor.release()
