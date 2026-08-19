"""The personal position tracker: manual holdings, owned, valued, honest.

The rules these pin:

- owner is the authenticated principal, never the body -- a second account
  cannot read, edit or remove another's holdings;
- valuation comes from the caller's audience-scoped claim store, never from
  the request, and a holding the store cannot price is `unpriced` with null
  value -- never a zero;
- a symbol the store has no entity for is refused on write: tracking a name
  with no data would fabricate coverage;
- the summary total is stated only when every holding priced, and total P&L
  only when every priced holding also carries a basis -- a partial sum is a
  smaller portfolio wearing the full one's name.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from neutron.test import TestClient

from omni.main import create_app


class _Lifespan:
    def __init__(self, app):
        import asyncio

        self.app = app
        self.receive = asyncio.Queue()
        self.send = asyncio.Queue()

    async def __aenter__(self):
        import asyncio

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
    await db.pool.execute("TRUNCATE users CASCADE")
    await db.pool.execute("TRUNCATE manual_holding, entity, claim CASCADE")
    yield


async def _seed_price(db, symbol: str, close: float, *, audience=None) -> None:
    eid = await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company',$1,$1) "
        "ON CONFLICT DO NOTHING RETURNING id",
        symbol,
    )
    if eid is None:
        eid = await db.pool.fetchval(
            "SELECT id FROM entity WHERE symbol = $1", symbol
        )
    at = datetime(2026, 8, 1, tzinfo=UTC)
    await db.pool.execute(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1,'price_snapshot','close',$2::jsonb,'polygon',$3,$3,1.0,
                $4, $5)
        """,
        eid, json.dumps({"close": close}), at,
        "byo_only" if audience is not None else "allowed",
        audience,
    )


async def _operator(client) -> dict:
    r = await client.post(
        "/auth/setup", json={"email": "op@example.com", "password": "a" * 16}
    )
    assert r.status_code == 200, r.text
    return {"authorization": f"Bearer {r.json()['token']}"}


async def test_holdings_require_authentication(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/holdings")
    assert r.status_code == 401


async def test_a_holding_is_valued_from_the_visible_store(db, database_url):
    await _seed_price(db, "AAPL", 100.0)
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        r = await client.post(
            "/holdings",
            json={"symbol": "aapl", "quantity": "10", "cost_basis": "800"},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["symbol"] == "AAPL"
        assert body["valuation"] == "priced"
        assert body["last_price"] == pytest.approx(100.0)
        assert body["value"] == pytest.approx(1000.0)
        assert body["unrealized_pnl"] == pytest.approx(200.0)

        listing = await client.get("/holdings", headers=headers)
        summary = listing.json()["summary"]
        assert summary["positions"] == 1
        assert summary["priced"] == 1
        assert summary["total_value"] == pytest.approx(1000.0)
        assert summary["total_pnl"] == pytest.approx(200.0)


async def test_a_holding_the_store_cannot_price_is_unpriced_not_zero(
    db, database_url
):
    await db.pool.execute(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company','ZZZ','Z')"
    )
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        r = await client.post(
            "/holdings", json={"symbol": "ZZZ", "quantity": "5"}, headers=headers
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["valuation"] == "unpriced"
        assert body["value"] is None
        assert body["last_price"] is None

        listing = await client.get("/holdings", headers=headers)
        summary = listing.json()["summary"]
        assert summary["positions"] == 1
        assert summary["priced"] == 0
        assert summary["total_value"] is None, (
            "a portfolio with an unpriced holding has no honest total"
        )


async def test_an_unknown_symbol_is_refused_rather_than_tracked(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        r = await client.post(
            "/holdings", json={"symbol": "NOSUCH", "quantity": "1"}, headers=headers
        )
    assert r.status_code == 400
    assert "cannot price" in r.json()["detail"]


async def test_holdings_are_scoped_to_their_owner(db, database_url):
    await _seed_price(db, "AAPL", 100.0)
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        operator = await _operator(client)
        await client.post(
            "/auth/register",
            json={"email": "member@example.com", "password": "b" * 16},
            headers=operator,
        )
        login = await client.post(
            "/auth/login", json={"email": "member@example.com", "password": "b" * 16}
        )
        member = {"authorization": f"Bearer {login.json()['token']}"}

        created = await client.post(
            "/holdings", json={"symbol": "AAPL", "quantity": "2"}, headers=operator
        )
        holding_id = created.json()["id"]

        sees = await client.get("/holdings", headers=member)
        assert sees.json()["holdings"] == []

        patched = await client.patch(
            f"/holdings/{holding_id}", json={"quantity": "99"}, headers=member
        )
        assert patched.status_code == 404

        removed = await client.delete(f"/holdings/{holding_id}", headers=member)
        assert removed.status_code == 404


async def test_an_edit_updates_quantity_and_a_delete_removes(db, database_url):
    await _seed_price(db, "AAPL", 100.0)
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        created = await client.post(
            "/holdings", json={"symbol": "AAPL", "quantity": "1"}, headers=headers
        )
        holding_id = created.json()["id"]

        edited = await client.patch(
            f"/holdings/{holding_id}",
            json={"quantity": "3", "cost_basis": "250"},
            headers=headers,
        )
        assert edited.json()["quantity"] == pytest.approx(3.0)
        assert edited.json()["unrealized_pnl"] == pytest.approx(50.0)

        gone = await client.delete(f"/holdings/{holding_id}", headers=headers)
        assert gone.status_code == 204
        listing = await client.get("/holdings", headers=headers)
        assert listing.json()["holdings"] == []


async def test_a_nonpositive_or_nonnumeric_quantity_is_refused(db, database_url):
    await _seed_price(db, "AAPL", 100.0)
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        for bad in ("-5", "0", "not-a-number"):
            r = await client.post(
                "/holdings", json={"symbol": "AAPL", "quantity": bad}, headers=headers
            )
            assert r.status_code == 400, bad


async def test_readding_a_symbol_updates_it_instead_of_duplicating(
    db, database_url
):
    await _seed_price(db, "AAPL", 100.0)
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        await client.post(
            "/holdings", json={"symbol": "AAPL", "quantity": "1"}, headers=headers
        )
        await client.post(
            "/holdings", json={"symbol": "AAPL", "quantity": "4"}, headers=headers
        )
        listing = await client.get("/holdings", headers=headers)
    assert len(listing.json()["holdings"]) == 1
    assert listing.json()["holdings"][0]["quantity"] == pytest.approx(4.0)
