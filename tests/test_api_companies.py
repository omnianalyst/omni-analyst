"""Companies rank on the same axes as everything else, and stay separate.

The risk this guards is not a crash. It is the page reading as an endorsement:
the ETF-versus-constituent experiment measured a ranker over these names and it
failed, so the ranking must carry that verdict with it, and must not be folded
into the core categories where it would inherit their standing.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from neutron.test import TestClient

from omni.api.companies import MIN_SESSIONS, _balanced
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
async def _clean(db):
    from omni.api import companies as mod
    mod._cache.clear()
    await db.pool.execute("TRUNCATE users CASCADE")
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield
    mod._cache.clear()


async def _company(db, symbol, name, *, sessions, start_price, daily_vol, drift=0.0005):
    import numpy as np

    eid = await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company',$1,$2) RETURNING id",
        symbol, name,
    )
    rng = np.random.default_rng(abs(hash(symbol)) % (2**31))
    steps = rng.normal(drift, daily_vol, size=sessions).cumsum()
    prices = start_price * np.exp(steps)
    base = datetime(2024, 1, 1, tzinfo=UTC)
    for i, price in enumerate(prices):
        await db.pool.execute(
            """
            INSERT INTO claim (entity_id, claim_type, key, value, source,
                               event_date, knowledge_date, confidence, redistributable)
            VALUES ($1,'price_snapshot','close',$2::jsonb,'polygon',$3,$3,1.0,'allowed')
            """,
            eid, json.dumps({"value": float(price)}), base + timedelta(days=i),
        )
    return eid


async def _setup(client):
    r = await client.post("/auth/setup",
                          json={"email": "operator@example.com", "password": "a" * 16})
    assert r.status_code == 200, r.text
    return {"authorization": f"Bearer {r.json()['token']}"}


async def test_companies_require_authentication(database_url):
    """Company prices are byo_only; an anonymous caller is not entitled to them."""
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        response = await client.get("/scanner/companies")
    assert response.status_code == 401


async def test_a_volatile_company_reaches_the_high_risk_tier(database_url, db):
    """The reason companies were added: the diversified categories cannot.

    28 broad stock ETFs top out at 27.1% annualised volatility against a 30%
    cut, so `high` was structurally unreachable there. An individual company
    clears it easily, and the census must show that rather than a zero.
    """
    await _company(db, "CALM", "Steady Co", sessions=400, start_price=100, daily_vol=0.004)
    await _company(db, "WILD", "Volatile Co", sessions=400, start_price=50, daily_vol=0.045)

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        response = await client.get("/scanner/companies", headers=headers)

    body = response.json()
    tiers = {c["symbol"]: c["risk_tier"] for c in body["companies"]}
    assert tiers["WILD"] == "high", f"got {tiers} — a 4.5%/day name must rank high"
    assert body["risk_census"]["high"] >= 1


async def test_a_company_with_too_little_history_is_counted_not_ranked(database_url, db):
    """Thin coverage is reported, never silently dropped."""
    await _company(db, "FULL", "Full History", sessions=300, start_price=100, daily_vol=0.02)
    await _company(db, "THIN", "Thin History", sessions=MIN_SESSIONS - 10,
                   start_price=100, daily_vol=0.02)

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        body = (await client.get("/scanner/companies", headers=headers)).json()

    ranked = {c["symbol"] for c in body["companies"]}
    assert "FULL" in ranked
    assert "THIN" not in ranked
    assert body["coverage"]["too_thin"] == 1
    assert body["coverage"]["with_prices"] == 2


async def test_the_payload_carries_the_verdict_that_the_ranker_failed(database_url, db):
    """Without this the ranking reads as an endorsement it has not earned."""
    await _company(db, "AAA", "A Co", sessions=300, start_price=100, daily_vol=0.02)

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        body = (await client.get("/scanner/companies", headers=headers)).json()

    standing = body["standing"]
    assert "ETFs remain the default core" in standing["verdict"]
    assert "3 of 9" in standing["verdict"]
    assert "not a recommendation" in standing["scope"]


async def test_companies_are_not_folded_into_the_core_category_rankings(database_url, db):
    """Separation is structural: /scanner/market must not gain a company list."""
    await _company(db, "AAA", "A Co", sessions=300, start_price=100, daily_vol=0.02)

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        market = await client.get("/scanner/market", headers=headers)

    if market.status_code == 200:
        categories = market.json().get("category_rankings", {})
        assert set(categories) == {"stocks", "defensive", "crypto"}
        for entries in categories.values():
            assert all(e.get("asset_class") != "company" for e in entries)


def test_the_balanced_score_reweights_over_available_components():
    """A name missing long-history components is scored on what it has, not
    penalised to zero for data it never had a chance to carry."""
    entries = [
        {"symbol": "A", "cagr_5y": 12.0, "positive_year_rate": 80.0,
         "volatility": 20.0, "return_365d": 10.0, "max_drawdown": -15.0},
        {"symbol": "B", "cagr_5y": None, "positive_year_rate": None,
         "volatility": 25.0, "return_365d": 5.0, "max_drawdown": -20.0},
    ]

    _balanced(entries)

    assert entries[0]["scores"]["balanced"] is not None
    assert entries[1]["scores"]["balanced"] is not None
    assert entries[1]["scores"]["components_available"] < entries[0]["scores"]["components_available"]


def test_an_empty_population_does_not_raise():
    entries: list[dict] = []
    _balanced(entries)
    assert entries == []
