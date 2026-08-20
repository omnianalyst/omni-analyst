"""Refresh the materialized statistics views.

The calibration buckets and finding payoff stats are materialized (migration
068) because every scheduler read of the live views aggregated the full
2.0M-row prediction table -- a constant 100% CPU on the shared box. The
resolve pass is the only writer of outcome fields, so refreshing there bounds
staleness for every reader.

CONCURRENTLY diffs old against new without blocking readers, and therefore
cannot run inside a transaction -- callers invoke it after their write
transactions close.

Frequency is throttled, and the lesson is on record: the first cut refreshed
on EVERY resolve tick. The scheduler resolves a handful of predictions a
minute, and a refresh is a full aggregate plus diff of the whole prediction
table -- so the cure ran 2M rows per minute and replaced one constant CPU
burn with another (measured 2026-08-20). A bucket's value moves by a
fraction of a resolution, so a staleness bound of minutes costs nothing
statistically. The throttle is keyed per pool: the scheduler's single
long-lived pool throttles in production, while each test's fresh pool
refreshes on first use so resolve-then-read tests stay deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from asyncpg import Pool

_REFRESH_CALIBRATION = "REFRESH MATERIALIZED VIEW CONCURRENTLY calibration_bucket"
_REFRESH_PAYOFF = "REFRESH MATERIALIZED VIEW CONCURRENTLY finding_payoff"

_THROTTLE = timedelta(minutes=10)
_last_refresh: dict[int, datetime] = {}


async def refresh_statistics(pool) -> None:
    """Refresh both views now, unconditionally."""
    await pool.execute(_REFRESH_CALIBRATION)
    await pool.execute(_REFRESH_PAYOFF)
    if isinstance(pool, Pool):
        _last_refresh[id(pool)] = datetime.now(UTC)


async def refresh_statistics_if_due(pool) -> bool:
    """Refresh when this pool has not refreshed within the staleness bound.

    Returns whether a refresh ran. The per-pool key means the production
    scheduler (one pool for its lifetime) throttles to one refresh per
    interval, while a test's newly created pool always refreshes on its first
    resolve -- resolve-then-read assertions stay deterministic without any
    test-only hook.
    """
    key = id(pool) if isinstance(pool, Pool) else 0
    last = _last_refresh.get(key)
    now = datetime.now(UTC)
    if last is not None and now - last < _THROTTLE:
        return False
    await refresh_statistics(pool)
    return True
