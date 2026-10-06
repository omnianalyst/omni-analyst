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
"""

from __future__ import annotations

import hashlib
import ipaddress
from datetime import timedelta

MAX_ATTEMPTS = 5
BASE_COOLDOWN = timedelta(seconds=60.0)
MAX_COOLDOWN = timedelta(seconds=3600.0)
LOOKBACK = timedelta(hours=24.0)


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
    admitted failure doubles it. The sixth attempt against a five-failure
    streak is the first one refused.
    """
    if failures < MAX_ATTEMPTS:
        return timedelta(0)
    steps = failures - MAX_ATTEMPTS
    doubled = BASE_COOLDOWN * (2**steps)
    return min(doubled, MAX_COOLDOWN)


async def check_login_throttle(pool, *, email: str, ip: str) -> tuple[bool, float]:
    """Read-only admission decision for one (email, ip) key.

    Returns (admitted, retry_after_seconds). Never writes: recording is a
    separate, deliberate act by the endpoint after it knows the outcome, so a
    refused attempt cannot inflate the failure history it was judged from.
    """
    row = await pool.fetchrow(
        """
        SELECT count(*) AS failures, max(at) AS last_at
        FROM auth_throttle_event
        WHERE email_hash = $1
          AND client_ip = $2
          AND at > now() - $3::interval
        """,
        email_fingerprint(email),
        normalize_ip(ip),
        LOOKBACK,
    )
    if row is None or not row["failures"]:
        return True, 0.0

    remaining = cooldown_for(int(row["failures"])).total_seconds() - _seconds_since(
        row["last_at"]
    )
    if remaining > 0:
        return False, remaining
    return True, 0.0


def _seconds_since(timestamp) -> float:
    from datetime import UTC, datetime

    if timestamp is None:
        return float("inf")
    return (datetime.now(UTC) - timestamp).total_seconds()


async def record_login_failure(pool, *, email: str, ip: str) -> None:
    await pool.execute(
        """
        INSERT INTO auth_throttle_event (email_hash, client_ip)
        VALUES ($1, $2)
        """,
        email_fingerprint(email),
        normalize_ip(ip),
    )
    # Bounded: the lookback window is 24h, and rows older than that can no
    # longer influence a decision, so dropping them here keeps the table flat
    # without a separate maintenance job.
    await pool.execute(
        "DELETE FROM auth_throttle_event WHERE at < now() - $1::interval",
        LOOKBACK,
    )


async def record_login_success(pool, *, email: str, ip: str) -> None:
    await pool.execute(
        "DELETE FROM auth_throttle_event WHERE email_hash = $1 AND client_ip = $2",
        email_fingerprint(email),
        normalize_ip(ip),
    )


__all__ = [
    "check_login_throttle",
    "cooldown_for",
    "email_fingerprint",
    "normalize_ip",
    "record_login_failure",
    "record_login_success",
]
