"""Objective API: the agentic surface.

The redistribution rule surfaces here through HTTP: no price source is
redistributable, so a shareable objective needing prices is refused with
``only_licensed_sources_can_produce_this`` -- the licence decision made in the
planner reaching the caller as a structured shortfall rather than a failed
write. The demand rule is the other thing these tests defend: a missing
producer becomes demand (the system's work queue), but a licensing refusal
does not, because fetching it again would not help.

The ``/analysis/run`` endpoint (D8) is the name-keyed invocation path for
analyses that declare their arguments. It is tested here end-to-end through
HTTP: the one declared analysis today (``perception.divergence``) returns a
result with provenance and a licence verdict, unknown names and
no-declared-arguments capabilities are distinct client errors, and nothing is
written to the database.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from neutron.test import TestClient

from omni.api.objective import build_router
from omni.main import create_app

GOOD_SECRET = "x" * 48


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


async def _auth_headers(client):
    response = await client.post(
        "/auth/setup",
        json={"email": "operator@example.com", "password": "a" * 16},
    )
    assert response.status_code == 200, response.text
    return {"authorization": f"Bearer {response.json()['token']}"}


async def _entity(db, symbol="AAPL", kind="company", name=None):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ($1, $2, $3) "
        "RETURNING id",
        kind,
        symbol,
        name or symbol,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users, entity CASCADE")
    yield


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("OMNI_JWT_SECRET", GOOD_SECRET)
    yield


async def test_a_satisfiable_objective_returns_steps_with_costs_and_no_shortfalls(
    db, database_url
):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.post(
            "/objective/plan",
            json={
                "text": "fundamentals for AAPL",
                "target": "AAPL",
                "needs": ["fundamental_metric"],
                "entity_kind": "company",
            },
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["satisfiable"] is True
    assert body["shortfalls"] == []
    assert len(body["steps"]) == 1
    step = body["steps"][0]
    assert step["capability"] == "edgar.companyfacts"
    assert step["claim_type"] == "fundamental_metric"
    assert step["cost"] == 2.0
    assert step["licence_tier"] == "shared"
    assert body["cost"] == 2.0
    assert body["summary"]


async def test_a_shareable_objective_needing_prices_is_refused(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.post(
            "/objective/plan",
            json={
                "text": "shared price read",
                "target": "AAPL",
                "needs": ["price_snapshot"],
                "shareable": True,
            },
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["steps"] == []
    assert len(body["shortfalls"]) == 1
    shortfall = body["shortfalls"][0]
    assert shortfall["claim_type"] == "price_snapshot"
    assert shortfall["reason"] == "only_licensed_sources_can_produce_this"
    assert not body["satisfiable"]


async def test_an_unanswerable_objective_produces_demand_rows(
    db, database_url, monkeypatch
):
    """An objective the registry cannot serve becomes demand.

    The no-producer condition is constructed, not discovered: build a registry
    with every producer of one real CLAIM_TYPES member removed, so the
    NO_PRODUCER shortfall path and the demand it raises are exercised no matter
    how complete the live registry becomes. The previous form read the live
    registry and skipped once every claim type gained a producer, which
    silently dropped coverage of this path.
    """
    from omni.api import objective as objective_module
    from omni.capability.extracted import CLAIM_TYPES
    from omni.capability.registry import Registry
    from omni.scheduler.worker import default_registry

    claim_type = "news_event"
    assert claim_type in CLAIM_TYPES

    def _registry_without(omitted):
        pruned = Registry()
        full = default_registry()
        for cap in full._by_name.values():
            if omitted not in cap.produces:
                pruned.add(cap)
        for name, hit_rate in full._reliability.items():
            pruned.observe_reliability(name, hit_rate)
        return pruned

    monkeypatch.setattr(
        objective_module, "default_registry", lambda: _registry_without(claim_type)
    )

    await _entity(db, symbol="AAPL")
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _auth_headers(client)
        before = await db.pool.fetchval("SELECT count(*) FROM demand")
        r = await client.post(
            "/objective/run",
            json={
                "text": "positioning for AAPL",
                "target": "AAPL",
                "needs": [claim_type],
            },
            headers=headers,
        )
        after = await db.pool.fetchval("SELECT count(*) FROM demand")

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["steps"] == []
    assert len(body["shortfalls"]) == 1
    assert body["shortfalls"][0]["reason"] == "no_capability_produces_this"
    assert len(body["demand_raised"]) == 1
    assert after == before + 1
    row = await db.pool.fetchrow(
        "SELECT claim_type::text AS claim_type, active FROM demand"
    )
    assert row["claim_type"] == claim_type
    assert row["active"]


async def test_a_licensing_shortfall_produces_no_demand(db, database_url):
    """Fetching it again would not help; it is forbidden, not absent."""
    await _entity(db, symbol="AAPL")
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _auth_headers(client)
        before = await db.pool.fetchval("SELECT count(*) FROM demand")
        r = await client.post(
            "/objective/run",
            json={
                "text": "shared price",
                "target": "AAPL",
                "needs": ["price_snapshot"],
                "shareable": True,
            },
            headers=headers,
        )
        after = await db.pool.fetchval("SELECT count(*) FROM demand")

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["shortfalls"][0]["reason"] == "only_licensed_sources_can_produce_this"
    assert body["demand_raised"] == []
    assert after == before


async def test_anonymous_objective_run_does_not_plan_execute_or_write(
    db, database_url, monkeypatch
):
    from omni.api import objective as objective_module

    await _entity(db, symbol="AAPL")
    called = []

    def fail_plan(*args, **kwargs):
        called.append("plan")
        raise AssertionError("anonymous request reached planning")

    async def fail_execute(*args, **kwargs):
        called.append("execute")
        raise AssertionError("anonymous request reached execution")

    monkeypatch.setattr(objective_module, "plan", fail_plan)
    monkeypatch.setattr(objective_module, "execute", fail_execute)
    before = await db.pool.fetchval("SELECT count(*) FROM demand")

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        response = await client.post(
            "/objective/run",
            json={
                "text": "fundamentals for AAPL",
                "target": "AAPL",
                "needs": ["fundamental_metric"],
            },
        )

    assert response.status_code == 401
    assert called == []
    assert await db.pool.fetchval("SELECT count(*) FROM demand") == before


async def test_plan_writes_nothing_to_the_database(db, database_url):
    await _entity(db, symbol="AAPL")
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        tables = ("claim", "demand", "gap")
        before = {
            t: await db.pool.fetchval(f"SELECT count(*) FROM {t}") for t in tables
        }
        # A mix of a plannable step and a licence shortfall, so the plan is
        # non-trivial and still touches nothing.
        r = await client.post(
            "/objective/plan",
            json={
                "text": "fundamentals and a shared price",
                "target": "AAPL",
                "needs": ["fundamental_metric", "price_snapshot"],
                "entity_kind": "company",
                "shareable": True,
            },
        )
        after = {
            t: await db.pool.fetchval(f"SELECT count(*) FROM {t}") for t in tables
        }

    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["steps"]) == 1
    assert len(body["shortfalls"]) == 1
    for t in tables:
        assert after[t] == before[t], f"{t} changed during planning"


async def test_capabilities_lists_builtin_adapters_with_tiers(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/capabilities")

    assert r.status_code == 200, r.text
    caps = {c["name"]: c for c in r.json()["capabilities"]}

    # Price feeds are licensed per operator: byo_only.
    assert caps["polygon.aggregates"]["licence_tier"] == "byo_only"
    assert caps["coingecko.market_chart"]["licence_tier"] == "byo_only"
    # EDGAR fundamentals are redistributable.
    assert caps["edgar.companyfacts"]["licence_tier"] == "allowed"
    # FRED macro is redistributable.
    assert caps["fred.series"]["licence_tier"] == "allowed"

    for name, cap in caps.items():
        assert cap["description"], name
        assert "produces" in cap
        assert "entity_kinds" in cap
        assert "reliability" in cap  # None when uncalibrated, never absent


# ----------------------------------------------------------- /analysis/run


_D8_BASE = datetime(2024, 1, 1, tzinfo=UTC)
_D8_N = 100
_D8_SPIKE = 15


def _d8_gen(perc_delta: float, fact_delta: float, *, seed: int = 0):
    rng = np.random.default_rng(seed)
    base = 50.0 + rng.normal(0, 0.8, _D8_N)
    perc = base.copy()
    fact = base.copy()
    for i in range(_D8_N - _D8_SPIKE, _D8_N):
        k = (i - (_D8_N - _D8_SPIKE)) / _D8_SPIKE
        perc[i] += perc_delta * k
        fact[i] += fact_delta * k
    dates = [_D8_BASE + timedelta(days=i) for i in range(_D8_N)]
    return list(zip(dates, perc.tolist())), list(zip(dates, fact.tolist()))


_D8_INSERT_CLAIM = """
INSERT INTO claim (entity_id, claim_type, key, value, source,
                   event_date, knowledge_date, confidence,
                   redistributable, audience_user_id, derivation)
VALUES ($1,$2::claim_type,$3,$4::jsonb,$5,$6,$7,$8,
        $9::redistribution,$10,'ingested')
RETURNING id
"""


async def _d8_seed(db, entity_id, *, seed=0):
    perc_obs, fact_obs = _d8_gen(+20, -20, seed=seed)
    for obs, claim_type, key, source in (
        (perc_obs, "perception_macro", "vix", "fred"),
        (fact_obs, "fundamental_metric", "Revenues", "sec_edgar"),
    ):
        for event_date, value in obs:
            await db.pool.execute(
                _D8_INSERT_CLAIM, entity_id, claim_type, key,
                json.dumps({"value": value}), source,
                event_date, event_date + timedelta(days=1), 1.0,
                "allowed", None,
            )


async def test_analysis_run_returns_result_with_evidence_and_licence(
    db, database_url
):
    await _entity(db, symbol="AAPL")
    entity_id = await db.pool.fetchval(
        "SELECT id FROM entity WHERE symbol = 'AAPL'"
    )
    await _d8_seed(db, entity_id)

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _auth_headers(client)
        r = await client.post(
            "/analysis/run",
            json={"capability": "perception.divergence", "target": "AAPL"},
            headers=headers,
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["capability"] == "perception.divergence"
    assert body["abstained"] is False
    assert body["result"]["claim_type"] == "perception_divergence"
    assert "direction" in body["result"]["value"]
    assert "score" in body["result"]["value"]
    assert body["result"]["confidence"] > 0.0
    assert len(body["evidence"]) > 0
    assert body["licence"]["redistributable"] == "allowed"
    assert body["licence"]["audience_user_id"] is None


async def test_analysis_run_serializes_a_non_claim_declared_result(
    db, database_url
):
    """market_risk.credit_risk (QF1) returns a plain dict, not a ClaimDraft --
    the serializer's ClaimDraft-only unpacking would 500 on this path before
    the isinstance(draft, dict) branch was added. FRED-sourced spreads resolve
    shareable even though the static extracted.py entry stays touches_byo=True."""
    await _entity(db, symbol="AAPL")
    entity_id = await db.pool.fetchval(
        "SELECT id FROM entity WHERE symbol = 'AAPL'"
    )
    now = datetime(2024, 1, 1, tzinfo=UTC)
    for key, value in (("BAMLC0A0CM", 1.1), ("BAMLH0A0HYM2", 3.8)):
        await db.pool.execute(
            _D8_INSERT_CLAIM, entity_id, "macro_series_point", key,
            json.dumps({"value": value}), "fred",
            now, now, 1.0, "allowed", None,
        )

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _auth_headers(client)
        r = await client.post(
            "/analysis/run",
            json={"capability": "market_risk.credit_risk", "target": "AAPL"},
            headers=headers,
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["abstained"] is False
    # A plain dict, emitted as-is -- not unpacked as a ClaimDraft.
    assert "score" in body["result"]
    assert "claim_type" not in body["result"]
    assert body["licence"]["redistributable"] == "allowed"
    assert body["licence"]["audience_user_id"] is None


async def test_analysis_run_unknown_capability_is_not_found(db, database_url):
    await _entity(db, symbol="AAPL")
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _auth_headers(client)
        r = await client.post(
            "/analysis/run",
            json={"capability": "does.not.exist", "target": "AAPL"},
            headers=headers,
        )

    assert r.status_code == 404, r.text
    detail = r.json()
    assert "does.not.exist" in detail["detail"]


async def test_analysis_run_no_declared_arguments_is_bad_request(
    db, database_url
):
    await _entity(db, symbol="AAPL")
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _auth_headers(client)
        r = await client.post(
            "/analysis/run",
            json={
                "capability": "backtest.evaluate_strategy_sharpe",
                "target": "AAPL",
            },
            headers=headers,
        )

    assert r.status_code == 400, r.text
    detail = r.json()
    assert "declares no arguments" in detail["detail"]


async def test_analysis_run_writes_nothing_to_the_database(db, database_url):
    """The name-keyed path returns a result; it does not persist a claim, a
    finding, or a gap. A claim count before and after proves it."""
    await _entity(db, symbol="AAPL")
    entity_id = await db.pool.fetchval(
        "SELECT id FROM entity WHERE symbol = 'AAPL'"
    )
    await _d8_seed(db, entity_id)

    tables = ("claim", "gap", "fill_attempt", "finding")
    before = {
        t: await db.pool.fetchval(f"SELECT count(*) FROM {t}") for t in tables
    }

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _auth_headers(client)
        r = await client.post(
            "/analysis/run",
            json={"capability": "perception.divergence", "target": "AAPL"},
            headers=headers,
        )

    assert r.status_code == 200, r.text
    after = {
        t: await db.pool.fetchval(f"SELECT count(*) FROM {t}") for t in tables
    }
    for t in tables:
        assert after[t] == before[t], f"{t} changed during analysis run"
