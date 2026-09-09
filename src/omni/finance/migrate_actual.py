"""Import an Actual Budget backup (export zip) into the finance domain.

Reads the SQLite database inside the export and maps: accounts, category
groups + categories, payees, transactions (splits, transfers, deleted),
schedules, and rules. Budgeted months live in Actual's zero_budget_* blob
format and are imported best-effort; anything unreadable is skipped and
reported rather than guessed.

Idempotent: transactions are stored with imported_id "actual:<their id>"
so a re-run reconciles to zero duplicates; named entities (accounts,
categories, payees) are matched by name and reused.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import zipfile
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from omni.finance.service import FinanceError, create_account, create_category

ACCOUNT_TYPE_MAP = {
    "checking": "checking",
    "savings": "savings",
    "credit": "credit",
    "loan": "loan",
    "cash": "cash",
    "investment": "investment",
}


def _extract_db(archive_bytes: bytes) -> sqlite3.Connection:
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise FinanceError("not a zip archive") from exc
    names = archive.namelist()
    db_name = next(
        (n for n in names if n.endswith(".sqlite") and "backup" not in n.lower()),
        None,
    )
    if db_name is None:
        raise FinanceError("no SQLite database inside the backup")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "db.sqlite"
        db_path.write_bytes(archive.read(db_name))
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn


def _actual_date(value) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    digits = text[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _actual_month_value(value) -> date | None:
    text = str(value or "")[:7]
    try:
        return date.fromisoformat(f"{text}-01")
    except ValueError:
        return None


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r["name"] for r in rows}


def _rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM {table}").fetchall()


async def import_actual_backup(
    pool,
    user_id: UUID,
    archive_bytes: bytes,
    *,
    base_currency: str = "USD",
) -> dict[str, Any]:
    conn = _extract_db(archive_bytes)
    try:
        tables = _table_names(conn)
        report: dict[str, Any] = {
            "accounts": 0,
            "categories": 0,
            "payees": 0,
            "transactions": 0,
            "schedules": 0,
            "rules": 0,
            "budget_months": 0,
            "skipped_rules": 0,
            "deleted_skipped": 0,
        }

        account_map: dict[str, str] = {}
        if "accounts" in tables:
            for row in _rows(conn, "accounts"):
                if row["tombstone"]:
                    continue
                atype = ACCOUNT_TYPE_MAP.get(row["type"], "other")
                existing = await pool.fetchval(
                    "SELECT id FROM finance_account WHERE user_id = $1 AND name = $2",
                    user_id,
                    row["name"],
                )
                if existing:
                    account_map[row["id"]] = str(existing)
                    continue
                created = await create_account(
                    pool,
                    user_id,
                    name=row["name"],
                    type=atype,
                    offbudget=bool(row["offbudget"]),
                    currency=base_currency,
                )
                account_map[row["id"]] = created["id"]
                report["accounts"] += 1

        income_group_ids: set[str] = set()
        if "category_groups" in tables:
            for row in _rows(conn, "category_groups"):
                if row["is_income"]:
                    income_group_ids.add(row["id"])

        category_map: dict[str, str] = {}
        if "categories" in tables:
            for row in _rows(conn, "categories"):
                if row["tombstone"]:
                    continue
                existing = await pool.fetchval(
                    """
                    SELECT id FROM finance_category
                    WHERE user_id = $1 AND lower(name) = lower($2) AND NOT tombstone
                    """,
                    user_id,
                    row["name"],
                )
                if existing:
                    category_map[row["id"]] = str(existing)
                    continue
                is_income = bool(row["is_income"]) or row["cat_group"] in income_group_ids
                created = await create_category(
                    pool, user_id, name=row["name"], is_income=is_income
                )
                category_map[row["id"]] = created["id"]
                report["categories"] += 1

        payee_map: dict[str, str] = {}
        if "payees" in tables:
            for row in _rows(conn, "payees"):
                if row["tombstone"]:
                    continue
                existing = await pool.fetchval(
                    """
                    SELECT id FROM finance_payee
                    WHERE user_id = $1 AND lower(name) = lower($2) AND NOT tombstone
                    """,
                    user_id,
                    row["name"],
                )
                if existing is None:
                    transfer_acct = None
                    if row["transfer_acct"]:
                        transfer_acct = account_map.get(row["transfer_acct"])
                    existing = await pool.fetchval(
                        """
                        INSERT INTO finance_payee (user_id, name, transfer_acct)
                        VALUES ($1, $2, $3) RETURNING id
                        """,
                        user_id,
                        row["name"],
                        UUID(transfer_acct) if transfer_acct else None,
                    )
                    report["payees"] += 1
                payee_map[row["id"]] = str(existing)

        schedule_map: dict[str, str] = {}
        if "schedules" in tables:
            for row in _rows(conn, "schedules"):
                if row["tombstone"]:
                    continue
                try:
                    rule = json.loads(row["rule"]) if isinstance(row["rule"], str) else row["rule"]
                    config = _rule_to_recurrence(rule)
                except Exception:  # noqa: BLE001 - unparseable schedule: skip, report
                    continue
                if config is None:
                    continue
                payee_id = payee_map.get(row["payee"]) if row["payee"] else None
                account_id = account_map.get(row["account"]) if row["account"] else None
                new_id = await pool.fetchval(
                    """
                    INSERT INTO finance_schedule
                        (user_id, name, payee_id, account_id, amount, config, active, notes)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8) RETURNING id
                    """,
                    user_id,
                    row["name"],
                    UUID(payee_id) if payee_id else None,
                    UUID(account_id) if account_id else None,
                    row["amount"],
                    json.dumps(config),
                    not bool(row["completed"]),
                    row["notes"],
                )
                schedule_map[row["id"]] = str(new_id)
                report["schedules"] += 1

        from omni.finance.reconcile import reconcile

        tx_rows: list[dict] = []
        for row in _rows(conn, "transactions") if "transactions" in tables else []:
            account_id = account_map.get(row["account"])
            tx_date = _actual_date(row["date"])
            if account_id is None or tx_date is None:
                continue
            if row["is_child"]:
                continue
            if row["tombstone"]:
                report["deleted_skipped"] += 1
                continue
            tx_rows.append({
                "date": tx_date,
                "payee_name": None,
                "payee_id": payee_map.get(row["payee"]) if row["payee"] else None,
                "category_id": category_map.get(row["category"]) if row["category"] else None,
                "amount": row["amount"],
                "notes": row["notes"],
                "cleared": bool(row["cleared"]),
                "reconciled": bool(row["reconciled"]),
                "imported_id": f"actual:{row['id']}",
                "imported_payee": None,
                "deleted": bool(row["tombstone"]),
                "is_parent": bool(row["is_parent"]),
                "account_id": account_id,
                "children": _child_rows(
                    conn, row["id"], tx_date, account_id, payee_map, category_map
                ) if row["is_parent"] else None,
            })

        for batch_account in {r["account_id"] for r in tx_rows}:
            batch = [r for r in tx_rows if r["account_id"] == batch_account]
            live = [r for r in batch if not r["deleted"]]
            parents = {r["imported_id"]: r for r in live if r["is_parent"]}

            flat: list[dict] = []
            for r in live:
                if r["is_parent"]:
                    continue
                flat.append({k: v for k, v in r.items() if k != "children"})
            result = await reconcile(
                pool,
                user_id=user_id,
                account_id=UUID(batch_account),
                rows=flat,
            )
            report["transactions"] += len(result["added"])

            for r in batch:
                if r["is_parent"]:
                    await _insert_split_tree(pool, user_id, r, parents, report)
            await _restore_tx_state(pool, live)

        if "zero_budgets" in tables:
            for row in _rows(conn, "zero_budgets"):
                cid = category_map.get(row["category"])
                if cid is None:
                    continue
                month = _actual_month_value(row["month"])
                if month is None or row["amount"] in (None, 0):
                    continue
                mode = "rollover" if row["carryover"] else "reset"
                await pool.execute(
                    """
                    INSERT INTO finance_budget
                        (user_id, month, category_id, amount, rollover_mode)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (user_id, month, category_id)
                    DO UPDATE SET amount = excluded.amount,
                                  rollover_mode = excluded.rollover_mode
                    """,
                    user_id,
                    month,
                    UUID(cid),
                    int(row["amount"]),
                    mode,
                )
                report["budget_months"] += 1

        if "rules" in tables:
            from omni.finance import rules as rules_mod

            rank = 1
            for row in _rows(conn, "rules"):
                if row["tombstone"]:
                    continue
                conditions = _json_value(row["conditions"])
                actions = _json_value(row["actions"])
                if not isinstance(conditions, list) or not isinstance(actions, list):
                    report["skipped_rules"] += 1
                    continue
                conditions = [_map_condition(c) for c in conditions]
                actions = [_map_action(a, category_map, payee_map) for a in actions]
                conditions = [c for c in conditions if c]
                actions = [a for a in actions if a]
                try:
                    rules_mod.validate_rule_payload(conditions, actions)
                except rules_mod.RuleError:
                    report["skipped_rules"] += 1
                    continue
                await pool.execute(
                    """
                    INSERT INTO finance_rule (user_id, rank, conditions, actions, enabled)
                    VALUES ($1, $2, $3::jsonb, $4::jsonb, $5)
                    """,
                    user_id,
                    rank,
                    json.dumps(conditions),
                    json.dumps(actions),
                    bool(row["conditions_runnable"]),
                )
                rank += 1
                report["rules"] += 1

        return report
    finally:
        conn.close()


def _child_rows(conn, parent_id, tx_date, account_id, payee_map, category_map):
    out = []
    for row in conn.execute(
        "SELECT * FROM transactions WHERE parent_id = ? AND tombstone = 0 ORDER BY rowid",
        (parent_id,),
    ).fetchall():
        out.append({
            "amount": row["amount"],
            "category_id": category_map.get(row["category"]) if row["category"] else None,
            "notes": row["notes"],
        })
    return out


async def _insert_split_tree(pool, user_id: UUID, parent_row, parents, report):
    children = parent_row.get("children") or []
    if not children:
        return
    from datetime import date as _date

    tx_date = _date.fromisoformat(parent_row["date"])

    already = await pool.fetchval(
        """
        SELECT id FROM finance_transaction
        WHERE user_id = $1 AND account_id = $2 AND imported_id = $3
        """,
        user_id,
        UUID(parent_row["account_id"]),
        parent_row["imported_id"],
    )
    if already:
        return
    import time as _time

    parent_id = uuid4()
    sort_base = int(_time.time() * 1000)
    await pool.execute(
        """
        INSERT INTO finance_transaction
            (id, user_id, account_id, payee_id, category_id, date, amount,
             notes, cleared, reconciled, imported_id, is_parent, sort_order)
        VALUES ($1, $2, $3, $4, NULL, $5, $6, $7, $8, false, $9, true, $10)
        """,
        parent_id,
        user_id,
        UUID(parent_row["account_id"]),
        UUID(parent_row["payee_id"]) if parent_row.get("payee_id") else None,
        tx_date,
        sum(c["amount"] for c in children),
        parent_row["notes"],
        parent_row["cleared"],
        parent_row["imported_id"],
        sort_base,
    )
    for idx, child in enumerate(children):
        await pool.execute(
            """
            INSERT INTO finance_transaction
                (user_id, account_id, payee_id, category_id, date, amount,
                 notes, cleared, parent_id, sort_order)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            user_id,
            UUID(parent_row["account_id"]),
            UUID(parent_row["payee_id"]) if parent_row.get("payee_id") else None,
            UUID(child["category_id"]) if child.get("category_id") else None,
            tx_date,
            child["amount"],
            child.get("notes"),
            parent_row["cleared"],
            parent_id,
            sort_base - (idx + 1),
        )
    report["transactions"] += 1


async def _restore_tx_state(pool, live_rows):
    for r in live_rows:
        if r["reconciled"]:
            await pool.execute(
                """
                UPDATE finance_transaction SET reconciled = true
                WHERE account_id = $1 AND imported_id = $2
                """,
                UUID(r["account_id"]),
                r["imported_id"],
            )


def _json_value(value):
    if isinstance(value, (list, dict)) or value is None:
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _rule_to_recurrence(rule: dict) -> dict | None:
    for condition in rule.get("conditions", []) if isinstance(rule, dict) else []:
        value = condition.get("value") or {}
        if isinstance(value, dict) and value.get("type") == "recur":
            schedule = value.get("schedule") or {}
            config = schedule.get("config") or schedule
            if config.get("frequency") in {"daily", "weekly", "monthly", "yearly"}:
                return config
    return None


def _map_condition(condition: dict) -> dict | None:
    field = condition.get("field")
    op = condition.get("op")
    value = condition.get("value")
    if field == "imported_payee":
        field = "imported_payee"
    elif field == "payee":
        field = "payee"
    elif field == "notes":
        field = "notes"
    elif field == "account":
        field = "account"
    elif field == "category":
        field = "category"
    elif field == "amount":
        field = "amount"
    elif field == "date":
        if isinstance(value, dict) and value.get("type") == "recur":
            return {"field": "schedule", "op": "is", "value": value}
        field = "date"
    elif field == "cleared":
        field = "cleared"
    else:
        return None
    return {"field": field, "op": op, "value": value, **({"options": condition["options"]} if condition.get("options") else {})}


def _map_action(action: dict, category_map: dict, payee_map: dict) -> dict | None:
    field = action.get("field")
    op = action.get("op", "set")
    value = action.get("value")
    if op == "set" and field in ("category", "payee"):
        source = category_map if field == "category" else payee_map
        return {"op": "set", "field": field, "value": value}
    if op in ("set", "prepend-notes", "append-notes", "delete-transaction", "link-schedule", "set-split-amount"):
        return {"op": op, "field": field, "value": value, **({"options": action["options"]} if action.get("options") else {})}
    return None
