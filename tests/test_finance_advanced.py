"""Splits, date rules, and bank-sync endpoint behaviour (honest failure
without credentials; encrypted credential round-trip)."""

from __future__ import annotations

import pytest
from neutron.test import TestClient

from omni.credentials import keyring
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
async def _clean(db, monkeypatch, tmp_path):
    monkeypatch.setenv(keyring.KEY_PATH_ENV, str(tmp_path / "credential.key"))
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


async def _operator(client, email="op@example.com") -> dict:
    r = await client.post(
        "/auth/setup", json={"email": email, "password": "a" * 16}
    )
    assert r.status_code in (200, 201), r.text
    return {"authorization": f"Bearer {r.json()['token']}"}


async def _setup(client, headers) -> dict:
    r = await client.post(
        "/finance/accounts", json={"name": "Checking", "type": "checking"}, headers=headers
    )
    account_id = r.json()["id"]
    r = await client.post(
        "/finance/categories", json={"name": "Groceries"}, headers=headers
    )
    groceries = r.json()["id"]
    r = await client.post(
        "/finance/categories", json={"name": "Household"}, headers=headers
    )
    household = r.json()["id"]
    return {"account": account_id, "groceries": groceries, "household": household}


async def test_split_transaction_children_carry_categories(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)

        r = await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": "2026-01-05",
            "amount": -9000,
            "payee": "Big Store",
            "splits": [
                {"amount": -6000, "category_id": ids["groceries"]},
                {"amount": -3000, "category_id": ids["household"]},
            ],
        }, headers=headers)
        assert r.status_code in (200, 201), r.text

        pool = app.db.pool
        parent = await pool.fetchrow(
            "SELECT id, is_parent, category_id FROM finance_transaction "
            "WHERE is_parent AND NOT deleted"
        )
        children = await pool.fetch(
            "SELECT amount, category_id FROM finance_transaction "
            "WHERE parent_id = $1 ORDER BY amount",
            parent["id"],
        )
        assert parent["category_id"] is None
        assert len(children) == 2
        assert [c["amount"] for c in children] == [-6000, -3000]

        r = await client.get("/finance/budget", params={"month": "2026-01"}, headers=headers)
        body = r.json()
        by_name = {c["name"]: c for c in body["categories"]}
        assert by_name["Groceries"]["activity"] == -6000
        assert by_name["Household"]["activity"] == -3000


async def test_split_summing_mismatch_is_refused(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        r = await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": "2026-01-05",
            "amount": -9000,
            "payee": "Big Store",
            "splits": [{"amount": -6000}],
        }, headers=headers)
        assert r.status_code == 400
        assert "sum to -6000" in r.text


async def test_split_parent_shown_and_children_flagged(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": "2026-01-05",
            "amount": -9000,
            "payee": "Big Store",
            "splits": [
                {"amount": -6000, "category_id": ids["groceries"]},
                {"amount": -3000, "category_id": ids["household"]},
            ],
        }, headers=headers)
        txs = (await client.get(
            "/finance/transactions", params={"month": "2026-01"}, headers=headers
        )).json()["transactions"]
        parents = [t for t in txs if t["split"]]
        children = [t for t in txs if t["split_child"]]
        assert len(parents) == 1 and parents[0]["amount"] == -9000
        assert len(children) == 2


async def test_ofx_import_through_the_endpoint(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        ofx = (
            "OFXHEADER:100\nDATA:OFXSGML\n\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>"
            "<BANKTRANLIST>"
            "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260115<TRNAMT>-42.10"
            "<FITID>TX-001<NAME>KROGER<MEMO>run</STMTTRN>"
            "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
        )
        r = await client.post("/finance/import", json={
            "account_id": ids["account"], "csv": ofx,
        }, headers=headers)
        assert r.status_code in (200, 201), r.text
        assert len(r.json()["added"]) == 1

        r = await client.post("/finance/import", json={
            "account_id": ids["account"], "csv": ofx,
        }, headers=headers)
        second = r.json()
        assert second["added"] == [] and second["updated"] == []


async def test_bank_endpoints_fail_honestly_without_credentials(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        r = await client.get("/finance/bank/credentials", headers=headers)
        assert r.status_code == 200
        providers = {p["key"]: p["configured"] for p in r.json()["providers"]}
        assert providers == {"gocardless": False, "simplefin": False}

        r = await client.get("/finance/bank/institutions", headers=headers)
        assert r.status_code == 400
        assert "credentials not configured" in r.text

        r = await client.post("/finance/bank/sync", json={}, headers=headers)
        assert r.status_code == 400
        assert "no bank accounts linked" in r.text

        r = await client.post(
            "/finance/bank/simplefin/link", headers=headers
        )
        assert r.status_code == 400
        assert "credentials not configured" in r.text


async def test_bank_credentials_round_trip_stores_encrypted(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        r = await client.put("/finance/bank/credentials", json={
            "provider": "gocardless",
            "fields": {"secret_id": "sid-1", "secret_key": "sk-1"},
        }, headers=headers)
        assert r.status_code == 200, r.text

        stored = await app.db.pool.fetchval(
            """
            SELECT data->'finance_bank_keys'->'gocardless'
            FROM user_settings
            """
        )
        assert "sid-1" not in str(stored)
        assert "sk-1" not in str(stored)

        r = await client.get("/finance/bank/credentials", headers=headers)
        providers = {p["key"]: p["configured"] for p in r.json()["providers"]}
        assert providers["gocardless"] is True

        r = await client.delete(
            "/finance/bank/credentials/gocardless", headers=headers
        )
        assert r.status_code in (200, 204)
        r = await client.get("/finance/bank/credentials", headers=headers)
        providers = {p["key"]: p["configured"] for p in r.json()["providers"]}
        assert providers["gocardless"] is False


async def test_date_rules_month_approx_and_between(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        r = await client.post("/finance/rules", json={
            "conditions": [
                {"field": "date", "op": "isapprox", "value": "2026-01-15"},
                {"field": "amount", "op": "lt", "value": 0},
            ],
            "actions": [{"field": "category", "value": "Groceries"}],
        }, headers=headers)
        assert r.status_code in (200, 201), r.text
        r = await client.post("/finance/rules", json={
            "conditions": [
                {
                    "field": "date",
                    "op": "isbetween",
                    "value": ["2026-02-01", "2026-02-28"],
                }
            ],
            "actions": [{"field": "notes", "value": "february"}],
        }, headers=headers)
        assert r.status_code in (200, 201), r.text

        await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": "2026-01-20",
            "amount": -1000,
            "payee": "Any Store",
        }, headers=headers)
        txs = (await client.get(
            "/finance/transactions", params={"month": "2026-01"}, headers=headers
        )).json()["transactions"]
        assert txs[0]["category"] == "Groceries"

        await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": "2026-02-10",
            "amount": -500,
            "payee": "Any Store",
        }, headers=headers)
        feb = (await client.get(
            "/finance/transactions", params={"month": "2026-02"}, headers=headers
        )).json()["transactions"]
        assert feb[0]["notes"] == "february"


async def test_listed_rules_carry_arrays_whatever_the_codec(db, database_url):
    """U21: GET /finance/rules must return conditions/actions as arrays of
    objects. Under asyncpg's default JSONB codec the raw columns are strings,
    and the UI indexing r.conditions[0] would get a character."""
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        r = await client.post("/finance/rules", json={
            "conditions": [{"field": "amount", "op": "lt", "value": 0}],
            "actions": [{"field": "notes", "value": "neg"}],
        }, headers=headers)
        assert r.status_code in (200, 201), r.text

        r = await client.get("/finance/rules", headers=headers)
        assert r.status_code == 200, r.text
        rules = r.json()["rules"]
        assert len(rules) == 1
        assert isinstance(rules[0]["conditions"], list)
        assert isinstance(rules[0]["conditions"][0], dict)
        assert rules[0]["conditions"][0]["field"] == "amount"
        assert isinstance(rules[0]["actions"], list)
        assert rules[0]["actions"][0]["value"] == "neg"
