"""Goals, base currency, field mappings, and the Actual backup importer."""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime

import pytest
from neutron.test import TestClient

from omni.finance.banksync import gocardless_transactions_to_rows
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


def this_month() -> str:
    return f"{datetime.now(UTC).date().year}-{str(datetime.now(UTC).date().month).zfill(2)}"


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
    return {"account": account_id, "groceries": r.json()["id"]}


async def test_monthly_goal_needed_and_progress(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        r = await client.put(
            f"/finance/categories/{ids['groceries']}/goal",
            json={"goal_type": "monthly", "goal_amount": 20000},
            headers=headers,
        )
        assert r.status_code in (200, 201, 204), r.text

        await client.put("/finance/budget", json={
            "month": this_month(),
            "category_id": ids["groceries"],
            "amount": 15000,
        }, headers=headers)
        body = (await client.get(
            "/finance/budget", params={"month": this_month()}, headers=headers
        )).json()
        row = next(c for c in body["categories"] if c["name"] == "Groceries")
        assert row["goal"]["type"] == "monthly"
        assert row["goal"]["needed"] == 5000
        assert row["goal"]["progress"] == 0.75

        r = await client.delete(
            f"/finance/categories/{ids['groceries']}/goal", headers=headers
        )
        body = (await client.get(
            "/finance/budget", params={"month": this_month()}, headers=headers
        )).json()
        row = next(c for c in body["categories"] if c["name"] == "Groceries")
        assert row["goal"] is None


async def test_save_by_date_goal_spreads_target(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        ids = await _setup(client, headers)
        from datetime import date as d

        _today = datetime.now(UTC).date()
        horizon = d(_today.year + 1, _today.month, 1).isoformat()[:10]
        r = await client.put(
            f"/finance/categories/{ids['groceries']}/goal",
            json={"goal_type": "save_by_date", "goal_amount": 120000, "goal_date": horizon},
            headers=headers,
        )
        assert r.status_code in (200, 201, 204), r.text

        body = (await client.get(
            "/finance/budget", params={"month": this_month()}, headers=headers
        )).json()
        row = next(c for c in body["categories"] if c["name"] == "Groceries")
        assert row["goal"]["type"] == "save_by_date"
        assert row["goal"]["months_left"] >= 11
        assert row["goal"]["needed_per_month"] <= 12000
        assert row["goal"]["progress"] == 0


async def test_base_currency_round_trip_and_budget_filtering(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        r = await client.get("/finance/base-currency", headers=headers)
        assert r.json()["currency"] == "USD"
        r = await client.put("/finance/base-currency", json={"currency": "EUR"}, headers=headers)
        assert r.status_code in (200, 201, 204)

        ids = await _setup(client, headers)
        await client.post("/finance/transactions", json={
            "account_id": ids["account"],
            "date": f"{this_month()}-05",
            "amount": 10000,
            "payee": "Employer",
            "category_id": None,
        }, headers=headers)
        body = (await client.get(
            "/finance/budget", params={"month": this_month()}, headers=headers
        )).json()
        assert body["base_currency"] == "EUR"
        # the USD opening balance must not leak into EUR budget math
        assert body["income"] == 0

        r = await client.put("/finance/base-currency", json={"currency": "XX"}, headers=headers)
        assert r.status_code == 400


def test_field_mapping_overrides_fallback_chain():
    tx = {
        "transactionId": "GC-9",
        "bookingDate": "2026-01-05",
        "valueDate": "2026-01-06",
        "transactionAmount": {"amount": "-42.10", "currency": "EUR"},
        "debtorName": "WRONG NAME",
        "remittanceInformationUnstructured": "real merchant here",
    }
    rows = gocardless_transactions_to_rows([tx], [], "acct", mapping={
        "payment": {"payee": "remittanceInformationUnstructured"},
    })
    assert rows[0]["payee_name"] == "real merchant here"

    rows = gocardless_transactions_to_rows([tx], [], "acct")
    assert rows[0]["payee_name"] == "WRONG NAME"


def _build_actual_backup() -> bytes:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, type TEXT,
            offbudget INTEGER, closed INTEGER, tombstone INTEGER);
        CREATE TABLE category_groups (id TEXT PRIMARY KEY, is_income INTEGER, name TEXT, tombstone INTEGER);
        CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT, cat_group TEXT,
            is_income INTEGER, tombstone INTEGER, sort_order INTEGER);
        CREATE TABLE payees (id TEXT PRIMARY KEY, name TEXT, transfer_acct TEXT, tombstone INTEGER);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account TEXT, payee TEXT,
            category TEXT, date INTEGER, amount INTEGER, notes TEXT, cleared INTEGER,
            reconciled INTEGER, imported_id TEXT, imported_payee TEXT, transfer_id TEXT,
            parent_id TEXT, is_parent INTEGER, is_child INTEGER, tombstone INTEGER, sort_order INTEGER, schedule TEXT);
        CREATE TABLE schedules (id TEXT PRIMARY KEY, name TEXT, rule TEXT, payee TEXT,
            account TEXT, amount INTEGER, completed INTEGER, tombstone INTEGER, notes TEXT);
        CREATE TABLE rules (id TEXT PRIMARY KEY, conditions TEXT, actions TEXT,
            conditions_runnable INTEGER, tombstone INTEGER);
        CREATE TABLE zero_budgets (month TEXT, category TEXT, amount INTEGER, carryover INTEGER);
        CREATE TABLE zero_budget_months (id TEXT PRIMARY KEY, buffered INTEGER);
        INSERT INTO zero_budgets VALUES ('2026-01', 'c2', 15000, 1);
        INSERT INTO zero_budgets VALUES ('2026-02', 'c2', 0, 1);
        INSERT INTO zero_budgets VALUES ('2026-03', 'c1', 9999, 0);
        INSERT INTO zero_budget_months VALUES ('2026-01', 5000);
        INSERT INTO accounts VALUES ('a1', 'Old Checking', 'checking', 0, 0, 0);
        INSERT INTO category_groups VALUES ('g1', 1, 'Income', 0);
        INSERT INTO category_groups VALUES ('g2', 0, 'Expenses', 0);
        INSERT INTO categories VALUES ('c1', 'Paycheck', 'g1', 0, 0, 1);
        INSERT INTO categories VALUES ('c2', 'Groceries', 'g2', 0, 0, 2);
        INSERT INTO payees VALUES ('p1', 'Kroger', NULL, 0);
        INSERT INTO transactions VALUES ('t1', 'a1', 'p1', 'c2', 20260115, -4210, 'run', 1, 0, NULL, NULL, NULL, NULL, 0, 0, 0, 1, NULL);
        INSERT INTO transactions VALUES ('t2', 'a1', NULL, 'c1', 20260131, 200000, NULL, 1, 1, NULL, NULL, NULL, NULL, 0, 0, 0, 2, NULL);
        INSERT INTO transactions VALUES ('t3', 'a1', 'p1', 'c2', 20260116, -1000, NULL, 1, 0, NULL, NULL, NULL, NULL, 1, 0, 0, 3, NULL);
        INSERT INTO transactions VALUES ('t4', 'a1', 'p1', NULL, 20260116, -600, NULL, 1, 0, NULL, NULL, NULL, 't3', 0, 1, 0, 4, NULL);
        INSERT INTO transactions VALUES ('t5', 'a1', 'p1', NULL, 20260116, -400, NULL, 1, 0, NULL, NULL, NULL, 't3', 0, 1, 0, 5, NULL);
        INSERT INTO transactions VALUES ('t6', 'a1', 'p1', 'c2', 20260110, -999, 'deleted one', 1, 0, NULL, NULL, NULL, NULL, 0, 0, 1, 6, NULL);
        INSERT INTO schedules VALUES ('s1', 'Rent', ?, 'p1', 'a1', -120000, 0, 0, NULL);
        INSERT INTO rules VALUES ('r1', ?, ?, 1, 0);
        """
    )
    schedule_rule = json.dumps({
        "conditions": [{
            "field": "date",
            "op": "is",
            "value": {
                "type": "recur",
                "schedule": {"config": {
                    "frequency": "monthly",
                    "start": "2026-01-01",
                    "patterns": [{"type": "dayOfMonth", "value": 1}],
                }},
            },
        }],
    })
    rule_conditions = json.dumps([
        {"field": "imported_payee", "op": "contains", "value": "kroger"}
    ])
    rule_actions = json.dumps([{"op": "set", "field": "category", "value": "Groceries"}])
    conn.execute("UPDATE schedules SET rule = ?", (schedule_rule,))
    conn.execute("UPDATE rules SET conditions = ?, actions = ?", (rule_conditions, rule_actions))
    conn.commit()

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "db.sqlite"
        target = sqlite3.connect(db_path)
        target.executescript("".join(conn.iterdump()))
        target.commit()
        target.close()
        conn.close()

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("db.sqlite", db_path.read_bytes())
            zf.writestr("metadata.json", json.dumps({"budgetName": "Test"}))
        return zip_buffer.getvalue()


async def test_actual_backup_migration_round_trip(db, database_url):
    import base64

    archive = _build_actual_backup()
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        r = await client.post("/finance/migrate/actual", json={
            "zip_base64": base64.b64encode(archive).decode()
        }, headers=headers)
        assert r.status_code in (200, 201), r.text
        report = r.json()
        assert report["accounts"] == 1
        assert report["categories"] == 2
        assert report["payees"] == 1
        assert report["schedules"] == 1
        assert report["rules"] == 1
        # t1 + t2 standalone, t3 becomes a split parent -> 3 written
        assert report["transactions"] >= 3

        pool = app.db.pool
        standalone = await pool.fetch(
            "SELECT amount, notes, reconciled FROM finance_transaction "
            "WHERE imported_id IN ('actual:t1', 'actual:t2') ORDER BY imported_id"
        )
        assert len(standalone) == 2
        assert standalone[0]["amount"] == -4210
        assert standalone[1]["amount"] == 200000
        assert standalone[1]["reconciled"] is True

        deleted = await pool.fetchval(
            "SELECT count(*) FROM finance_transaction WHERE imported_id = 'actual:t6'"
        )
        assert deleted is None or deleted == 0
        assert report["deleted_skipped"] == 1

        schedules = await pool.fetch("SELECT name, amount, active FROM finance_schedule")
        assert len(schedules) == 1 and schedules[0]["name"] == "Rent"

        rules = await pool.fetch("SELECT conditions, actions FROM finance_rule")
        assert len(rules) == 1

        budgets = await pool.fetch(
            "SELECT month, amount, rollover_mode FROM finance_budget ORDER BY month"
        )
        assert len(budgets) == 2
        assert budgets[0]["amount"] == 15000
        assert budgets[0]["rollover_mode"] == "rollover"
        assert budgets[1]["amount"] == 9999
        assert budgets[1]["rollover_mode"] == "reset"

        r = await client.post("/finance/migrate/actual", json={
            "zip_base64": base64.b64encode(archive).decode()
        }, headers=headers)
        second = r.json()
        assert second["transactions"] == 0
        assert second["accounts"] == 0
        assert second["categories"] == 0


async def test_actual_migration_refuses_non_zip(db, database_url):
    import base64

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _operator(client)
        r = await client.post("/finance/migrate/actual", json={
            "zip_base64": base64.b64encode(b"not a zip").decode()
        }, headers=headers)
        assert r.status_code == 400
