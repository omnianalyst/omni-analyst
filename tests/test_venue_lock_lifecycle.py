"""Venue keyed-lock lifecycle (audit finding 10).

``_locks.setdefault(user_id, ...)`` created an entry on any refresh or
disconnect and only ``disconnect_user`` removed it, so users with no live
venue leaked a lock object per user forever. The registry must be empty
after reconciliation leaves a user with nothing connected.
"""

from __future__ import annotations

from uuid import uuid4

from omni.venue import manager


async def _clear_registry():
    manager._venues.clear()
    manager._locks.clear()
    manager._lock_refs.clear()


async def test_refresh_with_no_configured_venues_leaves_no_lock_behind(db):
    await _clear_registry()
    user_id = uuid4()
    # No user_settings row at all: the common "operator never touched venues"
    # path, previously a guaranteed leak.
    await manager.refresh_venues(db.pool, user_id)
    assert user_id not in manager._venues
    assert user_id not in manager._locks, "a lock leaked for a venue-less user"
    assert user_id not in manager._lock_refs


async def test_disconnect_venue_cleans_up_the_last_entry(db):
    await _clear_registry()
    user_id = uuid4()
    manager._venues[user_id] = {"questrade": object()}
    await manager.disconnect_venue(user_id, "questrade")
    assert user_id not in manager._venues
    assert user_id not in manager._locks


async def test_a_live_venue_keeps_its_lock_resident(db):
    await _clear_registry()
    user_id = uuid4()
    venue = _Closable()
    manager._venues[user_id] = {"questrade": venue}
    async with manager._user_lock(user_id):
        pass
    # Live venue: state (and its lock) must stay for the portfolio API.
    assert user_id in manager._venues
    assert user_id in manager._locks
    await manager.disconnect_user(user_id)
    assert venue.closed
    assert user_id not in manager._locks


async def test_sequential_reconciles_do_not_accumulate_entries(db):
    await _clear_registry()
    user_id = uuid4()
    for _ in range(3):
        await manager.refresh_venues(db.pool, user_id)
    assert manager._locks == {}
    assert manager._lock_refs == {}


class _Closable:
    closed = False

    async def aclose(self):
        self.closed = True
