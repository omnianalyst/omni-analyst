"""Singleton ownership for the scheduler, enforced in PostgreSQL.

``deploy.replicas: 1`` is orchestration intent, not a distributed lock: a
second scheduler started by hand, a Kubernetes rolling update, or any other
entry point can still double the provider API spend for the same coverage.
This module makes the database the arbiter: a long-lived session advisory
lock whose holder IS the one scheduler. A second instance finds the lock
taken and refuses to run, loudly, instead of racing.

Session advisory locks live exactly as long as the connection they were
taken on. That is the lease -- and its weakness: if the dedicated connection
dies (a database restart, an idle-timeout, a network blip), the lease is
RELEASED while the scheduler keeps running on its pool. A successor can
then acquire the lock and double-run alongside the zombie. ``verify``
exists so the running scheduler can notice: a liveness probe on the lease
connection. A dead connection cannot silently hold a lease it no longer
has; the watchdog in ``omni.scheduler.__main__`` polls ``verify`` and stops
the scheduler the moment the lease connection is gone.
"""

from __future__ import annotations

import asyncio
import hashlib

SINGLETON_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"omni:scheduler:singleton").digest()[:8], "big", signed=True
)

#: How long a verify probe may take before the lease is treated as lost.
VERIFY_TIMEOUT = 5.0


class SingletonLeaseLost(Exception):
    """The connection holding the singleton lock is no longer usable."""


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

    async def verify(self) -> bool:
        """True if the lease connection is still alive and ours.

        A session advisory lock cannot be checked from its own session
        (``pg_try_advisory_lock`` is re-entrant and would succeed regardless),
        so the probe is the connection's own liveness: if the connection
        answers, the session -- and therefore the lock -- still exists. If
        it errors or times out, the lease must be assumed lost or stuck.
        """
        try:
            async with asyncio.timeout(VERIFY_TIMEOUT):
                return await self._conn.fetchval("SELECT 1") == 1
        except Exception:  # noqa: BLE001 - any probe failure means "not held"
            return False

    async def release(self) -> None:
        import contextlib

        # Try the graceful unlock first so the unlock is visible in the
        # session's own telemetry. Then TERMINATE the connection rather
        # than pool-release it: the lease is the connection, and handing a
        # dead one back to the pool would ask it to reset a socket that
        # will never answer -- the shutdown path would hang exactly when
        # it most needs to finish. Terminate is synchronous and performs
        # the holder cleanup; a healthy connection is closed by it, which
        # ends the advisory lock either way.
        with contextlib.suppress(Exception):
            await self._conn.execute("SELECT pg_advisory_unlock($1)", SINGLETON_LOCK_KEY)
        with contextlib.suppress(Exception):
            self._conn.terminate()
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


__all__ = [
    "SINGLETON_LOCK_KEY",
    "SchedulerSingletonLock",
    "SingletonLeaseLost",
    "acquire_scheduler_singleton",
]
