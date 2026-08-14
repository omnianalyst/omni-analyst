import asyncio
import json
from uuid import uuid4

import pytest

from omni.venue import manager


class FakeVenue:
    def __init__(self, token: str, *, close_error: bool = False) -> None:
        self.token = token
        self.closed = False
        self.close_error = close_error

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error:
            raise RuntimeError("close failed")


@pytest.fixture(autouse=True)
async def _clean_manager(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    await manager.disconnect_all()
    yield
    await manager.disconnect_all()


async def test_two_users_hold_distinct_connections_and_reads(monkeypatch):
    alice = uuid4()
    bob = uuid4()
    configs = {
        alice: {"venues": {"questrade": {"enabled": True, "credentials": {"token": "alice"}}}},
        bob: {"venues": {"questrade": {"enabled": True, "credentials": {"token": "bob"}}}},
    }

    async def load(_pool, user_id):
        return configs.get(user_id, {})

    async def connect(_key, credentials, _on_refresh_token):
        return FakeVenue(credentials["token"])

    monkeypatch.setattr(manager, "_load_venue_config", load)
    monkeypatch.setattr(manager, "_connect_venue", connect)
    monkeypatch.setattr(manager, "decrypt_fields", lambda values, _fields: values)

    await manager.refresh_venues(None, alice)
    await manager.refresh_venues(None, bob)

    alice_venue = manager.get_venue(alice, "questrade")
    bob_venue = manager.get_venue(bob, "questrade")
    assert alice_venue.token == "alice"
    assert bob_venue.token == "bob"
    assert alice_venue is not bob_venue
    assert manager.connected_venues(alice) == {"questrade": alice_venue}
    assert manager.connected_venues(bob) == {"questrade": bob_venue}


async def test_one_user_cannot_disconnect_another_users_connection(monkeypatch):
    alice = uuid4()
    bob = uuid4()
    configs = {
        alice: {"venues": {"questrade": {"enabled": True, "credentials": {"token": "alice"}}}},
        bob: {"venues": {"questrade": {"enabled": True, "credentials": {"token": "bob"}}}},
    }

    async def load(_pool, user_id):
        return configs.get(user_id, {})

    async def connect(_key, credentials, _on_refresh_token):
        return FakeVenue(credentials["token"])

    monkeypatch.setattr(manager, "_load_venue_config", load)
    monkeypatch.setattr(manager, "_connect_venue", connect)
    monkeypatch.setattr(manager, "decrypt_fields", lambda values, _fields: values)

    await manager.refresh_venues(None, alice)
    await manager.refresh_venues(None, bob)
    alice_venue = manager.get_venue(alice, "questrade")
    bob_venue = manager.get_venue(bob, "questrade")

    configs[bob] = {"venues": {"questrade": {"enabled": False}}}
    await manager.refresh_venues(None, bob)

    assert bob_venue.closed is True
    assert manager.get_venue(bob, "questrade") is None
    assert alice_venue.closed is False
    assert manager.get_venue(alice, "questrade") is alice_venue


async def test_user_without_settings_cannot_see_existing_connection(monkeypatch):
    alice = uuid4()
    bob = uuid4()
    configs = {
        alice: {"venues": {"questrade": {"enabled": True, "credentials": {"token": "alice"}}}}
    }

    async def load(_pool, user_id):
        return configs.get(user_id, {})

    monkeypatch.setattr(manager, "_load_venue_config", load)
    monkeypatch.setattr(manager, "_connect_venue", lambda *_args: None)
    monkeypatch.setattr(manager, "decrypt_fields", lambda values, _fields: values)

    venue = FakeVenue("alice")
    manager._venues[alice] = {"questrade": venue}
    status = await manager.refresh_venues(None, bob)

    assert status == {}
    assert manager.connected_venues(bob) == {}
    assert manager.connected_venues(alice) == {"questrade": venue}


async def test_shutdown_closes_every_owner_even_when_one_close_fails():
    alice = uuid4()
    bob = uuid4()
    failing = FakeVenue("alice", close_error=True)
    healthy = FakeVenue("bob")
    manager._venues[alice] = {"questrade": failing}
    manager._venues[bob] = {"questrade": healthy}

    await manager.disconnect_all()

    assert failing.closed is True
    assert healthy.closed is True
    assert manager.connected_venues(alice) == {}
    assert manager.connected_venues(bob) == {}


async def test_concurrent_refreshes_create_one_connection_for_one_owner(monkeypatch):
    owner = uuid4()
    connects = 0

    async def load(_pool, _user_id):
        return {
            "venues": {
                "questrade": {
                    "enabled": True,
                    "credentials": {"token": "owner"},
                }
            }
        }

    async def connect(_key, credentials, _on_refresh_token):
        nonlocal connects
        connects += 1
        await asyncio.sleep(0.01)
        return FakeVenue(credentials["token"])

    monkeypatch.setattr(manager, "_load_venue_config", load)
    monkeypatch.setattr(manager, "_connect_venue", connect)
    monkeypatch.setattr(manager, "decrypt_fields", lambda values, _fields: values)

    await asyncio.gather(
        manager.refresh_venues(None, owner),
        manager.refresh_venues(None, owner),
    )

    assert connects == 1
    assert set(manager.connected_venues(owner)) == {"questrade"}


async def test_ibkr_and_hyperliquid_are_never_connected_by_the_api_manager(monkeypatch):
    owner = uuid4()

    async def load(_pool, _user_id):
        return {
            "venues": {
                "ibkr": {"enabled": True, "credentials": {"password": "secret"}},
                "hyperliquid": {"enabled": True, "credentials": {"private_key": "key"}},
            }
        }

    async def connect(*_args):
        raise AssertionError("an unavailable venue reached the connector")

    monkeypatch.setattr(manager, "_load_venue_config", load)
    monkeypatch.setattr(manager, "_connect_venue", connect)

    status = await manager.refresh_venues(None, owner)

    assert status == {"ibkr": "unavailable", "hyperliquid": "scheduler-only"}
    assert manager.connected_venues(owner) == {}


async def test_connection_errors_redact_credentials(monkeypatch):
    owner = uuid4()
    secret = "refresh-token-that-must-not-leak"

    async def load(_pool, _user_id):
        return {
            "venues": {
                "questrade": {
                    "enabled": True,
                    "credentials": {"refresh_token": secret},
                }
            }
        }

    async def connect(_key, _credentials, _on_refresh_token):
        raise RuntimeError(f"provider rejected {secret}")

    monkeypatch.setattr(manager, "_load_venue_config", load)
    monkeypatch.setattr(manager, "_connect_venue", connect)
    monkeypatch.setattr(manager, "decrypt_fields", lambda values, _fields: values)

    status = await manager.refresh_venues(None, owner)

    assert status["questrade"] == "error: provider rejected [redacted]"
    assert secret not in status["questrade"]


async def test_reconcile_visits_every_active_configured_user(db, monkeypatch):
    operator = uuid4()
    member = uuid4()
    inactive = uuid4()
    await db.pool.executemany(
        "INSERT INTO users (id, email, password_hash, active, role) "
        "VALUES ($1, $2, 'hash', $3, $4)",
        [
            (operator, "operator@example.com", True, "operator"),
            (member, "member@example.com", True, "member"),
            (inactive, "inactive@example.com", False, "member"),
        ],
    )
    await db.pool.executemany(
        "INSERT INTO user_settings (user_id, data) VALUES ($1, $2::jsonb)",
        [
            (operator, json.dumps({"venues": {}})),
            (member, json.dumps({"venues": {}})),
            (inactive, json.dumps({"venues": {}})),
        ],
    )
    seen = []

    async def refresh(_pool, user_id):
        seen.append(user_id)
        return {}

    monkeypatch.setattr(manager, "refresh_venues", refresh)

    await manager.reconcile_once(db.pool)

    assert set(seen) == {operator, member}


async def test_reconcile_loop_records_contained_connection_errors(db, monkeypatch):
    await db.pool.execute("TRUNCATE loop_health")
    stopping = asyncio.Event()
    owner = uuid4()

    async def reconcile(_pool):
        stopping.set()
        return {owner: {"questrade": "error: provider unavailable"}}

    monkeypatch.setattr(manager, "reconcile_once", reconcile)

    await manager.reconcile_forever(db.pool, stopping, interval=17.0)

    row = await db.pool.fetchrow(
        "SELECT last_status, last_error, last_result, expected_interval_seconds "
        "FROM loop_health WHERE loop_name = 'venue_reconciliation'"
    )
    assert row["last_status"] == "failure"
    assert row["last_error"] == f"{owner}:questrade=error: provider unavailable"
    assert row["last_result"] == "1 configured users checked"
    assert row["expected_interval_seconds"] == 17.0
