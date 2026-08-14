"""Venue credentials go in through one door, encrypted, and never come back out.

The defects guarded here are all quiet ones: a secret echoed in a response, a
plaintext row that reads back perfectly, a partially-migrated record reported as
safe, and trading keys reachable from the process that answers public requests.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from neutron.test import TestClient

from omni.api.settings import _venue_catalog_payload
from omni.credentials import keyring
from omni.main import create_app


class _Lifespan:
    def __init__(self, app):
        self.app = app
        self.receive = asyncio.Queue()
        self.send = asyncio.Queue()

    async def __aenter__(self):
        self.task = asyncio.create_task(
            self.app({"type": "lifespan"}, self.receive.get, self.send.put)
        )
        await self.receive.put({"type": "lifespan.startup"})
        assert (await self.send.get())["type"] == "lifespan.startup.complete"
        return self.app

    async def __aexit__(self, *exc):
        await self.receive.put({"type": "lifespan.shutdown"})
        await self.send.get()
        await self.task


@pytest.fixture(autouse=True)
def _isolated_key(tmp_path, monkeypatch):
    monkeypatch.delenv(keyring.KEY_ENV, raising=False)
    monkeypatch.setenv(keyring.KEY_PATH_ENV, str(tmp_path / "credential.key"))
    yield


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


async def _setup(client):
    r = await client.post("/auth/setup",
                          json={"email": "operator@example.com", "password": "a" * 16})
    assert r.status_code == 200, r.text
    return {"authorization": f"Bearer {r.json()['token']}"}


async def _member(client, operator_headers):
    created = await client.post(
        "/auth/register",
        headers=operator_headers,
        json={"email": "member@example.com", "password": "b" * 16},
    )
    assert created.status_code == 201, created.text
    login = await client.post(
        "/auth/login",
        json={"email": "member@example.com", "password": "b" * 16},
    )
    assert login.status_code == 200, login.text
    return {"authorization": f"Bearer {login.json()['token']}"}


async def test_storing_credentials_requires_authentication(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.post("/settings/venue/questrade/credentials",
                              json={"credentials": {"refresh_token": "t"}})
    assert r.status_code == 401


async def test_a_stored_secret_is_encrypted_in_the_database(database_url, db):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        r = await client.post("/settings/venue/questrade/credentials", headers=headers,
                              json={"credentials": {"refresh_token": "super-secret-token"}})

    assert r.status_code == 201, r.text
    assert r.json()["encrypted"] is True

    row = await db.pool.fetchval("SELECT data FROM user_settings LIMIT 1")
    raw = row if isinstance(row, str) else json.dumps(row)
    assert "super-secret-token" not in raw, "the secret is sitting in the database"
    assert "enc:v1:" in raw


async def test_the_secret_is_never_echoed_back(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        await client.post("/settings/venue/questrade/credentials", headers=headers,
                          json={"credentials": {"refresh_token": "super-secret-token"}})
        config = await client.get("/settings/config", headers=headers)

    assert "super-secret-token" not in config.text


async def test_hyperliquid_credentials_are_refused_from_the_browser(database_url):
    """The api process must never hold keys that can move the carry book."""
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        r = await client.post("/settings/venue/hyperliquid/credentials", headers=headers,
                              json={"credentials": {"private_key": "0xdead"}})

    assert r.status_code == 400
    assert "deployment-managed" in r.text


async def test_unknown_fields_and_missing_required_fields_are_refused(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        unknown = await client.post("/settings/venue/questrade/credentials", headers=headers,
                                    json={"credentials": {"refresh_token": "t", "nope": "x"}})
        missing = await client.post("/settings/venue/questrade/credentials", headers=headers,
                                    json={"credentials": {"practice": True}})
        empty = await client.post("/settings/venue/questrade/credentials", headers=headers,
                                  json={"credentials": {}})

    assert unknown.status_code == 400 and "nope" in unknown.text
    assert missing.status_code == 400 and "refresh_token" in missing.text
    assert empty.status_code == 400


async def test_an_unknown_venue_is_not_found(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        r = await client.post("/settings/venue/nasdaq/credentials", headers=headers,
                              json={"credentials": {"token": "x"}})
    assert r.status_code == 404


async def test_ibkr_is_not_advertised_or_accepted(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        config = await client.get("/settings/config", headers=headers)
        mutation = await client.post(
            "/settings/venue/ibkr/credentials",
            headers=headers,
            json={"credentials": {"username": "u", "password": "p"}},
        )

    assert "ibkr" not in {entry["key"] for entry in config.json()["venue_catalog"]}
    assert mutation.status_code == 404


async def test_only_configured_questrade_can_be_enabled(database_url, monkeypatch):
    from omni.venue import manager

    async def refresh(_pool, _user_id):
        return {"questrade": "connected"}

    monkeypatch.setattr(manager, "refresh_venues", refresh)
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        missing = await client.post(
            "/settings/venue/questrade/toggle",
            headers=headers,
            json={"enabled": True},
        )
        invalid = await client.post(
            "/settings/venue/questrade/toggle",
            headers=headers,
            json={"enabled": "yes"},
        )
        scheduler_only = await client.post(
            "/settings/venue/hyperliquid/toggle",
            headers=headers,
            json={"enabled": True},
        )
        await client.post(
            "/settings/venue/questrade/credentials",
            headers=headers,
            json={"credentials": {"refresh_token": "token"}},
        )
        enabled = await client.post(
            "/settings/venue/questrade/toggle",
            headers=headers,
            json={"enabled": True},
        )
        config = await client.get("/settings/config", headers=headers)

    questrade = next(v for v in config.json()["venue_catalog"] if v["key"] == "questrade")
    assert missing.status_code == 400 and "credentials" in missing.text
    assert invalid.status_code == 400 and "boolean" in invalid.text
    assert scheduler_only.status_code == 400 and "scheduler-managed" in scheduler_only.text
    assert enabled.status_code == 201
    assert enabled.json() == {"status": "enabled", "venue_status": "connected"}
    assert questrade["enabled"] is True


async def test_live_status_reads_only_the_callers_connections(
    database_url, monkeypatch
):
    from omni.venue import manager

    class Venue:
        def __init__(self, name):
            self.name = name

        async def positions(self):
            return [
                SimpleNamespace(
                    symbol=self.name,
                    quantity=1,
                    market_type="spot",
                    average_entry=10,
                )
            ]

        async def balances(self):
            return [SimpleNamespace(asset=self.name, free=1, locked=0)]

    seen = []
    owner_venues = {}

    async def refresh(_pool, user_id):
        seen.append(("refresh", user_id))
        return {"questrade": "connected"}

    def connected(user_id):
        seen.append(("read", user_id))
        return {"questrade": owner_venues[user_id]}

    monkeypatch.setattr(manager, "refresh_venues", refresh)
    monkeypatch.setattr(manager, "connected_venues", connected)

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        operator_headers = await _setup(client)
        member_headers = await _member(client, operator_headers)
        operator_id = await app.db.pool.fetchval(
            "SELECT id FROM users WHERE role = 'operator'"
        )
        member_id = await app.db.pool.fetchval(
            "SELECT id FROM users WHERE role = 'member'"
        )
        await app.db.pool.executemany(
            "INSERT INTO user_settings (user_id, data) VALUES ($1, $2::jsonb)",
            [
                (operator_id, json.dumps({"venues": {"questrade": {
                    "enabled": True,
                    "credentials": {"refresh_token": "legacy"},
                }}})),
                (member_id, json.dumps({"venues": {"questrade": {
                    "enabled": True,
                    "credentials": {"refresh_token": "legacy"},
                }}})),
            ],
        )
        owner_venues[operator_id] = Venue("operator-only")
        owner_venues[member_id] = Venue("member-only")

        operator = await client.get(
            "/settings/venues/status", headers=operator_headers
        )
        member = await client.get(
            "/settings/venues/status", headers=member_headers
        )

    operator_venue = next(v for v in operator.json()["venues"] if v["key"] == "questrade")
    member_venue = next(v for v in member.json()["venues"] if v["key"] == "questrade")
    assert operator_venue["positions"][0]["symbol"] == "operator-only"
    assert member_venue["positions"][0]["symbol"] == "member-only"
    assert operator_venue["status"] == "connected"
    assert operator_venue["checked_at"] <= operator.json()["checked_at"]
    assert operator_venue["error"] is None
    assert seen == [
        ("refresh", operator_id),
        ("read", operator_id),
        ("refresh", member_id),
        ("read", member_id),
    ]


async def test_live_status_reports_read_failure_and_completion_time(
    database_url, monkeypatch
):
    from omni.venue import manager

    class FailingVenue:
        name = "questrade"

        async def positions(self):
            raise RuntimeError("account read timed out")

        async def balances(self):
            return []

    async def refresh(_pool, _user_id):
        return {"questrade": "connected"}

    venue = FailingVenue()
    monkeypatch.setattr(manager, "refresh_venues", refresh)
    monkeypatch.setattr(manager, "connected_venues", lambda _user_id: {"questrade": venue})

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        user_id = await app.db.pool.fetchval("SELECT id FROM users LIMIT 1")
        await app.db.pool.execute(
            "INSERT INTO user_settings (user_id, data) VALUES ($1, $2::jsonb)",
            user_id,
            json.dumps({"venues": {"questrade": {
                "enabled": True,
                "credentials": {"refresh_token": "legacy"},
            }}}),
        )
        response = await client.get("/settings/venues/status", headers=headers)

    questrade = next(v for v in response.json()["venues"] if v["key"] == "questrade")
    assert questrade["status"] == "error"
    assert questrade["error"] == "positions: account read timed out"
    assert questrade["positions"] == []
    assert questrade["checked_at"] <= response.json()["checked_at"]


async def test_clearing_credentials_disables_the_venue(database_url, db):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        await client.post("/settings/venue/questrade/credentials", headers=headers,
                          json={"credentials": {"refresh_token": "t"}})
        cleared = await client.delete("/settings/venue/questrade/credentials", headers=headers)
        config = await client.get("/settings/config", headers=headers)

    assert cleared.json()["status"] == "cleared"
    entry = next(v for v in config.json()["venue_catalog"] if v["key"] == "questrade")
    assert entry["configured"] is False
    assert entry["enabled"] is False
    assert entry["configuration_source"] == "unavailable"


def test_a_plaintext_record_is_reported_as_legacy_not_encrypted():
    saved = {
        "venues": {
            "questrade": {
                "enabled": False,
                "credentials": {
                    "refresh_token": "still-plaintext",
                },
            }
        }
    }

    entry = next(v for v in _venue_catalog_payload(saved) if v["key"] == "questrade")

    assert entry["configuration_source"] == "legacy"
    assert entry["configured"] is True


def test_a_fully_encrypted_record_reports_encrypted():
    saved = {
        "venues": {
            "questrade": {
                "enabled": True,
                "credentials": {"refresh_token": keyring.encrypt("t"), "practice": True},
            }
        }
    }

    entry = next(v for v in _venue_catalog_payload(saved) if v["key"] == "questrade")

    assert entry["configuration_source"] == "encrypted"


def test_hyperliquid_always_reports_deployment_regardless_of_stored_rows():
    saved = {"venues": {"hyperliquid": {"credentials": {"private_key": "leaked"}}}}

    entry = next(v for v in _venue_catalog_payload(saved) if v["key"] == "hyperliquid")

    assert entry["configuration_source"] == "deployment"


async def test_generic_settings_mutation_cannot_replace_venue_credentials(database_url, db):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        await client.post(
            "/settings/venue/questrade/credentials",
            headers=headers,
            json={"credentials": {"refresh_token": "keep-me", "practice": True}},
        )
        mutation = await client.post(
            "/settings",
            headers=headers,
            json={"venues": {"questrade": {"enabled": True}}},
        )

    row = await db.pool.fetchval("SELECT data FROM user_settings LIMIT 1")
    data = json.loads(row) if isinstance(row, str) else row
    encrypted = data["venues"]["questrade"]["credentials"]["refresh_token"]
    assert mutation.status_code in {404, 405}
    assert keyring.decrypt(encrypted) == "keep-me"


async def test_rotated_token_is_encrypted_without_dropping_other_settings(database_url):
    from omni.venue import manager

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        await client.post(
            "/settings/venue/questrade/credentials",
            headers=headers,
            json={"credentials": {"refresh_token": "old", "practice": False}},
        )
        user_id = await app.db.pool.fetchval("SELECT id FROM users LIMIT 1")
        await manager.store_venue_refresh_token(
            app.db.pool, user_id, "questrade", "rotated"
        )
        row = await app.db.pool.fetchval(
            "SELECT data FROM user_settings WHERE user_id = $1", user_id
        )

    data = json.loads(row) if isinstance(row, str) else row
    credentials = data["venues"]["questrade"]["credentials"]
    assert credentials["practice"] is False
    assert credentials["refresh_token"] != "rotated"
    assert keyring.decrypt(credentials["refresh_token"]) == "rotated"


async def test_concurrent_narrow_writes_preserve_credentials_and_enabled_state(database_url):
    from omni.venue import manager

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        await client.post(
            "/settings/venue/questrade/credentials",
            headers=headers,
            json={"credentials": {"refresh_token": "old", "practice": True}},
        )
        user_id = await app.db.pool.fetchval("SELECT id FROM users LIMIT 1")
        await asyncio.gather(
            manager.store_venue_credentials(
                app.db.pool,
                user_id,
                "questrade",
                {"refresh_token": "replacement", "practice": False},
            ),
            manager.set_venue_enabled(app.db.pool, user_id, "questrade", False),
        )
        row = await app.db.pool.fetchval(
            "SELECT data FROM user_settings WHERE user_id = $1", user_id
        )

    data = json.loads(row) if isinstance(row, str) else row
    venue = data["venues"]["questrade"]
    assert venue["enabled"] is False
    assert venue["credentials"]["practice"] is False
    assert keyring.decrypt(venue["credentials"]["refresh_token"]) == "replacement"


async def test_replacing_credentials_preserves_other_settings_fields(database_url):
    from omni.venue import manager

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        await _setup(client)
        user_id = await app.db.pool.fetchval("SELECT id FROM users LIMIT 1")
        await app.db.pool.execute(
            "INSERT INTO user_settings (user_id, data) VALUES ($1, $2::jsonb)",
            user_id,
            json.dumps({
                "providers": {"display_density": "compact"},
                "venues": {"questrade": {"enabled": True}},
            }),
        )
        await manager.store_venue_credentials(
            app.db.pool,
            user_id,
            "questrade",
            {"refresh_token": "replacement", "practice": True},
        )
        row = await app.db.pool.fetchval(
            "SELECT data FROM user_settings WHERE user_id = $1", user_id
        )

    data = json.loads(row) if isinstance(row, str) else row
    assert data["providers"] == {"display_density": "compact"}
    assert data["venues"]["questrade"]["enabled"] is True
    assert keyring.decrypt(
        data["venues"]["questrade"]["credentials"]["refresh_token"]
    ) == "replacement"
