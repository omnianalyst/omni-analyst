"""Refresh the materialized statistics views.

The calibration buckets and finding payoff stats are materialized (migration
068) because every scheduler read of the live views aggregated the full
2.0M-row prediction table -- a constant 100% CPU on the shared box. The
resolve pass is the only writer of outcome fields, so refreshing once per
pass bounds staleness at one resolve interval: the honest granularity of a
calibration bucket anyway.

CONCURRENTLY diffs old against new without blocking readers, and therefore
cannot run inside a transaction -- callers invoke it after their write
transactions close.
"""

from __future__ import annotations

_REFRESH_CALIBRATION = "REFRESH MATERIALIZED VIEW CONCURRENTLY calibration_bucket"
_REFRESH_PAYOFF = "REFRESH MATERIALIZED VIEW CONCURRENTLY finding_payoff"


async def refresh_statistics(pool) -> None:
    await pool.execute(_REFRESH_CALIBRATION)
    await pool.execute(_REFRESH_PAYOFF)
