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

from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from asyncpg import Pool

_REFRESH_CALIBRATION = "REFRESH MATERIALIZED VIEW CONCURRENTLY calibration_bucket"
_REFRESH_PAYOFF = "REFRESH MATERIALIZED VIEW CONCURRENTLY finding_payoff"

_THROTTLE = timedelta(minutes=10)
# Bounded: each entry retains one (closed) pool object so its id stays
# unambiguous for as long as the entry lives. The scheduler contributes one
# entry; a long test session contributes at most _MAX_TRACKED.
_MAX_TRACKED = 64
_last_refresh: OrderedDict[int, tuple[datetime, Pool]] = OrderedDict()


async def refresh_statistics(pool) -> None:
    """Refresh both views now, unconditionally."""
    await pool.execute(_REFRESH_CALIBRATION)
    await pool.execute(_REFRESH_PAYOFF)
    if isinstance(pool, Pool):
        key = id(pool)
        _last_refresh[key] = (datetime.now(UTC), pool)
        _last_refresh.move_to_end(key)
        while len(_last_refresh) > _MAX_TRACKED:
            _last_refresh.popitem(last=False)


async def refresh_statistics_if_due(pool) -> bool:
    """Refresh when this pool has not refreshed within the staleness bound.

    Returns whether a refresh ran. The throttle is per pool OBJECT, not per
    id(): CPython recycles ids after GC, and keying by bare id() let a fresh
    test pool inherit a dead pool's recent-refresh stamp and skip its refresh
    -- the test then read a stale calibration_bucket (decile counts from the
    previous test) and failed, only on machines whose GC happened to reuse
    the address within the throttle window (CI, observed 2026-08-26). The
    stored pool reference makes the identity check exact; it is retained
    (bounded above) so the id cannot be reused while the entry exists.
    """
    if not isinstance(pool, Pool):
        # Not a pool (a bare connection): always refresh. The only callers
        # pass pools; this path exists so a non-pool never silently inherits
        # a throttle stamp it cannot be identified by.
        await refresh_statistics(pool)
        return True
    key = id(pool)
    entry = _last_refresh.get(key)
    now = datetime.now(UTC)
    if entry is not None and entry[1] is pool and now - entry[0] < _THROTTLE:
        return False
    await refresh_statistics(pool)
    return True
