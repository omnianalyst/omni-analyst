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


async def test_loops_report_never_run_on_a_fresh_deployment(db, database_url):
    # No loop has ever written. never_run distinguishes a fresh deployment from
    # a stale loop -- the honesty property the status rail relies on. A
    # regression that graded never_run as stale, or computed a fake age, would
    # flip these.
    #
    # The shared test DB carries residue from other suites' writes (the
    # autouse _clean_users only truncates users), so clear every loop's output
    # table to model a genuinely fresh deployment.
    await db.pool.execute(
        "TRUNCATE prediction, finding, fill_attempt, demand, claim CASCADE"
    )
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_token(client)
        r = await client.get(
            "/system/status", headers={"authorization": f"Bearer {token}"}
        )
    assert r.status_code == 200
    for entry in r.json()["loops"]:
        assert entry["never_run"] is True, entry
        assert entry["last_activity"] is None
        assert entry["age_seconds"] is None


async def test_a_loop_that_has_written_reports_an_age_and_not_never_run(
    db, database_url
):
    # Seed one demand row ~2h ago so the demand loop has written exactly once.
    entity = await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('company', 'T', 'T') RETURNING id"
    )
    await db.pool.execute(
        "INSERT INTO demand (entity_id, claim_type, channel, created_at) "
        "VALUES ($1, 'macro_series_point'::claim_type, 'test', "
        "now() - interval '2 hours')",
        entity,
    )

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_token(client)
        r = await client.get(
            "/system/status", headers={"authorization": f"Bearer {token}"}
        )
    assert r.status_code == 200
    demand = next(lo for lo in r.json()["loops"] if lo["loop"] == "demand")
    assert demand["never_run"] is False
    assert demand["last_activity"] is not None
    # ~2h = ~7200s. A real age, not None (which would read as never_run) and not
    # a value the pre-fix shape-only tests could have caught.
    assert demand["age_seconds"] is not None
    assert demand["age_seconds"] > 7000
