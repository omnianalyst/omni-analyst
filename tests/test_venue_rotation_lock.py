"""Venue token rotation is serialized across processes (audit finding A14).

``_user_lock`` is an asyncio lock in ONE process. The scheduler's reconcile
loop and the API's settings endpoints refresh the same user's venues from
two different processes, and Questrade refresh tokens are single-use: two
connects racing on the same stored token can invalidate each other's
rotation and leave a consumed-but-persisted credential. The cross-process
arbiter is a PostgreSQL session advisory lock per user; a caller that
cannot take it in bounded time gets an honest busy status instead of
racing.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from omni.db import connect
from omni.venue import manager
from omni.venue.manager import _rotation_lock, _rotation_lock_key


async def _clear_registry():
    manager._venues.clear()
    manager._locks.clear()
    manager._lock_refs.clear()


class TestRotationLock:
    async def test_two_pools_serialize_on_the_lock(self, db, database_url):
        # Two pools stand in for two processes (API + scheduler).
        other = await connect(database_url)
        try:
            user_id = uuid4()
            order: list[str] = []
            release = asyncio.Event()

            async def first_process():
                async with _rotation_lock(other.pool, user_id):
                    order.append("first")
                    await asyncio.wait_for(release.wait(), timeout=5)

            async def second_process():
                async with _rotation_lock(db.pool, user_id):
                    order.append("second")

            t1 = asyncio.create_task(first_process())
            await asyncio.sleep(0.3)
            t2 = asyncio.create_task(second_process())
            await asyncio.sleep(0.3)
            assert order == ["first"], (
                "the second process took the rotation lock while the first held it"
            )
            release.set()
            await asyncio.gather(t1, t2)
            assert order == ["first", "second"]
        finally:
            await other.close()

    async def test_different_users_do_not_serialize_behind_each_other(self, db):
        a, b = uuid4(), uuid4()
        assert _rotation_lock_key(a) != _rotation_lock_key(b)
        entered: list[uuid4] = []
        release = asyncio.Event()

        async def hold(user_id):
            async with _rotation_lock(db.pool, user_id):
                entered.append(user_id)
                if user_id == a:
                    await asyncio.wait_for(release.wait(), timeout=5)

        t1 = asyncio.create_task(hold(a))
        while a not in entered:
            await asyncio.sleep(0)
        t2 = asyncio.create_task(hold(b))
        await asyncio.sleep(0.2)
        assert b in entered, "one user's rotation blocked another user's"
        release.set()
        await asyncio.gather(t1, t2)

    async def test_refresh_reports_busy_instead_of_racing(self, db, monkeypatch):
        await _clear_registry()
        user_id = uuid4()
        await db.pool.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'x')",
            user_id,
            f"venue-{uuid4().hex[:8]}@example.com",
        )
        await db.pool.execute(
            """
            INSERT INTO user_settings (user_id, data) VALUES (
                $1,
                jsonb_build_object('venues', jsonb_build_object('questrade',
                    jsonb_build_object('enabled', true, 'credentials',
                        jsonb_build_object('refresh_token', 'enc:fake-token')))))
            """,
            user_id,
        )

        connects: list[str] = []

        async def _fake_connect(key, credentials, on_refresh_token=None):
            connects.append(key)
            return object()

        monkeypatch.setattr(manager, "_connect_venue", _fake_connect)
        monkeypatch.setattr(manager, "ROTATION_LOCK_TIMEOUT", 0.3)

        release = asyncio.Event()

        async def _hold_foreign_lock():
            async with _rotation_lock(db.pool, user_id):
                await asyncio.wait_for(release.wait(), timeout=5)

        holder = asyncio.create_task(_hold_foreign_lock())
        await asyncio.sleep(0.2)
        try:
            status = await manager.refresh_venues(db.pool, user_id)
        finally:
            release.set()
            await holder

        assert connects == [], "a refresh ran while another process held the lock"
        assert status.get("questrade", "").startswith(
            "error: another venue refresh"
        ), f"the busy refusal was not honest about why: {status}"

    async def test_concurrent_refreshes_in_one_process_never_overlap_the_connect(
        self, db, monkeypatch
    ):
        await _clear_registry()
        user_id = uuid4()
        await db.pool.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'x')",
            user_id,
            f"venue2-{uuid4().hex[:8]}@example.com",
        )
        await db.pool.execute(
            """
            INSERT INTO user_settings (user_id, data) VALUES (
                $1,
                jsonb_build_object('venues', jsonb_build_object('questrade',
                    jsonb_build_object('enabled', true, 'credentials',
                        jsonb_build_object('refresh_token', 'enc:fake-token')))))
            """,
            user_id,
        )

        inside = 0
        peak = 0

        async def _slow_connect(key, credentials, on_refresh_token=None):
            nonlocal inside, peak
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.1)
            inside -= 1
            return object()

        monkeypatch.setattr(manager, "_connect_venue", _slow_connect)
        monkeypatch.setattr(
            manager, "decrypt_fields", lambda blob, fields: dict(blob)
        )
        await asyncio.gather(
            manager.refresh_venues(db.pool, user_id),
            manager.refresh_venues(db.pool, user_id),
        )
        assert peak == 1, "two refreshes connected the same venue concurrently"
