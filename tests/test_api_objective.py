"""Objective API: the agentic surface.

The redistribution rule surfaces here through HTTP: no price source is
redistributable, so a shareable objective needing prices is refused with
``only_licensed_sources_can_produce_this`` -- the licence decision made in the
planner reaching the caller as a structured shortfall rather than a failed
write. The demand rule is the other thing these tests defend: a missing
producer becomes demand (the system's work queue), but a licensing refusal
does not, because fetching it again would not help.
"""

import asyncio

import pytest
from neutron.test import TestClient

from omni.api.objective import build_router
from omni.main import create_app


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
    await db.pool.execute("TRUNCATE entity CASCADE")
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
        before = await db.pool.fetchval("SELECT count(*) FROM demand")
        r = await client.post(
            "/objective/run",
            json={
                "text": "positioning for AAPL",
                "target": "AAPL",
                "needs": [claim_type],
            },
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
        before = await db.pool.fetchval("SELECT count(*) FROM demand")
        r = await client.post(
            "/objective/run",
            json={
                "text": "shared price",
                "target": "AAPL",
                "needs": ["price_snapshot"],
                "shareable": True,
            },
        )
        after = await db.pool.fetchval("SELECT count(*) FROM demand")

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["shortfalls"][0]["reason"] == "only_licensed_sources_can_produce_this"
    assert body["demand_raised"] == []
    assert after == before


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
