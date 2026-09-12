"""API-level parity: rollover modes, schedules endpoints, reports,
starting balances, search, tombstone-by-rule on import."""

from __future__ import annotations

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
async def _clean(db, monkeypatch, tmp_path):
    from omni.credentials import keyring

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
        "/finance/accounts",
        json={"name": "Checking", "type": "checking", "opening_balance_cents": 50000},
        headers=headers,
    )
    account_id = r.json()["id"]
    r = await client.post(
        "/finance/categories", json={"name": "Groceries"}, headers=headers
    )
    groceries = r.json()["id"]
    return {"account": account_id, "groceries": groceries}


async def test_opening_balance_books_under_starting_balances(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        await _setup(client, headers)
        txs = (await client.get(
            "/finance/transactions", params={"month": date_month()}, headers=headers
        )).json()["transactions"]
        opening = [t for t in txs if t["payee"] == "Starting Balance"]
        assert len(opening) == 1 and opening[0]["amount"] == 50000

        budget = (await client.get(
            "/finance/budget", params={"month": date_month()}, headers=headers
        )).json()
        income = [c for c in budget["categories"] if c["is_income"]]
        assert any(c["activity"] == 50000 for c in income)


def date_month() -> str:

    return f"{datetime.now(UTC).date().year}-{str(datetime.now(UTC).date().month).zfill(2)}"


async def test_rollover_modes_reset_and_hold(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        this_month = date_month()

        from datetime import date

        next_m = date(
            datetime.now(UTC).date().year + (datetime.now(UTC).date().month == 12),
            datetime.now(UTC).date().month % 12 + 1,
            1,
        ).strftime("%Y-%m")

        await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": f"{this_month}-05",
            "amount": -2000,
            "payee": "Kroger",
            "category_id": ids["groceries"],
        }, headers=headers)
        await client.put("/finance/budget", json={
            "month": this_month,
            "category_id": ids["groceries"],
            "amount": 10000,
            "rollover_mode": "reset",
        }, headers=headers)

        nxt = (await client.get(
            "/finance/budget", params={"month": next_m}, headers=headers
        )).json()
        groceries = next(c for c in nxt["categories"] if c["name"] == "Groceries")
        assert groceries["rollover"] == 0

        await client.put("/finance/budget", json={
            "month": this_month,
            "category_id": ids["groceries"],
            "amount": 10000,
            "rollover_mode": "hold",
        }, headers=headers)
        nxt = (await client.get(
            "/finance/budget", params={"month": next_m}, headers=headers
        )).json()
        groceries = next(c for c in nxt["categories"] if c["name"] == "Groceries")
        assert groceries["rollover"] == 0

        await client.put("/finance/budget", json={
            "month": this_month,
            "category_id": ids["groceries"],
            "amount": 10000,
            "rollover_mode": "rollover",
        }, headers=headers)
        nxt = (await client.get(
            "/finance/budget", params={"month": next_m}, headers=headers
        )).json()
        groceries = next(c for c in nxt["categories"] if c["name"] == "Groceries")
        assert groceries["rollover"] == 8000


async def test_schedules_crud_and_status(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        await _setup(client, headers)

        r = await client.post("/finance/schedules", json={
            "name": "Rent",
            "payee": "Landlord",
            "amount": -120000,
            "config": {
                "frequency": "monthly",
                "start": "2026-01-01",
                "patterns": [{"type": "dayOfMonth", "value": 1}],
            },
        }, headers=headers)
        assert r.status_code in (200, 201), r.text
        schedule_id = r.json()["id"]

        r = await client.get("/finance/schedules", headers=headers)
        schedules = r.json()["schedules"]
        assert len(schedules) == 1
        assert schedules[0]["name"] == "Rent"
        assert schedules[0]["status"] in {"upcoming", "due", "missed", "paid"}

        r = await client.patch(
            f"/finance/schedules/{schedule_id}",
            json={"active": False},
            headers=headers,
        )
        assert r.status_code in (200, 204)
        schedules = (await client.get("/finance/schedules", headers=headers)).json()["schedules"]
        assert schedules[0]["status"] == "inactive"

        r = await client.delete(
            f"/finance/schedules/{schedule_id}", headers=headers
        )
        assert r.status_code in (200, 204)
        assert (await client.get("/finance/schedules", headers=headers)).json()["schedules"] == []

        r = await client.post("/finance/schedules", json={
            "name": "Broken",
            "config": {"frequency": "fortnightly"},
        }, headers=headers)
        assert r.status_code == 400


async def test_reports_math(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        this_month = date_month()

        await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": f"{this_month}-05",
            "amount": -3000,
            "payee": "Kroger",
            "category_id": ids["groceries"],
        }, headers=headers)

        cash = (await client.get(
            "/finance/reports/cash-flow", params={"months": 3}, headers=headers
        )).json()
        current = next(m for m in cash["months"] if m["month"].startswith(this_month))
        assert current["expense"] == 3000 + 0 or current["expense"] >= 3000
        assert current["income"] >= 50000

        spending = (await client.get(
            "/finance/reports/spending", params={"month": this_month}, headers=headers
        )).json()
        groceries = next(c for c in spending["categories"] if c["category"] == "Groceries")
        assert groceries["total"] == -3000

        worth = (await client.get(
            "/finance/reports/net-worth", params={"months": 6}, headers=headers
        )).json()
        latest = [m for m in worth["months"] if m["net_worth"] is not None][-1]
        assert latest["net_worth"] == 50000 - 3000


async def test_search_filters_transactions(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        this_month = date_month()
        await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": f"{this_month}-05",
            "amount": -1000,
            "payee": "Kroger",
        }, headers=headers)
        await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": f"{this_month}-06",
            "amount": -2000,
            "payee": "Shell",
        }, headers=headers)

        txs = (await client.get(
            "/finance/transactions",
            params={"month": this_month, "q": "kroger"},
            headers=headers,
        )).json()["transactions"]
        assert len(txs) == 1 and txs[0]["payee"] == "Kroger"


async def test_rule_tombstones_rows_on_import(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        this_month = date_month()
        r = await client.post("/finance/rules", json={
            "conditions": [{"field": "imported_payee", "op": "contains", "value": "fee"}],
            "actions": [{"op": "delete-transaction"}],
        }, headers=headers)
        assert r.status_code in (200, 201), r.text

        r = await client.post("/finance/import", json={
            "account_id": ids["account"],
            "csv": f"Date,Description,Amount\n{this_month}-05,Kroger,-10.00\n{this_month}-06,Transfer fee,-2.50\n",
        }, headers=headers)
        body = r.json()
        assert len(body["added"]) == 1
        assert any(p.get("ignored") == "deleted by rule" for p in body["preview"])
