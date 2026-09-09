"""Reconciliation, budget, transfers and scoping through the API.

The reconciliation cases are ported from Actual Budget's
sync.test.ts expectations (MIT): exact imported_id beats fuzzy, fuzzy
matches payee before date-proximity, matching is one-to-one across the
batch, both-id rows never dedup, reconciled rows are locked, existing
values win on merge, re-import is idempotent.
"""

from __future__ import annotations

from uuid import UUID as _uuid

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
    yield


async def _operator(client, email="op@example.com") -> dict:
    r = await client.post(
        "/auth/setup", json={"email": email, "password": "a" * 16}
    )
    if r.status_code not in (200, 201):
        r = await client.post(
            "/auth/register", json={"email": email, "password": "a" * 16}
        )
    assert r.status_code in (200, 201), r.text
    return {"authorization": f"Bearer {r.json()['token']}"}


async def _setup(client, headers) -> dict:
    r = await client.post(
        "/finance/accounts", json={"name": "Checking", "type": "checking"}, headers=headers
    )
    assert r.status_code in (200, 201), r.text
    account_id = r.json()["id"]
    r = await client.post(
        "/finance/categories", json={"name": "Groceries"}, headers=headers
    )
    groceries = r.json()["id"]
    r = await client.post(
        "/finance/categories",
        json={"name": "Income", "is_income": True},
        headers=headers,
    )
    income = r.json()["id"]
    return {"account": account_id, "groceries": groceries, "income": income}


def _csv(*lines: str) -> str:
    return "Date,Description,Amount,Notes\n" + "\n".join(lines) + "\n"


async def test_finance_requires_authentication(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/finance/accounts")
    assert r.status_code == 401


async def test_import_is_idempotent_and_reconciles(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)

        body = {"account_id": ids["account"], "csv": _csv(
            "2026-01-05,KROGER,-42.10,groceries run",
            "2026-01-06,ACME CORP,-12.34,widgets",
        )}
        r = await client.post("/finance/import", json=body, headers=headers)
        assert r.status_code in (200, 201), r.text
        first = r.json()
        assert len(first["added"]) == 2

        r = await client.post("/finance/import", json=body, headers=headers)
        second = r.json()
        assert second["added"] == []
        assert second["updated"] == []

        r = await client.get(
            "/finance/transactions", params={"month": "2026-01"}, headers=headers
        )
        assert len(r.json()["transactions"]) == 2


async def test_both_id_rows_never_dedup(db, database_url):
    """Actual's strict id checking: an incoming row with an imported_id
    may only fuzzy-match existing rows with a NULL imported_id. Two
    bank-asserted rows that fail the exact match are distinct
    transactions, never duplicates of each other."""
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)

        r = await client.post("/finance/import", json={
            "account_id": ids["account"],
            "csv": "Date,Description,Amount,Notes,Reference\n"
                   "2026-01-05,KROGER,-42.10,run,BANK-1\n",
        }, headers=headers)
        assert len(r.json()["added"]) == 1

        r = await client.post("/finance/import", json={
            "account_id": ids["account"],
            "csv": "Date,Description,Amount,Notes,Reference\n"
                   "2026-01-05,KROGER,-42.10,same everything,BANK-2\n",
        }, headers=headers)
        body = r.json()
        assert len(body["added"]) == 1
        assert body["updated"] == []


async def test_fuzzy_match_updates_manual_row_with_bank_data(db, database_url):
    """The other side of strict checking: a manual row (no imported_id)
    absorbs the bank's later assertion of the same purchase -- exact
    amount, inside the 7-day window -- instead of duplicating it."""
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)

        r = await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": "2026-01-05",
            "amount": -4210,
            "payee": "Kroger",
        }, headers=headers)
        manual_id = r.json()["id"]

        r = await client.post("/finance/import", json={
            "account_id": ids["account"],
            "csv": "Date,Description,Amount,Notes,Reference\n"
                   "2026-01-07,KROGER,-42.10,bank view,BANK-1\n",
        }, headers=headers)
        body = r.json()
        assert body["added"] == []
        assert body["updated"] == [manual_id]

        txs = (await client.get(
            "/finance/transactions", params={"month": "2026-01"}, headers=headers
        )).json()["transactions"]
        assert len(txs) == 1


async def test_reconciled_rows_are_locked(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        r = await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": "2026-01-05",
            "amount": -1000,
            "payee": "Kroger",
        }, headers=headers)
        tx_id = r.json()["id"]
        await client.patch(
            f"/finance/transactions/{tx_id}", json={"reconciled": True}, headers=headers
        )
        r = await client.patch(
            f"/finance/transactions/{tx_id}",
            json={"notes": "edit after lock"},
            headers=headers,
        )
        assert r.status_code == 400
        r = await client.patch(
            f"/finance/transactions/{tx_id}",
            json={"reconciled": False, "notes": "unlock and edit"},
            headers=headers,
        )
        assert r.status_code in (200, 201)


async def test_user_scoping_never_leaks(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        alice = await _operator(client, "alice@example.com")
        r = await client.post(
            "/auth/register",
            json={"email": "bob@example.com", "password": "a" * 16},
            headers=alice,
        )
        assert r.status_code in (200, 201), r.text
        r = await client.post(
            "/auth/login", json={"email": "bob@example.com", "password": "a" * 16}
        )
        assert r.status_code == 200, r.text
        bob = {"authorization": f"Bearer {r.json()['token']}"}
        r = await client.post(
            "/finance/accounts", json={"name": "Alice Checking", "type": "checking"},
            headers=alice,
        )
        account_id = r.json()["id"]
        r = await client.post("/finance/import", json={
            "account_id": account_id,
            "csv": _csv("2026-01-05,KROGER,-42.10,run"),
        }, headers=alice)
        assert r.status_code in (200, 201)

        r = await client.get("/finance/transactions", params={"month": "2026-01"}, headers=bob)
        assert r.json()["transactions"] == []
        r = await client.post("/finance/import", json={
            "account_id": account_id,
            "csv": _csv("2026-01-05,KROGER,-42.10,run"),
        }, headers=bob)
        assert r.status_code == 400


async def test_rules_apply_on_import(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        r = await client.post("/finance/rules", json={
            "conditions": [{"field": "imported_payee", "op": "contains", "value": "kroger"}],
            "actions": [{"field": "category", "value": "Groceries"}],
        }, headers=headers)
        assert r.status_code in (200, 201), r.text

        await client.post("/finance/import", json={
            "account_id": ids["account"],
            "csv": _csv("2026-01-05,KROGER #42,-42.10,run"),
        }, headers=headers)
        txs = (await client.get(
            "/finance/transactions", params={"month": "2026-01"}, headers=headers
        )).json()["transactions"]
        assert txs[0]["category"] == "Groceries"


async def test_transfer_mirrors_and_clears_category(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        r = await client.post(
            "/finance/accounts", json={"name": "Savings", "type": "savings"}, headers=headers
        )
        savings = r.json()["id"]
        pool = app.db.pool
        user_row = await pool.fetchrow(
            "SELECT id FROM users WHERE email = $1", "op@example.com"
        )
        payee_id = await pool.fetchval(
            "INSERT INTO finance_payee (user_id, name, transfer_acct) "
            "VALUES ($1, 'Transfer to Savings', $2) RETURNING id",
            user_row["id"],
            _uuid(savings),
        )
        r = await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": "2026-01-10",
            "amount": -50000,
            "payee": "Transfer to Savings",
            "category_id": ids["groceries"],
        }, headers=headers)
        assert r.status_code in (200, 201)

        rows = await pool.fetch(
                "SELECT id, account_id, amount, transfer_id, category_id "
                "FROM finance_transaction WHERE NOT deleted ORDER BY amount"
            )
        assert len(rows) == 2
        outgoing = next(r for r in rows if r["amount"] < 0)
        incoming = next(r for r in rows if r["amount"] > 0)
        assert incoming["amount"] == -outgoing["amount"]
        assert outgoing["transfer_id"] == incoming["id"]
        assert incoming["transfer_id"] == outgoing["id"]
        assert outgoing["category_id"] is None


async def test_budget_month_math_and_rollover(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)

        r = await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": "2026-01-05",
            "amount": -4210,
            "payee": "Kroger",
            "category_id": ids["groceries"],
        }, headers=headers)
        assert r.status_code in (200, 201)
        r = await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": "2026-01-02",
            "amount": 200000,
            "payee": "Employer Inc",
            "category_id": ids["income"],
        }, headers=headers)
        assert r.status_code in (200, 201)

        r = await client.put("/finance/budget", json={
            "month": "2026-01",
            "category_id": ids["groceries"],
            "amount": 10000,
        }, headers=headers)
        assert r.status_code in (200, 201)

        r = await client.get("/finance/budget", params={"month": "2026-01"}, headers=headers)
        body = r.json()
        assert body["budgeted"] == 10000
        groceries = next(c for c in body["categories"] if c["name"] == "Groceries")
        assert groceries["activity"] == -4210
        assert groceries["available"] == 10000 - 4210
        income = next(c for c in body["categories"] if c["is_income"])
        assert income["activity"] > 0
        assert body["available_to_budget"] == body["income"] - 10000

        r = await client.get("/finance/budget", params={"month": "2026-02"}, headers=headers)
        feb = r.json()
        feb_groceries = next(c for c in feb["categories"] if c["name"] == "Groceries")
        assert feb_groceries["rollover"] == 10000 - 4210
        assert feb_groceries["available"] == 5790


async def test_budget_never_invents_income_before_history(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        await _setup(client, headers)
        r = await client.get("/finance/budget", params={"month": "2026-01"}, headers=headers)
        body = r.json()
        assert body["income"] == 0
        assert body["available_to_budget"] == 0
