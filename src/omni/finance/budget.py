"""Envelope budget math, following Actual Budget's model (MIT).

Source: the budget month logic in packages/loot-core
(server/aql/context/budget.ts). Copyright (c) James Long and Actual
Budget contributors, MIT license.

Envelope rules, kept: only cash on hand is budgetable (available =
income - budgeted, never credit), spending in a month is activity
against categories, unspent envelopes roll forward, income categories
hold money for assignment. Dropped for v1: per-month rollover overrides
(hold/cover/reset per category-month) -- the default rollover applies
uniformly and is stated in the API payload.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID


def month_of(value: date | str) -> date:
    text = str(value)[:10]
    if len(text) == 7:
        text += "-01"
    d = value if isinstance(value, date) else date.fromisoformat(text)
    return d.replace(day=1)


def previous_month(month: date) -> date:
    if month.month == 1:
        return month.replace(year=month.year - 1, month=12, day=1)
    return month.replace(month=month.month - 1, day=1)


async def base_currency(pool, user_id: UUID) -> str:
    value = await pool.fetchval(
        "SELECT data->'finance'->>'base_currency' FROM user_settings WHERE user_id = $1",
        user_id,
    )
    return (value or "USD").upper()


async def set_base_currency(pool, user_id: UUID, code: str) -> None:
    code = code.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError("currency must be a 3-letter code")
    await pool.execute(
        """
        INSERT INTO user_settings (user_id, data)
        VALUES ($1, jsonb_build_object(
            'finance', jsonb_build_object('base_currency', $2::text)))
        ON CONFLICT (user_id) DO UPDATE SET
            data = jsonb_set(
                user_settings.data,
                '{finance}',
                coalesce(user_settings.data->'finance', '{}'::jsonb)
                    || jsonb_build_object('base_currency', $2::text),
                true
            ),
            updated_at = now()
        """,
        user_id,
        code,
    )


async def _category_activity(pool, user_id: UUID, month: date, next_month: date) -> dict[UUID, int]:
    rows = await pool.fetch(
        """
        SELECT t.category_id AS category_id, sum(t.amount) AS total
        FROM finance_transaction t
        JOIN finance_account a ON a.id = t.account_id
        WHERE t.user_id = $1 AND NOT t.deleted
          AND t.category_id IS NOT NULL AND a.offbudget = false
          AND NOT t.is_parent AND a.currency = $4
          AND t.date >= $2 AND t.date < $3
        GROUP BY t.category_id
        """,
        user_id,
        month,
        next_month,
        await base_currency(pool, user_id),
    )
    return {r["category_id"]: int(r["total"]) for r in rows}


async def _budget_rows(pool, user_id: UUID, month: date) -> dict[UUID, tuple[int, str]]:
    rows = await pool.fetch(
        """
        SELECT category_id, amount, rollover_mode FROM finance_budget
        WHERE user_id = $1 AND month = $2
        """,
        user_id,
        month,
    )
    return {r["category_id"]: (int(r["amount"]), r["rollover_mode"]) for r in rows}


async def _income_total(pool, user_id: UUID, month: date, next_month: date) -> int:
    total = await pool.fetchval(
        """
        SELECT coalesce(sum(t.amount), 0)
        FROM finance_transaction t
        JOIN finance_account a ON a.id = t.account_id
        JOIN finance_category c ON c.id = t.category_id
        WHERE t.user_id = $1 AND NOT t.deleted
          AND c.is_income AND a.offbudget = false AND NOT t.is_parent
          AND a.currency = $4
          AND t.date >= $2 AND t.date < $3
        """,
        user_id,
        month,
        next_month,
        await base_currency(pool, user_id),
    )
    return int(total or 0)


async def _first_activity_month(pool, user_id: UUID) -> date | None:
    value = await pool.fetchval(
        """
        SELECT min(day) FROM (
            SELECT t.date AS day
            FROM finance_transaction t
            JOIN finance_account a ON a.id = t.account_id
            WHERE t.user_id = $1 AND NOT t.deleted AND NOT a.offbudget
            UNION ALL
            SELECT month AS day FROM finance_budget WHERE user_id = $1
        ) history
        """,
        user_id,
    )
    return month_of(value) if value else None


async def month_view(pool, user_id: UUID, month: date) -> dict[str, Any]:
    """The full envelope picture for one month.

    Rollover is computed by walking back through real months of history,
    bounded by the first month with on-budget activity -- an empty
    history zeroes the chain honestly instead of inventing a starting
    balance.
    """
    first = await _first_activity_month(pool, user_id)
    categories = await pool.fetch(
        """
        SELECT id, name, is_income, sort_order,
               goal_type, goal_amount, goal_date
        FROM finance_category
        WHERE user_id = $1 AND NOT tombstone ORDER BY sort_order, lower(name)
        """,
        user_id,
    )
    next_month = month.replace(
        year=month.year + (month.month == 12), month=month.month % 12 + 1, day=1
    )

    current = await _budget_rows(pool, user_id, month)
    base = await base_currency(pool, user_id)
    activity = await _category_activity(pool, user_id, month, next_month)
    income = await _income_total(pool, user_id, month, next_month)
    rollover: dict[UUID, int] = {}
    unassigned_carry = 0
    if first is not None and month > first:
        chain_month = first
        balance: dict[UUID, int] = {}
        while chain_month < month:
            chain_next = chain_month.replace(
                year=chain_month.year + (chain_month.month == 12),
                month=chain_month.month % 12 + 1,
                day=1,
            )
            chain_budgets = await _budget_rows(pool, user_id, chain_month)
            chain_activity = await _category_activity(pool, user_id, chain_month, chain_next)
            # Unassigned cash carries like a category envelope: income adds,
            # assignments subtract, and a reset-mode leftover returns to the
            # pool while a hold-mode leftover stays consumed by its category.
            unassigned_carry += await _income_total(pool, user_id, chain_month, chain_next)
            for cat in categories:
                if cat["is_income"]:
                    continue
                cid = cat["id"]
                budgeted, mode = chain_budgets.get(cid, (0, "rollover"))
                opening = balance.get(cid, 0)
                ending = opening + budgeted + chain_activity.get(cid, 0)
                if mode == "reset":
                    carried = 0
                elif mode == "hold":
                    carried = opening
                else:
                    carried = ending
                unassigned_carry -= budgeted
                unassigned_carry += ending - carried
                balance[cid] = carried
            chain_month = chain_next
        rollover = {cid: value for cid, value in balance.items() if value != 0}

    rows = []
    total_budgeted = 0
    for cat in categories:
        cid = cat["id"]
        if cat["is_income"]:
            rows.append({
                "id": str(cid),
                "name": cat["name"],
                "is_income": True,
                "budgeted": 0,
                "activity": activity.get(cid, 0),
                "available": 0,
                "rollover": 0,
            })
            continue
        cat_budgeted, cat_mode = current.get(cid, (0, "rollover"))
        total_budgeted += cat_budgeted
        available = cat_budgeted + rollover.get(cid, 0) + activity.get(cid, 0)
        rows.append({
            "id": str(cid),
            "name": cat["name"],
            "is_income": False,
            "budgeted": cat_budgeted,
            "activity": activity.get(cid, 0),
            "available": available,
            "rollover": rollover.get(cid, 0),
            "rollover_mode": cat_mode,
            "goal": _goal_payload(
                cat, cat_budgeted, available, month
            ),
        })

    return {
        "month": month.isoformat(),
        "base_currency": base,
        "income": income,
        "budgeted": total_budgeted,
        "available_to_budget": unassigned_carry + income - total_budgeted,
        "categories": rows,
    }


def _goal_payload(cat, budgeted: int, available: int, month: date) -> dict | None:
    goal_type = cat["goal_type"]
    goal_amount = cat["goal_amount"]
    if goal_type is None or goal_amount is None:
        return None
    goal = {
        "type": goal_type,
        "target": int(goal_amount),
        "date": cat["goal_date"].isoformat() if cat["goal_date"] else None,
    }
    if goal_type == "monthly":
        goal["needed"] = max(goal["target"] - budgeted, 0)
        goal["progress"] = budgeted / goal["target"] if goal["target"] else None
    else:
        accumulated = available
        goal["accumulated"] = accumulated
        if cat["goal_date"] is not None:
            months_left = (
                (cat["goal_date"].year - month.year) * 12
                + (cat["goal_date"].month - month.month)
                + 1
            )
            months_left = max(months_left, 1)
        else:
            months_left = 1
        goal["months_left"] = months_left
        goal["needed_per_month"] = max(goal["target"] - accumulated, 0) / months_left
        goal["progress"] = accumulated / goal["target"] if goal["target"] else None
    return goal
