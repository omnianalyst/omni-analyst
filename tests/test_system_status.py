"""The /system/status endpoint: loop health read from the data each loop writes.

A loop that is alive writes rows; a dead one stops. This test pins the contract:
the endpoint refuses anonymous callers (it reveals provider fill rates and
counts), and after auth it returns a freshness row per loop plus demand and
production summaries.
"""

import asyncio

import pytest
from neutron.test import TestClient

from omni.main import create_app

GOOD_SECRET = "x" * 48


class _Lifespan:
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
        msg = await self._send.get()
        assert msg["type"] == "lifespan.startup.complete", msg
        return self._app

    async def __aexit__(self, *exc):
        await self._receive.put({"type": "lifespan.shutdown"})
        await self._send.get()
        await self._task


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("OMNI_JWT_SECRET", GOOD_SECRET)
    yield


@pytest.fixture(autouse=True)
async def _clean_users(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


async def _setup_token(client) -> str:
    r = await client.post(
        "/auth/setup",
        json={"email": "op@example.com", "password": "a" * 16},
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def test_status_refuses_anonymous_caller(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/system/status")
    assert r.status_code == 401


async def test_status_returns_freshness_per_loop_for_operator(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_token(client)
        r = await client.get(
            "/system/status", headers={"authorization": f"Bearer {token}"}
        )
    assert r.status_code == 200, r.text
    body = r.json()

    # Every loop the engine runs is represented, even on an empty DB where it
    # has never written -- never_run distinguishes that from stale.
    loop_names = {entry["loop"] for entry in body["loops"]}
    assert {"prediction", "finding", "fill", "demand", "claim_ingest"} <= loop_names
    for entry in body["loops"]:
        assert "last_activity" in entry
        assert "age_seconds" in entry
        assert "never_run" in entry

    assert "active" in body["demand"]
    assert "production_24h" in body
    assert "predictions" in body["production_24h"]
    assert "findings" in body["production_24h"]
