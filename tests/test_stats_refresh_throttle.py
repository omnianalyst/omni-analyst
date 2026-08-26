"""The materialized-view refresh throttle is per pool OBJECT, not per id().

2026-08-26: keying the throttle by bare id(pool) let a fresh test pool inherit
a dead pool's recent-refresh stamp (CPython recycles ids after GC), skip its
refresh, and read a stale calibration_bucket -- the calibration-quality test
failed on CI and never locally, because the failure needs an address reuse
inside the ten-minute throttle window. These tests pin the identity contract
directly, without needing to win that GC lottery.

The refresh SQL itself is stubbed: the matviews only exist after migrations,
and the defect under test is the refresh DECISION, not the SQL.
"""

import os
from datetime import UTC, datetime

import asyncpg

from omni.conviction import stats_refresh
from omni.conviction.stats_refresh import refresh_statistics_if_due

_refreshes: list[object] = []


async def _fake_refresh(pool) -> None:
    """Records the call and applies the same stamp the real writer does, so
    the decision logic under test sees an authentic throttle state."""
    _refreshes.append(pool)
    stats_refresh._last_refresh[id(pool)] = (datetime.now(UTC), pool)
    stats_refresh._last_refresh.move_to_end(id(pool))


async def _pool() -> asyncpg.Pool:
    url = os.environ["TEST_DATABASE_URL"]
    return await asyncpg.create_pool(url, min_size=1)


async def test_a_live_pool_is_throttled_only_by_its_own_refresh(monkeypatch):
    monkeypatch.setattr(stats_refresh, "refresh_statistics", _fake_refresh)
    pool = await _pool()
    stats_refresh._last_refresh.clear()
    _refreshes.clear()
    try:
        assert await refresh_statistics_if_due(pool) is True
        assert await refresh_statistics_if_due(pool) is False  # inside window
        assert _refreshes == [pool]
    finally:
        stats_refresh._last_refresh.clear()
        await pool.close()


async def test_a_recycled_id_never_inherits_a_dead_pools_stamp(monkeypatch):
    """The exact CI failure: an entry exists under this id, written by a
    DIFFERENT (now dead) pool moments ago. The live pool must refresh --
    under the old id()-keyed dict it skipped and read stale aggregates."""
    monkeypatch.setattr(stats_refresh, "refresh_statistics", _fake_refresh)
    live = await _pool()
    stats_refresh._last_refresh.clear()
    _refreshes.clear()
    stats_refresh._last_refresh[id(live)] = (datetime.now(UTC), object())
    try:
        assert await refresh_statistics_if_due(live) is True
        assert _refreshes == [live]
    finally:
        stats_refresh._last_refresh.clear()
        await live.close()


async def test_the_stamp_tracks_the_pool_not_the_address(monkeypatch):
    monkeypatch.setattr(stats_refresh, "refresh_statistics", _fake_refresh)
    pool = await _pool()
    stats_refresh._last_refresh.clear()
    await refresh_statistics_if_due(pool)
    try:
        entry = stats_refresh._last_refresh[id(pool)]
        assert entry[1] is pool  # identity, not a timestamp at this address
    finally:
        stats_refresh._last_refresh.clear()
        await pool.close()
