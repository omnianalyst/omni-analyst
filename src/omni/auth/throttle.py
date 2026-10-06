"""Durable, shared login throttling keyed by (email fingerprint, client IP).

The in-memory limiter in ``omni.auth.ratelimit`` stays as the per-process
burst guard (five attempts per minute per IP). This module is the
authoritative half -- state in PostgreSQL, visible to every replica,
surviving restarts -- and it escalates instead of resetting: a key's failure
rows form its current streak (a successful login deletes them), and once the
streak reaches MAX_ATTEMPTS the cooldown doubles with every further admitted
failure, capped at one hour. Only admitted attempts are recorded: a refused
attempt extends nothing, so hammering a locked door does not itself lengthen
the sentence, while the history it is judged from still contains every
attempt that got through.

The email is fingerprinted (sha256) rather than stored: a throttle log is
operational telemetry, not a roster of the deployment's addresses.

Elapsed time is measured on the database clock (``clock_timestamp()``), not
the API host's: an API replica whose clock drifts behind the database would
otherwise see a fresh streak as still hot, and one drifting ahead would
never lock anything. The decision runs inside the admission guard's
transaction (see ``omni.auth.admission``), so check-and-record is one
serialized unit per (email, IP) pair.

Pruning is NOT done on the request path: every failure used to run a global
age-based DELETE, an unbounded table scan's worth of work in the hottest
write path auth has. ``prune_auth_state`` does the bounded, batched version
and the scheduler's maintenance loop calls it on its own cadence.
"""

from __future__ import annotations

import hashlib
import ipaddress
from datetime import timedelta

MAX_ATTEMPTS = 5
BASE_COOLDOWN = timedelta(seconds=60.0)
MAX_COOLDOWN = timedelta(seconds=3600.0)
LOOKBACK = timedelta(hours=24.0)

#: Maintenance batch size for both prune statements. Bounded so a backlog
#: (a deployment that was down past several windows) costs one bounded batch
#: per pass, never one giant transaction.
PRUNE_BATCH = 1000


def normalize_ip(raw: str) -> str:
    """Canonical textual form, so IPv6 equivalents share one bucket.

    ``::1``, ``0:0:0:0:0:0:0:1`` and ``[::1]`` are the same client; without
    normalization they would be three throttle keys.
    """
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return raw


def email_fingerprint(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def cooldown_for(failures: int) -> timedelta:
    """Escalating lockout once the failure streak reaches MAX_ATTEMPTS.

    Five admitted failures lock the key for the base cooldown; every further
    admitted failure doubles it, capped at MAX_COOLDOWN. The doubling is
    clamped BEFORE the multiplication: ``BASE_COOLDOWN * 2**steps`` with an
    unbounded steps overflows timedelta for a streak of 46 (the count is
    attacker-influenced via imported state and concurrent admissions), and
    the cap makes every step past six produce the same answer anyway.
    """
    if failures < MAX_ATTEMPTS:
        return timedelta(0)
    steps = min(failures - MAX_ATTEMPTS, 6)
    return min(BASE_COOLDOWN * (1 << steps), MAX_COOLDOWN)


async def check_login_throttle(pool, *, email: str, ip: str) -> tuple[bool, float]:
    """Read-only admission decision for one (email, ip) key.

    Returns (admitted, retry_after_seconds). Never writes: recording is a
    separate, deliberate act by the endpoint after it knows the outcome, so a
    refused attempt cannot inflate the failure history it was judged from.
    Called inside ``credential_guard``'s transaction, ``pool`` is the guard's
    connection, which is what makes the check-then-record pair atomic.

    Elapsed-since-last-failure is computed by PostgreSQL against
    ``clock_timestamp()``: the stored ``at`` values were written by the
    database's clock, and comparing them to an API host's wall clock turns a
    skewed host into a silently wrong lockout.
    """
    row = await pool.fetchrow(
        """
        SELECT count(*) AS failures,
               extract(epoch FROM (clock_timestamp() - max(at))) AS elapsed
        FROM auth_throttle_event
        WHERE email_hash = $1
          AND client_ip = $2
          AND at > clock_timestamp() - $3::interval
        """,
        email_fingerprint(email),
        normalize_ip(ip),
        LOOKBACK,
    )
    if row is None or not row["failures"]:
        return True, 0.0

    remaining = cooldown_for(int(row["failures"])).total_seconds() - float(
        row["elapsed"] or 0.0
    )
    if remaining > 0:
        return False, remaining
    return True, 0.0


async def record_login_failure(pool, *, email: str, ip: str) -> None:
    await pool.execute(
        """
        INSERT INTO auth_throttle_event (email_hash, client_ip)
        VALUES ($1, $2)
        """,
        email_fingerprint(email),
        normalize_ip(ip),
    )


async def record_login_success(pool, *, email: str, ip: str) -> None:
    await pool.execute(
        "DELETE FROM auth_throttle_event WHERE email_hash = $1 AND client_ip = $2",
        email_fingerprint(email),
        normalize_ip(ip),
    )


async def prune_auth_state(pool, *, batch: int = PRUNE_BATCH) -> dict[str, int]:
    """Bounded, off-request-path cleanup of expired throttle state.

    Deletes at most ``batch`` failure rows older than the lookback and at
    most ``batch`` budget rows a day past expiry. Returns how many of each
    were removed. Age-leading indexes (077) serve both ORDER BY clauses, and
    FOR UPDATE SKIP LOCKED keeps a second maintenance runner from fighting
    this one over the same rows.

    A ``ctid`` is used within a single statement only: it is not a stable
    row identifier across statements, and nothing here persists it.
    """
    if batch < 1:
        raise ValueError("prune batch must be positive")
    events = await pool.fetch(
        f"""
        WITH old AS (
            SELECT ctid FROM auth_throttle_event
            WHERE at < clock_timestamp() - $1::interval
            ORDER BY at
            LIMIT {int(batch)}
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM auth_throttle_event e USING old WHERE e.ctid = old.ctid
        RETURNING 1
        """,
        LOOKBACK,
    )
    budgets = await pool.fetch(
        f"""
        WITH old AS (
            SELECT key FROM auth_budget
            WHERE expires_at < clock_timestamp() - interval '1 day'
            ORDER BY expires_at
            LIMIT {int(batch)}
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM auth_budget b USING old WHERE b.key = old.key
        RETURNING 1
        """,
    )
    return {
        "throttle_events": len(events),
        "budgets": len(budgets),
    }


__all__ = [
    "check_login_throttle",
    "cooldown_for",
    "email_fingerprint",
    "normalize_ip",
    "prune_auth_state",
    "record_login_failure",
    "record_login_success",
]
