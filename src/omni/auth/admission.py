"""Atomic admission for credential endpoints (audit finding A01).

Throttling that checks a counter, verifies a password, and only then
records the outcome is not a limit under concurrency: N simultaneous
requests all observe the same admitted state before any of them records.
The 075 table made the state durable but kept the check-then-record shape.

Admission here is two committed database operations per request:

1. ``reserve_budgets`` -- a fixed-window request budget reserved by a single
   atomic upsert per key, over as many keys as the policy names (client IP,
   account fingerprint). Reservation is independent of outcome: a successful
   login does not refund the budget, so the budget cannot be laundered by
   interleaving valid accounts, and rotating either side of the durable
   (email, IP) key does not reset the other side's allowance.
2. ``credential_guard`` -- a transaction-scoped advisory lock on the
   (email, IP) pair, so the throttle check, password verification, and
   outcome recording for one pair are serialized: exactly one request at a
   time decides and records for a given pair, deployment-wide.

Budgets are fixed windows (the whole row expires at the window edge), not
sliding ones; that boundary behaviour is the documented trade for the
reservation being one cheap statement. The escalating cooldown in
``omni.auth.throttle`` remains the durable per-pair lockout; the in-memory
limiter in ``omni.auth.ratelimit`` remains the per-process burst guard.
Neither substitutes for the other.
"""

from __future__ import annotations

import hashlib
import math
from contextlib import asynccontextmanager
from datetime import timedelta

from neutron.error import AppError
from starlette.exceptions import HTTPException

from omni.auth.throttle import check_login_throttle, email_fingerprint, normalize_ip

_RESERVE = """
INSERT INTO auth_budget(key, used, expires_at)
VALUES ($1, 1, clock_timestamp() + $3::interval)
ON CONFLICT (key) DO UPDATE SET
    used = CASE WHEN auth_budget.expires_at <= clock_timestamp()
                THEN 1 ELSE auth_budget.used + 1 END,
    expires_at = CASE WHEN auth_budget.expires_at <= clock_timestamp()
                      THEN clock_timestamp() + $3::interval
                      ELSE auth_budget.expires_at END
WHERE auth_budget.expires_at <= clock_timestamp()
   OR auth_budget.used < $2
RETURNING used
"""

#: How long a credential request may wait for a pool connection. Admission
#: under load must fail loudly and quickly (503), not queue unbounded.
POOL_ACQUIRE_TIMEOUT = 2.0


def refuse(seconds: float = 60.0) -> HTTPException:
    """429 with a Retry-After, rendered as problem+json by the app."""
    return HTTPException(
        429,
        "Too many attempts; retry later.",
        headers={"Retry-After": str(max(1, math.ceil(seconds)))},
    )


def busy() -> AppError:
    return AppError(
        503, "unavailable", "Service Unavailable",
        "Authentication is busy; retry shortly.",
    )


async def reserve_budgets(pool, budgets) -> None:
    """Atomically reserve one unit from each named budget.

    ``budgets`` is an iterable of ``(key, maximum, window_seconds)``. Keys
    are sorted before reservation so concurrent requests touching the same
    set of keys lock budget rows in one consistent order and cannot
    deadlock against each other. A key whose budget is exhausted raises 429
    with the seconds left in its window; success commits the reservation
    whether the protected work later succeeds or fails.
    """
    items = sorted(budgets)
    async with pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn, conn.transaction():
        for key, maximum, seconds in items:
            if maximum < 1 or seconds <= 0:
                raise ValueError("invalid admission budget")
            row = await conn.fetchrow(
                _RESERVE, key, maximum, timedelta(seconds=seconds)
            )
            if row is None:
                retry = await conn.fetchval(
                    "SELECT greatest(1, extract(epoch FROM "
                    "(expires_at - clock_timestamp()))) "
                    "FROM auth_budget WHERE key = $1",
                    key,
                )
                raise refuse(float(retry or 1))


def _guard_key(fingerprint: str, ip: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"login:{fingerprint}:{ip}".encode()).digest()[:8],
        "big",
        signed=True,
    )


@asynccontextmanager
async def credential_guard(pool, *, email: str, ip: str):
    """Serialize admission for one (email, ip) pair and yield the connection.

    Reserves the deployment's IP and account budgets (settings
    ``omni_auth_ip_budget`` / ``omni_auth_account_budget``, one-minute fixed
    windows), then opens a transaction holding a transaction-scoped advisory
    lock on the pair. Inside the yielded transaction the caller verifies
    credentials and records the outcome; the commit happens when the context
    exits cleanly, so a recorded failure survives a 401 raised afterwards --
    the caller must raise authorization errors AFTER the guard exits, never
    inside it, or the failure record rolls back with the refusal.

    Pool exhaustion is a 503, never an anonymous-looking 401: an
    infrastructure failure must not masquerade as a bad password.
    """
    ip = normalize_ip(ip)
    fingerprint = email_fingerprint(email)
    from omni.config import settings

    try:
        await reserve_budgets(pool, [
            (f"credential:ip:{ip}", settings.omni_auth_ip_budget, 60),
            (
                f"credential:account:{fingerprint}",
                settings.omni_auth_account_budget,
                60,
            ),
        ])
    except TimeoutError as exc:
        # Pool exhaustion is infrastructure, not a bad password.
        raise busy() from exc

    acquire = pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT)
    try:
        conn = await acquire.__aenter__()
    except TimeoutError as exc:
        raise busy() from exc
    try:
        async with conn.transaction():
            locked = await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock($1)",
                _guard_key(fingerprint, ip),
            )
            if not locked:
                # Another request for the same pair is mid-verification.
                # Retry in a second rather than piling on: one serialized
                # argon2 verify does not take longer than that.
                raise refuse(1)
            admitted, retry_after = await check_login_throttle(
                conn, email=email, ip=ip
            )
            if not admitted:
                raise refuse(retry_after)
            yield conn
    finally:
        await acquire.__aexit__(None, None, None)


__all__ = ["busy", "credential_guard", "refuse", "reserve_budgets"]
