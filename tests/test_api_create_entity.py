"""POST /entities -- the demand-driven path for names outside the seed.

What these tests defend: creation is authenticated, honest about what it does
(an entity plus demand, never data), idempotent on (kind, symbol), and raises
exactly the claim types its kind warrants -- no more, because demand beyond a
kind's honest set is noise dressed as coverage.
"""

import pytest
from neutron.test import TestClient

from omni.api.coverage import build_router
from omni.main import create_app

from test_api_coverage import _Lifespan, _make_app, _auth, _user


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def test_anonymous_creation_is_refused(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.post(
            "/entities", json={"symbol": "BOTZ", "kind": "etf"},
        )
    assert r.status_code == 401


async def test_creation_mints_the_entity_and_its_kinds_demand(
    db, database_url
):
    user = await _user(db)
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.post(
            "/entities",
            json={"symbol": "botz", "kind": "etf", "name": "Global X Robotics"},
            headers=_auth(user),
        )

    assert r.status_code == 201
    body = r.json()
    assert body["symbol"] == "BOTZ"
    assert body["name"] == "Global X Robotics"

    rows = await db.pool.fetch(
        "SELECT claim_type::text AS t FROM demand "
        "WHERE entity_id = $1 AND channel = 'direct' AND active",
        body["id"],
    )
    # An ETF's honest set is price alone: no fundamentals, no on-chain.
    assert sorted(r["t"] for r in rows) == ["price_snapshot"]
    # No claim was written -- attention was recorded, data was not invented.
    assert await db.pool.fetchval(
        "SELECT count(*) FROM claim WHERE entity_id = $1", body["id"],
    ) == 0


async def test_creation_is_idempotent_and_does_not_double_demand(
    db, database_url
):
    user = await _user(db)
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        first = await client.post(
            "/entities", json={"symbol": "BOTZ", "kind": "etf"},
            headers=_auth(user),
        )
        second = await client.post(
            "/entities", json={"symbol": "BOTZ", "kind": "etf"},
            headers=_auth(user),
        )

    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert await db.pool.fetchval(
        "SELECT count(*) FROM demand WHERE entity_id = $1 AND active",
        first.json()["id"],
    ) == 1

    # A second user tracking the same name IS a second unit of demand --
    # the ledger's weight rule, not duplication.
    other = await _user(db)
    async with _Lifespan(app), TestClient(app) as client:
        await client.post(
            "/entities", json={"symbol": "BOTZ", "kind": "etf"},
            headers=_auth(other),
        )
    assert await db.pool.fetchval(
        "SELECT count(*) FROM demand WHERE entity_id = $1 AND active",
        first.json()["id"],
    ) == 2


async def test_the_same_ticker_can_exist_as_two_kinds(db, database_url):
    """The QTUM case: a token and an ETF share a spelling. The seed held only
    the token; a user tracking the ETF must be able to create the other kind,
    and search must then return both for the operator to tell apart."""
    user = await _user(db)
    token = await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('crypto_asset', 'QTUM', 'Qtum') RETURNING id",
    )
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.post(
            "/entities", json={"symbol": "QTUM", "kind": "etf"},
            headers=_auth(user),
        )
        assert r.status_code == 201
        assert r.json()["id"] != str(token)

        found = await client.get("/entities", params={"q": "QTUM"})

    symbols = [(e["kind"], e["symbol"]) for e in found.json()["entities"]]
    assert ("crypto_asset", "QTUM") in symbols
    assert ("etf", "QTUM") in symbols


async def test_an_unknown_kind_is_refused_rather_than_minted(db, database_url):
    user = await _user(db)
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.post(
            "/entities", json={"symbol": "XYZ", "kind": "galaxy"},
            headers=_auth(user),
        )
    assert r.status_code == 400
    assert await db.pool.fetchval("SELECT count(*) FROM entity") == 0
