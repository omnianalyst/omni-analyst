"""Watchlists as the implicit demand channel.

A watchlist is a second inlet to the demand ledger: adding an entity raises
demand for a sensible default set of claim types for its kind, and the gap
engine consumes that demand exactly as it consumes demand raised by a question.
These tests hold the behaviours that make that true -- claim types vary by
kind, two watchers are two rows whose weight rank sums, removal deactivates
without deleting, a list is private to its owner and invisible to anonymous
callers, and a watched-but-uncovered entity produces a gap with no question
ever asked.
"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from neutron.auth.jwt import create_token
from neutron.test import TestClient

from omni.alerts.rules import evaluate
from omni.api.watchlist import build_router
from omni.coverage.gaps import detect_gaps, persist_gaps
from omni.demand.ledger import direct_attention, rank
from omni.main import create_app
from omni.watchlist import lists as wl
from omni.watchlist.lists import add_entity, delete_list as delete_list_fn


async def _user(db, email) -> uuid4:
    return await db.pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id",
        email,
    )


async def _entity(db, kind="company", symbol="AAPL", name=None) -> uuid4:
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ($1, $2, $3) RETURNING id",
        kind,
        symbol,
        name or symbol,
    )


async def _active_claim_types(db, entity_id) -> dict[str, int]:
    rows = await db.pool.fetch(
        "SELECT claim_type::text AS claim_type, count(*)::int AS n "
        "FROM demand WHERE entity_id = $1 AND active GROUP BY claim_type",
        entity_id,
    )
    return {r["claim_type"]: r["n"] for r in rows}


class _Lifespan:
    """Drive the ASGI lifespan protocol, which httpx's ASGITransport skips."""

    def __init__(self, app):
        self._app = app
        self._receive = asyncio.Queue()
        self._send = asyncio.Queue()
        self._task = None

    async def __aenter__(self):
        self._task = asyncio.create_task(
            self._app({"type": "lifespan"}, self._receive.get, self._send.put)
        )
        await self._receive.put({"type": "lifespan.startup"})
        message = await self._send.get()
        assert message["type"] == "lifespan.startup.complete", message
        return self._app

    async def __aexit__(self, *exc):
        await self._receive.put({"type": "lifespan.shutdown"})
        await self._send.get()
        await self._task


def _make_app(database_url):
    app = create_app(database_url)
    app.include_router(build_router(app))
    return app


def _token(user_id) -> dict:
    os.environ.setdefault("OMNI_JWT_SECRET", "t" * 48)
    token = create_token({"sub": str(user_id)}, os.environ["OMNI_JWT_SECRET"])
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, users CASCADE")
    yield


class TestAddingRaisesDemand:
    async def test_a_company_raises_price_and_fundamentals(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db, "company", "AAPL")
        wl_id = (await wl.create(db.pool, user_id=user, name="mine"))["id"]

        entry = await wl.add_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=user
        )
        assert entry is not None

        types = await _active_claim_types(db, entity)
        assert set(types) == {"price_snapshot", "fundamental_metric"}

    async def test_a_crypto_asset_raises_price_and_onchain(self, db):
        user = await _user(db, "b@example.com")
        entity = await _entity(db, "crypto_asset", "ETH", "Ethereum")
        wl_id = (await wl.create(db.pool, user_id=user, name="crypto"))["id"]

        await wl.add_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=user
        )

        types = await _active_claim_types(db, entity)
        assert set(types) == {"price_snapshot", "onchain_supply", "onchain_flow"}
        # The split is by kind, not a single list for everything: a token does
        # not demand fundamentals, and a stock does not demand on-chain flows.
        assert "fundamental_metric" not in types

    async def test_demand_is_keyless_and_owned_by_the_watcher(self, db):
        user = await _user(db, "c@example.com")
        entity = await _entity(db, "company", "MSFT")
        wl_id = (await wl.create(db.pool, user_id=user, name="x"))["id"]
        await wl.add_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=user
        )

        row = await db.pool.fetchrow(
            "SELECT key, requested_by, channel FROM demand WHERE entity_id = $1",
            entity,
        )
        assert row["key"] is None
        assert row["requested_by"] == user
        assert row["channel"] == "direct"

    async def test_adding_twice_does_not_double_demand(self, db):
        """Idempotent add: a second add must not raise demand again, because two
        rows for the same (entity, type, owner, key) inflate weight without
        adding signal -- the deduplication the ledger forbids on write."""
        user = await _user(db, "d@example.com")
        entity = await _entity(db, "company", "NVDA")
        wl_id = (await wl.create(db.pool, user_id=user, name="x"))["id"]

        await wl.add_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=user
        )
        await wl.add_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=user
        )

        assert await db.pool.fetchval("SELECT count(*) FROM demand") == 2


class TestSharedDemand:
    async def test_two_watchers_produce_two_rows_that_rank_sums(self, db):
        entity = await _entity(db, "company", "AAPL")
        user_a = await _user(db, "a@example.com")
        user_b = await _user(db, "b@example.com")
        wl_a = (await wl.create(db.pool, user_id=user_a, name="a"))["id"]
        wl_b = (await wl.create(db.pool, user_id=user_b, name="b"))["id"]

        await wl.add_entity(
            db.pool, watchlist_id=wl_a, entity_id=entity, user_id=user_a
        )
        await wl.add_entity(
            db.pool, watchlist_id=wl_b, entity_id=entity, user_id=user_b
        )

        # Two demand rows for the same claim type -- not deduplicated.
        price_rows = await db.pool.fetchval(
            "SELECT count(*) FROM demand "
            "WHERE entity_id = $1 AND claim_type = 'price_snapshot'",
            entity,
        )
        assert price_rows == 2

        ranked = {
            (r["entity_id"], str(r["claim_type"]), r["key"]): r
            for r in await rank(db.pool)
        }
        row = ranked[(entity, "price_snapshot", None)]
        assert row["requester_count"] == 2
        assert row["total_weight"] == 2.0


class TestRemoval:
    async def test_remove_deactivates_demand_without_deleting_it(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db, "company", "AAPL")
        wl_id = (await wl.create(db.pool, user_id=user, name="mine"))["id"]
        await wl.add_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=user
        )

        total_before = await db.pool.fetchval("SELECT count(*) FROM demand")
        assert total_before == 2
        assert await _active_claim_types(db, entity) != {}

        ok = await wl.remove_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=user
        )
        assert ok is True

        # Active demand is gone ...
        assert await _active_claim_types(db, entity) == {}
        # ... but the rows remain in the ledger, now inactive.
        total_after = await db.pool.fetchval("SELECT count(*) FROM demand")
        assert total_after == 2
        inactive = await db.pool.fetchval(
            "SELECT count(*) FROM demand WHERE active = false"
        )
        assert inactive == 2
        # And the entry itself is gone.
        assert (
            await db.pool.fetchval(
                "SELECT count(*) FROM watchlist_entry WHERE watchlist_id = $1",
                wl_id,
            )
            == 0
        )

    async def test_remove_drops_out_of_rank(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db, "company", "AAPL")
        wl_id = (await wl.create(db.pool, user_id=user, name="mine"))["id"]
        await wl.add_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=user
        )

        await wl.remove_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=user
        )

        assert await rank(db.pool) == []

    async def test_removing_one_list_preserves_the_same_entity_on_another_list(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db, "company", "AAPL")
        first = (await wl.create(db.pool, user_id=user, name="first"))["id"]
        second = (await wl.create(db.pool, user_id=user, name="second"))["id"]
        await wl.add_entity(
            db.pool, watchlist_id=first, entity_id=entity, user_id=user
        )
        await wl.add_entity(
            db.pool, watchlist_id=second, entity_id=entity, user_id=user
        )

        first_ids = {
            row["demand_id"]
            for row in await db.pool.fetch(
                "SELECT demand_id FROM watchlist_entry_demand WHERE watchlist_id = $1",
                first,
            )
        }
        second_ids = {
            row["demand_id"]
            for row in await db.pool.fetch(
                "SELECT demand_id FROM watchlist_entry_demand WHERE watchlist_id = $1",
                second,
            )
        }
        assert len(first_ids) == 2
        assert len(second_ids) == 2
        assert first_ids.isdisjoint(second_ids)

        assert await wl.remove_entity(
            db.pool, watchlist_id=first, entity_id=entity, user_id=user
        )

        active_ids = {
            row["id"]
            for row in await db.pool.fetch(
                "SELECT id FROM demand WHERE entity_id = $1 AND active", entity
            )
        }
        inactive_ids = {
            row["id"]
            for row in await db.pool.fetch(
                "SELECT id FROM demand WHERE entity_id = $1 AND NOT active", entity
            )
        }
        assert active_ids == second_ids
        assert inactive_ids == first_ids

    async def test_remove_preserves_direct_and_alert_created_demand(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db, "company", "AAPL")
        wl_id = (await wl.create(db.pool, user_id=user, name="mine"))["id"]
        await wl.add_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=user
        )
        watchlist_ids = {
            row["demand_id"]
            for row in await db.pool.fetch(
                "SELECT demand_id FROM watchlist_entry_demand WHERE watchlist_id = $1",
                wl_id,
            )
        }
        direct_id = await direct_attention(
            db.pool,
            entity_id=entity,
            claim_type="fundamental_metric",
            requested_by=user,
        )

        now = datetime.now(UTC)
        claim_id = await db.pool.fetchval(
            "INSERT INTO claim "
            "(entity_id, claim_type, key, value, source, event_date, knowledge_date, "
            "confidence, redistributable) "
            "VALUES ($1, 'price_snapshot', 'close', $2::jsonb, 'test', $3, $4, "
            "0.9, 'allowed') RETURNING id",
            entity,
            json.dumps({"value": 150}),
            now - timedelta(days=1),
            now,
        )
        alert = await db.pool.fetchrow(
            "INSERT INTO alert (user_id, entity_id, claim_type, condition) "
            "VALUES ($1, $2, 'price_snapshot', $3::jsonb) RETURNING *",
            user,
            entity,
            json.dumps({"kind": "value_above", "threshold": 100}),
        )
        fired = await evaluate(db.pool, alert, audience=user)
        assert [row["id"] for row in fired] == [claim_id]

        all_ids = {
            row["id"]
            for row in await db.pool.fetch(
                "SELECT id FROM demand WHERE entity_id = $1", entity
            )
        }
        alert_ids = all_ids - watchlist_ids - {direct_id}
        assert len(alert_ids) == 1

        assert await wl.remove_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=user
        )

        active_ids = {
            row["id"]
            for row in await db.pool.fetch(
                "SELECT id FROM demand WHERE entity_id = $1 AND active", entity
            )
        }
        inactive_ids = {
            row["id"]
            for row in await db.pool.fetch(
                "SELECT id FROM demand WHERE entity_id = $1 AND NOT active", entity
            )
        }
        assert active_ids == {direct_id} | alert_ids
        assert inactive_ids == watchlist_ids


class TestVisibility:
    async def test_another_user_cannot_see_or_touch_a_watchlist(self, db):
        owner = await _user(db, "owner@example.com")
        other = await _user(db, "other@example.com")
        entity = await _entity(db, "company", "AAPL")
        wl_id = (await wl.create(db.pool, user_id=owner, name="mine"))["id"]
        await wl.add_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=owner
        )

        # Not in the other user's list of watchlists.
        assert await wl.lists_for_user(db.pool, user_id=other) == []
        # Entries are invisible: returns None, not the owner's rows.
        assert await wl.entries(db.pool, watchlist_id=wl_id, user_id=other) is None
        # The other user cannot add to a list they do not own.
        assert (
            await wl.add_entity(
                db.pool, watchlist_id=wl_id, entity_id=entity, user_id=other
            )
            is None
        )
        # Nor remove from it.
        assert (
            await wl.remove_entity(
                db.pool, watchlist_id=wl_id, entity_id=entity, user_id=other
            )
            is False
        )

    async def test_an_anonymous_caller_is_refused_at_every_endpoint(
        self, db, database_url
    ):
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            post = await client.post("/watchlists", json={"name": "x"})
            get = await client.get("/watchlists")
            entries = await client.get(
                f"/watchlists/{uuid4()}/entries"
            )

        assert post.status_code == 401
        assert get.status_code == 401
        assert entries.status_code == 401

    async def test_an_authenticated_caller_can_create_their_list(
        self, db, database_url
    ):
        user = await _user(db, "auth@example.com")
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post("/watchlists", json={"name": "mine"}, headers=_token(user))

        assert r.status_code == 201, r.text
        assert r.json()["name"] == "mine"


class TestImplicitDemandReachesTheGapEngine:
    async def test_a_watched_uncovered_entity_raises_a_gap_with_no_question(
        self, db
    ):
        user = await _user(db, "a@example.com")
        entity = await _entity(db, "company", "AAPL")
        wl_id = (await wl.create(db.pool, user_id=user, name="mine"))["id"]

        # No question is ever asked about AAPL. The only demand is the
        # watchlist's.
        await wl.add_entity(
            db.pool, watchlist_id=wl_id, entity_id=entity, user_id=user
        )
        assert await db.pool.fetchval(
            "SELECT count(*) FROM demand WHERE requested_by IS NULL"
        ) == 0

        gaps = await detect_gaps(db.pool)
        missing = [
            g
            for g in gaps
            if g["entity_id"] == entity and g["gap_class"] == "missing"
        ]
        assert missing, "a watched-but-uncovered entity must raise a gap"
        assert {g["claim_type"] for g in missing} == {
            "price_snapshot",
            "fundamental_metric",
        }

        written = await persist_gaps(db.pool, gaps)
        assert written >= len(missing)
        assert await db.pool.fetchval(
            "SELECT count(*) FROM gap "
            "WHERE entity_id = $1 AND gap_class = 'missing' AND resolved_at IS NULL",
            entity,
        ) == len(missing)


class TestDeleteList:
    async def test_delete_removes_entries_and_withdraws_demand(self, db):
        user = await db.pool.fetchval(
            "INSERT INTO users (email, password_hash) "
            "VALUES ($1, 'x') RETURNING id", "del@example.com"
        )
        entity = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('company', 'AAPL', 'Apple') RETURNING id"
        )
        created = await wl.create(db.pool, user_id=user, name="to delete")
        await add_entity(
            db.pool, watchlist_id=created["id"], entity_id=entity, user_id=user
        )
        demand_ids = [
            r["demand_id"]
            for r in await db.pool.fetch(
                "SELECT demand_id FROM watchlist_entry_demand "
                "WHERE watchlist_id = $1", created["id"],
            )
        ]
        assert demand_ids, "entry raised demand"

        ok = await delete_list_fn(db.pool, watchlist_id=created["id"], user_id=user)
        assert ok is True
        assert await db.pool.fetchval(
            "SELECT count(*) FROM watchlist WHERE id = $1", created["id"]
        ) == 0
        assert await db.pool.fetchval(
            "SELECT count(*) FROM watchlist_entry WHERE watchlist_id = $1",
            created["id"],
        ) == 0
        # The demand rows are withdrawn (inactive), not left standing.
        for did in demand_ids:
            active = await db.pool.fetchval(
                "SELECT active FROM demand WHERE id = $1", did
            )
            assert active is False

    async def test_delete_another_users_list_is_false_not_deleted(self, db):
        owner = await db.pool.fetchval(
            "INSERT INTO users (email, password_hash) "
            "VALUES ($1, 'x') RETURNING id", "owner@example.com"
        )
        other = await db.pool.fetchval(
            "INSERT INTO users (email, password_hash) "
            "VALUES ($1, 'x') RETURNING id", "other@example.com"
        )
        created = await wl.create(db.pool, user_id=owner, name="mine")
        assert await delete_list_fn(
            db.pool, watchlist_id=created["id"], user_id=other
        ) is False
        assert await db.pool.fetchval(
            "SELECT count(*) FROM watchlist WHERE id = $1", created["id"]
        ) == 1
