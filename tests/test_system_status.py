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


async def _member_token(client, operator_token: str) -> str:
    created = await client.post(
        "/auth/register",
        json={"email": "member@example.com", "password": "b" * 16},
        headers={"authorization": f"Bearer {operator_token}"},
    )
    assert created.status_code == 201, created.text
    login = await client.post(
        "/auth/login",
        json={"email": "member@example.com", "password": "b" * 16},
    )
    assert login.status_code == 200, login.text
    return login.json()["token"]


async def test_status_refuses_anonymous_caller(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/system/status")
    assert r.status_code == 401


async def test_status_refuses_authenticated_member(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        operator_token = await _setup_token(client)
        member_token = await _member_token(client, operator_token)
        r = await client.get(
            "/system/status",
            headers={"authorization": f"Bearer {member_token}"},
        )
    assert r.status_code == 403


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


async def test_health_overall_is_none_on_a_fresh_deployment(db, database_url):
    # No loop has ever iterated, so loop_health is empty. overall is None (honest
    # "no data yet"), not a fake "ok" -- the effect-derived `loops` array already
    # carries the never_run signal for this case.
    await db.pool.execute("TRUNCATE loop_health")
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_token(client)
        r = await client.get(
            "/system/status", headers={"authorization": f"Bearer {token}"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["health"]["overall"] is None
    by_loop = {row["loop"]: row for row in body["health"]["loops"]}
    assert {
        "autonomous.macro",
        "venue_reconciliation",
        "carry",
        "nav",
        "shadow_decision",
        "shadow_scoring",
        "launch_sweep",
    }.issubset(by_loop)
    assert by_loop["carry"] == {
        "loop": "carry",
        "state": "never_run",
        "last_status": None,
        "last_success_at": None,
        "last_failure_at": None,
        "consecutive_failures": 0,
        "last_error": None,
        "last_result": None,
        "expected_interval_seconds": 86400.0,
    }


async def test_health_grades_each_loop_from_its_recorded_state(db, database_url):
    # Seed three loop_health rows that exercise every verdict branch:
    #  - 'fill':    recent success, no failures                 -> ok
    #  - 'predict': recent success but consecutive_failures > 0 -> failing
    #  - 'sweep':   success long ago (>5x its interval)         -> stale
    await db.pool.execute("TRUNCATE loop_health")
    await db.pool.execute(
        """
        INSERT INTO loop_health
            (loop_name, last_success_at, last_failure_at,
             consecutive_failures, last_error, expected_interval_seconds)
        VALUES
            ('fill',    now(),                              NULL, 0, NULL, 30),
            ('predict', now(),                              now(), 2, 'NoCoverage', 300),
            ('sweep',   now() - interval '2 hours',         NULL, 0, NULL, 300)
        """
    )
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_token(client)
        r = await client.get(
            "/system/status", headers={"authorization": f"Bearer {token}"}
        )
    assert r.status_code == 200, r.text
    health = r.json()["health"]
    by_loop = {h["loop"]: h for h in health["loops"]}
    assert by_loop["fill"]["state"] == "ok"
    assert by_loop["predict"]["state"] == "failing"
    assert "NoCoverage" in by_loop["predict"]["last_error"]
    # A 2h-old success vs a 300s interval (stale threshold 5x = 1500s) is stale.
    assert by_loop["sweep"]["state"] == "stale"
    # overall is the worst present -> failing outranks stale outranks ok.
    assert health["overall"] == "failing"


async def test_health_marks_a_loop_that_only_ever_failed_as_failing(db, database_url):
    # last_success_at NULL + consecutive_failures > 0: a loop that has never
    # succeeded. This is the quietly-broken case the feature exists to catch --
    # it must not grade 'stale' (which implies a prior success) or 'ok'.
    await db.pool.execute("TRUNCATE loop_health")
    await db.pool.execute(
        """
        INSERT INTO loop_health
            (loop_name, last_success_at, last_failure_at,
             consecutive_failures, last_error, expected_interval_seconds)
        VALUES ('resolve', NULL, now(), 5, 'always raises', 60)
        """
    )
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_token(client)
        r = await client.get(
            "/system/status", headers={"authorization": f"Bearer {token}"}
        )
    assert r.status_code == 200
    by_loop = {h["loop"]: h for h in r.json()["health"]["loops"]}
    assert by_loop["resolve"]["state"] == "failing"
    assert r.json()["health"]["overall"] == "failing"


async def test_status_reports_claim_store_volume_and_daily_arrival(
    db, database_url
):
    """The board's data-throughput tiles: total under management and the
    last-24h arrival rate, so a store that stops growing while demand is
    active becomes visible on the same page as the loops that fill it."""

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_token(client)
        response = await client.get(
            "/system/status", headers={"authorization": f"Bearer {token}"}
        )

    body = response.json()
    assert body["claims"]["total"] >= 0
    assert body["claims"]["last_24h"] >= 0
