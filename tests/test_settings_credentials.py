"""Venue credentials go in through one door, encrypted, and never come back out.

The defects guarded here are all quiet ones: a secret echoed in a response, a
plaintext row that reads back perfectly, a partially-migrated record reported as
safe, and trading keys reachable from the process that answers public requests.
"""

import asyncio
import json

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
        missing = await client.post("/settings/venue/ibkr/credentials", headers=headers,
                                    json={"credentials": {"username": "u"}})
        empty = await client.post("/settings/venue/questrade/credentials", headers=headers,
                                  json={"credentials": {}})

    assert unknown.status_code == 400 and "nope" in unknown.text
    assert missing.status_code == 400 and "password" in missing.text
    assert empty.status_code == 400


async def test_an_unknown_venue_is_not_found(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        r = await client.post("/settings/venue/nasdaq/credentials", headers=headers,
                              json={"credentials": {"token": "x"}})
    assert r.status_code == 404


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


def test_a_partially_migrated_record_is_reported_as_legacy_not_encrypted():
    """One plaintext secret is enough to make the record unsafe.

    Reporting 'encrypted' because the other field happens to be wrapped would
    tell the operator the row is safe when half of it is readable.
    """
    saved = {
        "venues": {
            "ibkr": {
                "enabled": False,
                "credentials": {
                    "username": keyring.encrypt("operator"),
                    "password": "still-plaintext",
                },
            }
        }
    }

    entry = next(v for v in _venue_catalog_payload(saved) if v["key"] == "ibkr")

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
