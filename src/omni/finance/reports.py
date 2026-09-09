"""Reports over the finance ledger: cash flow, category spending, net
worth. All figures are computed from recorded transactions only -- no
estimated balances, no extrapolation past the last posted day."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from omni.finance.budget import month_of


def _shift(month: date, delta: int) -> date:
    total = month.year * 12 + (month.month - 1) + delta
    return date(total // 12, total % 12 + 1, 1)


async def cash_flow(pool, user_id: UUID, months: int = 6, end: date | None = None) -> dict:
    from omni.finance.budget import base_currency

    base = await base_currency(pool, user_id)
    end = month_of(end or date.today())
    start = _shift(end, -(months - 1))
    rows = await pool.fetch(
        """
        SELECT date_trunc('month', t.date)::date AS month, a.currency AS currency,
               sum(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END) AS income,
               sum(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END) AS expense
        FROM finance_transaction t
        JOIN finance_account a ON a.id = t.account_id
        WHERE t.user_id = $1 AND NOT t.deleted AND NOT t.is_parent
          AND t.date >= $2 AND t.date < $3
        GROUP BY 1, 2 ORDER BY 1
        """,
        user_id,
        start,
        _shift(end, 1),
    )
    by_month = {
        (r["month"], r["currency"]): (int(r["income"] or 0), int(r["expense"] or 0))
        for r in rows
    }
    currencies = sorted({r["currency"] for r in rows} | {base})
    series = []
    for offset in range(months):
        month = _shift(end, -(months - 1) + offset)
        income, expense = by_month.get((month, base), (0, 0))
        series.append({
            "month": month.isoformat(),
            "income": income,
            "expense": expense,
            "net": income - expense,
        })
    other = {}
    for currency in currencies:
        if currency == base:
            continue
        totals = [
            by_month[(m, currency)]
            for m in {_shift(end, -(months - 1) + offset) for offset in range(months)}
            if (m, currency) in by_month
        ]
        if totals:
            other[currency] = {
                "income": sum(t[0] for t in totals),
                "expense": sum(t[1] for t in totals),
            }
    return {"base_currency": base, "months": series, "other_currencies": other}


async def spending_by_category(pool, user_id: UUID, month: str) -> list[dict]:
    from omni.finance.budget import base_currency

    month_date = month_of(month)
    rows = await pool.fetch(
        """
        SELECT c.name AS category, coalesce(sum(t.amount), 0) AS total,
               count(*) AS tx_count
        FROM finance_transaction t
        JOIN finance_category c ON c.id = t.category_id
        JOIN finance_account a ON a.id = t.account_id
        WHERE t.user_id = $1 AND NOT t.deleted AND NOT t.is_parent
          AND NOT c.is_income AND a.offbudget = false AND a.currency = $4
          AND t.date >= $2 AND t.date < $3
        GROUP BY c.name ORDER BY sum(t.amount)
        """,
        user_id,
        month_date,
        _shift(month_date, 1),
        await base_currency(pool, user_id),
    )
    return [
        {
            "category": r["category"],
            "total": int(r["total"]),
            "tx_count": int(r["tx_count"]),
        }
        for r in rows
    ]


async def net_worth(pool, user_id: UUID, months: int = 12) -> dict:
    """Ledger net worth: cumulative sum of every transaction (on- and
    off-budget), per month, including starting balances -- grouped per
    currency and never summed across them (no invented FX). Accounts with
    no transactions are listed separately so the UI can say 'no history'
    instead of implying zero."""
    from omni.finance.budget import base_currency

    base = await base_currency(pool, user_id)
    today = date.today()
    window_start = _shift(month_of(today), -(months - 1))
    window_end = _shift(month_of(today), 1)
    rows = await pool.fetch(
        """
        SELECT date_trunc('month', t.date)::date AS month, a.currency AS currency,
               sum(t.amount) AS net
        FROM finance_transaction t
        JOIN finance_account a ON a.id = t.account_id
        WHERE t.user_id = $1 AND NOT t.deleted AND NOT t.is_parent
          AND t.date < $2
        GROUP BY 1, 2 ORDER BY 1
        """,
        user_id,
        window_end,
    )
    currencies = sorted({r["currency"] for r in rows} | {base})
    series = []
    per_currency_now: dict[str, int] = {}
    for currency in currencies:
        changes = {
            r["month"]: int(r["net"] or 0)
            for r in rows
            if r["currency"] == currency
        }
        opening = await pool.fetchval(
            """
            SELECT coalesce(sum(t.amount), 0) FROM finance_transaction t
            JOIN finance_account a ON a.id = t.account_id
            WHERE t.user_id = $1 AND NOT t.deleted AND NOT t.is_parent
              AND a.currency = $2 AND t.date < $3
            """,
            user_id,
            currency,
            window_start,
        )
        running = int(opening or 0)
        for offset in range(months):
            month = _shift(month_of(today), -(months - 1) + offset)
            if month <= month_of(today):
                running += changes.get(month, 0)
            if currency == base:
                series.append({
                    "month": month.isoformat(),
                    "net_worth": running if month <= month_of(today) else None,
                    "change": changes.get(month, 0) if month <= month_of(today) else None,
                })
        per_currency_now[currency] = running

    empty = await pool.fetch(
        """
        SELECT a.name FROM finance_account a
        WHERE a.user_id = $1 AND NOT a.closed
          AND NOT EXISTS (
              SELECT 1 FROM finance_transaction t
              WHERE t.account_id = a.id AND NOT t.deleted
          )
        ORDER BY a.name
        """,
        user_id,
    )
    return {
        "base_currency": base,
        "months": series,
        "balances_by_currency": per_currency_now,
        "accounts_without_history": [r["name"] for r in empty],
    }
