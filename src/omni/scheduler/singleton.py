"""Singleton ownership for the scheduler, enforced in PostgreSQL.

``deploy.replicas: 1`` is orchestration intent, not a distributed lock: a
second scheduler started by hand, a Kubernetes rolling update, or any other
entry point can still double the provider API spend for the same coverage.
This module makes the database the arbiter: a long-lived session advisory
lock whose holder IS the one scheduler. A second instance finds the lock
taken and refuses to run, loudly, instead of racing.
"""

from __future__ import annotations

import hashlib

SINGLETON_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"omni:scheduler:singleton").digest()[:8], "big", signed=True
)


class SchedulerSingletonLock:
    """Holds a dedicated pool connection for the life of the scheduler.

    The advisory lock is session-scoped: it lives exactly as long as the
    connection it was taken on, so holding one pooled connection open is the
    lease. ``release()`` (or the connection dying) ends the lease and lets a
    successor start.
    """

    def __init__(self, pool, conn) -> None:
        self._pool = pool
        self._conn = conn

    async def release(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            await self._conn.execute("SELECT pg_advisory_unlock($1)", SINGLETON_LOCK_KEY)
        with contextlib.suppress(Exception):
            await self._pool.release(self._conn)


async def acquire_scheduler_singleton(pool) -> SchedulerSingletonLock | None:
    """Take the singleton lease, or return None if another scheduler holds it.

    Uses the non-blocking try form on purpose: a second scheduler must refuse
    and exit with a clear log line, not queue up behind the first and appear
    hung.
    """
    conn = await pool.acquire()
    try:
        locked = await conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", SINGLETON_LOCK_KEY
        )
    except Exception:
        await pool.release(conn)
        raise
    if not locked:
        await pool.release(conn)
        return None
    return SchedulerSingletonLock(pool, conn)


__all__ = ["SINGLETON_LOCK_KEY", "SchedulerSingletonLock", "acquire_scheduler_singleton"]
