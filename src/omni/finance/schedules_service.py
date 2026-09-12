"""Schedules service: CRUD and upcoming-with-status views."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from omni.finance import schedules as engine
from omni.finance.service import FinanceError


async def list_schedules(pool, user_id: UUID) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT s.id, s.name, s.amount, s.config, s.active, s.notes,
               s.payee_id, s.account_id, p.name AS payee_name,
               a.name AS account_name
        FROM finance_schedule s
        LEFT JOIN finance_payee p ON p.id = s.payee_id AND p.user_id = s.user_id
        LEFT JOIN finance_account a ON a.id = s.account_id AND a.user_id = s.user_id
        WHERE s.user_id = $1
        ORDER BY s.created_at
        """,
        user_id,
    )
    today = date.today()
    out = []
    for r in rows:
        config = r["config"]
        if isinstance(config, str):
            config = json.loads(config)
        rec = engine.Recurrence.from_config(config)
        tx_dates = []
        if r["payee_id"]:
            dates = await pool.fetch(
                """
                SELECT date FROM finance_transaction
                WHERE user_id = $1 AND payee_id = $2 AND NOT deleted
                  AND date >= $3
                """,
                user_id,
                r["payee_id"],
                today - timedelta(days=430),
            )
            tx_dates = [row["date"] for row in dates]
        status = engine.schedule_status(rec, today, tx_dates) if r["active"] else {"status": "inactive", "next": None}
        out.append({
            "id": str(r["id"]),
            "name": r["name"],
            "amount": _int_or_none(r["amount"]),
            "config": config,
            "active": r["active"],
            "notes": r["notes"],
            "payee": r["payee_name"],
            "account": r["account_name"],
            **status,
        })
    return out


def _int_or_none(value) -> int | None:
    return int(value) if value is not None else None


async def create_schedule(
    pool,
    user_id: UUID,
    *,
    name: str,
    config: dict,
    payee: str | None = None,
    account_id: UUID | None = None,
    amount: int | None = None,
    notes: str | None = None,
) -> dict:
    name = name.strip()
    if not name or len(name) > 120:
        raise FinanceError("schedule name must contain 1 to 120 characters")
    rec = engine.Recurrence.from_config(config)
    if rec.start is None:
        raise FinanceError("schedule config needs a start date")
    payee_id = None
    if payee:
        payee_id = await pool.fetchval(
            """
            SELECT id FROM finance_payee
            WHERE user_id = $1 AND lower(name) = lower($2) AND NOT tombstone
            """,
            user_id,
            payee,
        )
    if account_id is not None:
        account_owner = await pool.fetchval(
            "SELECT user_id FROM finance_account WHERE id = $1", account_id
        )
        if account_owner != user_id:
            raise FinanceError("account not found")
    row = await pool.fetchrow(
        """
        INSERT INTO finance_schedule
            (user_id, name, payee_id, account_id, amount, config, notes)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) RETURNING id
        """,
        user_id,
        name,
        payee_id,
        account_id,
        amount,
        json.dumps(config),
        notes,
    )
    next_date = engine.next_occurrence(rec, date.today())
    return {"id": str(row["id"]), "name": name, "next": next_date.isoformat() if next_date else None}


async def set_schedule_active(pool, user_id: UUID, schedule_id: UUID, active: bool) -> None:
    updated = await pool.execute(
        "UPDATE finance_schedule SET active = $3, updated_at = now() WHERE id = $1 AND user_id = $2",
        schedule_id,
        user_id,
        active,
    )
    if updated == "UPDATE 0":
        raise FinanceError("schedule not found")


async def delete_schedule(pool, user_id: UUID, schedule_id: UUID) -> None:
    await pool.execute(
        "DELETE FROM finance_schedule WHERE id = $1 AND user_id = $2",
        schedule_id,
        user_id,
    )
