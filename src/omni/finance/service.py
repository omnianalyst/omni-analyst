"""Finance domain service: CRUD, payee resolution, import orchestration."""

from __future__ import annotations

import csv
import io
import json
import re
import time
from datetime import date as date_type
from typing import Any
from uuid import UUID, uuid4

from omni.finance import budget as budget_mod
from omni.finance import rules as rules_mod
from omni.finance.normalisation import (
    ImportRowError,
    normalised_string,
    normalise_import_row,
)
from omni.finance.reconcile import reconcile
from omni.finance.transfers import create_transfer_side
from omni.finance.writer import finance_write

ACCOUNT_TYPES = frozenset(
    {"checking", "savings", "credit", "loan", "cash", "investment", "other"}
)

DATE_HEADER_RE = re.compile(r"date|posted|transaction date", re.I)
PAYEE_HEADER_RE = re.compile(r"payee|description|merchant|name|detail", re.I)
AMOUNT_HEADER_RE = re.compile(r"amount|value", re.I)
DEBIT_HEADER_RE = re.compile(r"debit|withdraw|money_out", re.I)
CREDIT_HEADER_RE = re.compile(r"credit|deposit|money_in", re.I)
NOTES_HEADER_RE = re.compile(r"notes|memo|comment", re.I)
ID_HEADER_RE = re.compile(r"fitid|reference|ref|id", re.I)

DATE_PATTERNS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%d.%m.%Y", "%Y/%m/%d")


class FinanceError(ValueError):
    pass


class _Unset:
    """Marks a field the caller did not send, distinct from explicit null."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


async def _require_owned_category(pool, user_id: UUID, category_id) -> None:
    if category_id in (None, ""):
        return
    try:
        cid = _uuid_or_none(category_id)
    except ValueError:
        raise FinanceError("category not found") from None
    if cid is None:
        return
    owner = await pool.fetchval(
        "SELECT user_id FROM finance_category WHERE id = $1", cid
    )
    if owner != user_id:
        raise FinanceError("category not found")


async def list_accounts(pool, user_id: UUID) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT a.id, a.name, a.type, a.currency, a.offbudget, a.closed,
               a.balance_current, a.balance_as_of,
               coalesce((
                   SELECT sum(t.amount) FROM finance_transaction t
                   WHERE t.account_id = a.id AND t.user_id = a.user_id
                     AND NOT t.deleted AND NOT t.is_parent
               ), 0) AS ledger_balance
        FROM finance_account a
        WHERE a.user_id = $1
        ORDER BY a.closed, a.name
        """,
        user_id,
    )
    out = []
    for r in rows:
        out.append({
            "id": str(r["id"]),
            "name": r["name"],
            "type": r["type"],
            "currency": r["currency"],
            "offbudget": r["offbudget"],
            "closed": r["closed"],
            "balance_current": _int_or_none(r["balance_current"]),
            "ledger_balance": int(r["ledger_balance"] or 0),
            "balance_as_of": r["balance_as_of"].isoformat() if r["balance_as_of"] else None,
        })
    return out


@finance_write
async def create_account(
    pool,
    user_id: UUID,
    *,
    name: str,
    type: str,
    offbudget: bool = False,
    opening_balance_cents: int | None = None,
    currency: str = "USD",
) -> dict:
    name = name.strip()
    if not name or len(name) > 80:
        raise FinanceError("account name must contain 1 to 80 characters")
    if type not in ACCOUNT_TYPES:
        raise FinanceError(f"unsupported account type {type!r}")
    currency = currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise FinanceError("currency must be a 3-letter code")
    existing = await pool.fetchval(
        "SELECT id FROM finance_account WHERE user_id = $1 AND name = $2",
        user_id,
        name,
    )
    if existing:
        raise FinanceError(f"an account named {name!r} already exists")
    row = await pool.fetchrow(
        """
        INSERT INTO finance_account (user_id, name, type, currency, offbudget, balance_current, balance_as_of)
        VALUES ($1, $2, $3, $4, $5, $6, CASE WHEN $6::bigint IS NULL THEN NULL ELSE CURRENT_DATE END)
        RETURNING id
        """,
        user_id,
        name,
        type,
        currency,
        offbudget,
        opening_balance_cents,
    )
    if opening_balance_cents not in (None, 0):
        income_id = await pool.fetchval(
            """
            SELECT id FROM finance_category
            WHERE user_id = $1 AND is_income AND NOT tombstone
              AND lower(name) = 'starting balances'
            """,
            user_id,
        )
        if income_id is None:
            income_id = await pool.fetchval(
                """
                INSERT INTO finance_category (user_id, name, is_income)
                VALUES ($1, 'Starting Balances', true) RETURNING id
                """,
                user_id,
            )
        payee_id = await resolve_payee(pool, user_id, "Starting Balance")
        await pool.execute(
            """
            INSERT INTO finance_transaction
                (user_id, account_id, payee_id, category_id, date, amount,
                 cleared, sort_order, notes)
            VALUES ($1, $2, $3, $4, CURRENT_DATE, $5, true, $6, 'Opening balance')
            """,
            user_id,
            row["id"],
            payee_id,
            income_id,
            opening_balance_cents,
            int(time.time() * 1000),
        )
    return {"id": str(row["id"]), "name": name, "type": type}


async def close_account(pool, user_id: UUID, account_id: UUID, closed: bool) -> None:
    await pool.execute(
        "UPDATE finance_account SET closed = $3, updated_at = now() WHERE id = $1 AND user_id = $2",
        account_id,
        user_id,
        closed,
    )


async def list_categories(pool, user_id: UUID) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT id, name, is_income FROM finance_category
        WHERE user_id = $1 AND NOT tombstone ORDER BY sort_order, lower(name)
        """,
        user_id,
    )
    return [
        {"id": str(r["id"]), "name": r["name"], "is_income": r["is_income"]}
        for r in rows
    ]


async def create_category(pool, user_id: UUID, *, name: str, is_income: bool = False) -> dict:
    name = name.strip()
    if not name or len(name) > 80:
        raise FinanceError("category name must contain 1 to 80 characters")
    existing = await pool.fetchval(
        """
        SELECT id FROM finance_category
        WHERE user_id = $1 AND lower(name) = lower($2) AND NOT tombstone
        """,
        user_id,
        name,
    )
    if existing:
        raise FinanceError(f"a category named {name!r} already exists")
    row = await pool.fetchrow(
        "INSERT INTO finance_category (user_id, name, is_income) VALUES ($1, $2, $3) RETURNING id",
        user_id,
        name,
        is_income,
    )
    return {"id": str(row["id"]), "name": name, "is_income": is_income}


async def delete_category(pool, user_id: UUID, category_id: UUID) -> None:
    await pool.execute(
        "UPDATE finance_category SET tombstone = true WHERE id = $1 AND user_id = $2",
        category_id,
        user_id,
    )


async def resolve_payee(pool, user_id: UUID, name: str | None) -> UUID | None:
    """Actual's resolvePayee: reuse by exact (case-folded) name, else
    create. None for a nameless row."""
    if name is None:
        return None
    existing = await pool.fetchval(
        """
        SELECT id FROM finance_payee
        WHERE user_id = $1 AND lower(name) = lower($2) AND NOT tombstone
        """,
        user_id,
        name,
    )
    if existing:
        return existing
    return await pool.fetchval(
        "INSERT INTO finance_payee (user_id, name) VALUES ($1, $2) RETURNING id",
        user_id,
        name,
    )


async def list_transactions(
    pool,
    user_id: UUID,
    *,
    month: str,
    account_id: UUID | None = None,
    query: str | None = None,
) -> list[dict]:
    month_date = date_type.fromisoformat(str(month)[:10])
    rows = await pool.fetch(
        """
        SELECT t.id, t.date, t.amount, t.notes, t.cleared, t.reconciled,
               t.imported_id, t.imported_payee, t.transfer_id,
               t.parent_id, t.is_parent, t.schedule_id,
               p.name AS payee_name, c.name AS category_name,
               a.name AS account_name, a.currency AS currency
        FROM finance_transaction t
        JOIN finance_account a ON a.id = t.account_id
        LEFT JOIN finance_payee p ON p.id = t.payee_id
        LEFT JOIN finance_category c ON c.id = t.category_id
        WHERE t.user_id = $1 AND NOT t.deleted
          AND t.date >= $2 AND t.date < ($2 + interval '1 month')
          AND ($3::uuid IS NULL OR t.account_id = $3)
          AND ($4::text IS NULL OR p.name ILIKE '%' || $4 || '%'
               OR t.notes ILIKE '%' || $4 || '%'
               OR t.imported_payee ILIKE '%' || $4 || '%')
        ORDER BY t.date DESC, t.sort_order DESC
        """,
        user_id,
        month_date,
        account_id,
        query,
    )
    return [
        {
            "id": str(r["id"]),
            "date": r["date"].isoformat(),
            "amount": int(r["amount"]),
            "notes": r["notes"],
            "cleared": r["cleared"],
            "reconciled": r["reconciled"],
            "payee": r["payee_name"],
            "category": r["category_name"],
            "account": r["account_name"],
            "transfer": r["transfer_id"] is not None,
            "split": bool(r["is_parent"]),
            "split_child": r["parent_id"] is not None,
            "schedule_id": str(r["schedule_id"]) if r["schedule_id"] else None,
            "currency": r["currency"],
        }
        for r in rows
    ]


@finance_write
async def create_manual_transaction(
    pool,
    user_id: UUID,
    *,
    account_id: UUID,
    date: str,
    amount: int,
    payee_name: str | None,
    category_id: UUID | None,
    notes: str | None,
    cleared: bool = True,
    splits: list[dict] | None = None,
) -> dict:
    if amount == 0:
        raise FinanceError("amount must be non-zero")
    owner = await pool.fetchval(
        "SELECT user_id FROM finance_account WHERE id = $1", account_id
    )
    if owner != user_id:
        raise FinanceError("account not found")
    await _require_owned_category(pool, user_id, category_id)
    for split in splits or []:
        await _require_owned_category(pool, user_id, split.get("category_id"))
    tx_date = date_type.fromisoformat(str(date)[:10])
    payee_id = await resolve_payee(pool, user_id, payee_name)
    transfer_acct = None
    if payee_id and not splits:
        transfer_acct = await pool.fetchval(
            "SELECT transfer_acct FROM finance_payee WHERE id = $1", payee_id
        )
    if category_id is None and not splits:
        applied_notes, applied_category = await _apply_rules_to_manual(
            pool, user_id, account_id, payee_name, notes, amount, tx_date
        )
        if applied_category is not None:
            category_id = applied_category
        if notes is None and applied_notes is not None:
            notes = applied_notes
    is_parent = bool(splits)
    if is_parent:
        split_total = sum(int(s["amount"]) for s in splits)
        if split_total != amount:
            raise FinanceError(
                f"split amounts sum to {split_total}, parent is {amount}"
            )
        if any(int(s["amount"]) == 0 for s in splits):
            raise FinanceError("split amounts must be non-zero")
    tx_id = uuid4()
    row = {
        "id": tx_id,
        "account_id": account_id,
        "date": tx_date,
        "amount": amount,
        "notes": notes,
        "transfer_acct": transfer_acct,
    }
    inserted = await pool.fetchval(
        """
        INSERT INTO finance_transaction
            (id, user_id, account_id, payee_id, category_id, date, amount,
             notes, cleared, is_parent, sort_order)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        tx_id,
        user_id,
        account_id,
        payee_id,
        category_id,
        tx_date,
        amount,
        notes,
        cleared,
        is_parent,
        int(time.time() * 1000),
    )
    if is_parent:
        for idx, split in enumerate(splits):
            await pool.execute(
                """
                INSERT INTO finance_transaction
                    (id, user_id, account_id, payee_id, category_id, date,
                     amount, notes, cleared, parent_id, sort_order)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                uuid4(),
                user_id,
                account_id,
                payee_id,
                _uuid_or_none(split.get("category_id")),
                tx_date,
                int(split["amount"]),
                split.get("notes") or notes,
                cleared,
                tx_id,
                int(time.time() * 1000) - (idx + 1),
            )
    elif transfer_acct is not None:
        await create_transfer_side(pool, user_id=user_id, tx_row=row)
    return {"id": str(inserted)}


def _uuid_or_none(value) -> UUID | None:
    if value in (None, ""):
        return None
    return UUID(str(value))


async def _apply_rules_to_manual(
    pool, user_id: UUID, account_id: UUID, payee_name, notes, amount, tx_date
):
    """Rules apply to manual transactions too; an explicit category or
    note from the caller always wins over automation."""
    from omni.finance.normalisation import normalised_string as _norm

    rules = await load_rules(pool, user_id)
    if not rules:
        return None, None
    account_name = await pool.fetchval(
        "SELECT name FROM finance_account WHERE id = $1", account_id
    )
    category_rows = await pool.fetch(
        "SELECT id, name FROM finance_category WHERE user_id = $1 AND NOT tombstone",
        user_id,
    )
    resolve = {
        "category": {
            _norm(r["name"]): {"id": r["id"], "valid": True} for r in category_rows
        },
        "payee": {},
    }
    tx = {"amount": amount, "date": tx_date.isoformat(), "cleared": True, "notes": notes}
    names = {
        "payee": payee_name,
        "notes": notes,
        "account": account_name,
        "imported_payee": None,
        "category": None,
    }
    tx, _applied = rules_mod.apply_rules(rules, tx, names=names, resolve=resolve)
    applied_category = tx.get("category_id")
    return tx.get("notes"), UUID(str(applied_category)) if applied_category else None


@finance_write
async def update_transaction(
    pool,
    user_id: UUID,
    tx_id: UUID,
    *,
    category_id: UUID | None | _Unset = UNSET,
    notes: str | None | _Unset = UNSET,
    cleared: bool | None = None,
    reconciled: bool | None = None,
) -> None:
    """Patch a transaction.

    `UNSET` means the caller did not send the field; an explicit `None` clears
    it. `cleared`/`reconciled` keep their null-means-omitted coalesce, which
    is correct for flags that have no meaningful null.
    """
    locked = await pool.fetchval(
        "SELECT reconciled FROM finance_transaction WHERE id = $1 AND user_id = $2",
        tx_id,
        user_id,
    )
    if locked is None:
        raise FinanceError("transaction not found")
    if locked and reconciled is not False and (
        category_id is not UNSET or notes is not UNSET or cleared is not None
    ):
        raise FinanceError("a reconciled transaction is locked")
    if category_id is not UNSET and category_id is not None:
        await _require_owned_category(pool, user_id, category_id)
    await pool.execute(
        """
        UPDATE finance_transaction SET
            category_id = CASE WHEN $3 THEN $4::uuid ELSE category_id END,
            notes = CASE WHEN $5 THEN $6::text ELSE notes END,
            cleared = coalesce($7, cleared),
            reconciled = coalesce($8, reconciled),
            updated_at = now()
        WHERE id = $1 AND user_id = $2
        """,
        tx_id,
        user_id,
        category_id is not UNSET,
        category_id if category_id is not UNSET else None,
        notes is not UNSET,
        notes if notes is not UNSET else None,
        cleared,
        reconciled,
    )


@finance_write
async def delete_transaction(pool, user_id: UUID, tx_id: UUID) -> None:
    """Soft-delete a transaction together with everything linked to it.

    A split is one economic unit: deleting the parent and leaving the children
    active keeps them polluting budgets and reports, which filter on
    `is_parent` but not on orphaned children. A transfer is one movement of
    money: deleting one side changes combined wealth. So the linked group --
    parent and children, plus the transfer partner and its own children --
    goes together or not at all, and a reconciled member locks the group.
    """
    row = await pool.fetchrow(
        """
        SELECT parent_id, transfer_id FROM finance_transaction
        WHERE id = $1 AND user_id = $2 AND NOT deleted
        """,
        tx_id,
        user_id,
    )
    if row is None:
        raise FinanceError("transaction not found")
    if row["parent_id"] is not None:
        raise FinanceError("delete the split parent, not a child")

    ids = {tx_id}
    children = await pool.fetch(
        """
        SELECT id FROM finance_transaction
        WHERE parent_id = $1 AND user_id = $2 AND NOT deleted
        """,
        tx_id,
        user_id,
    )
    ids.update(child["id"] for child in children)

    partners = await pool.fetch(
        """
        SELECT t.id FROM finance_transaction t
        WHERE t.user_id = $1 AND NOT t.deleted
          AND t.transfer_id IS NOT NULL
          AND (
              t.transfer_id = ANY($2::uuid[])
              OR t.id IN (
                  SELECT transfer_id FROM finance_transaction
                  WHERE id = ANY($2::uuid[]) AND transfer_id IS NOT NULL
              )
          )
        """,
        user_id,
        list(ids),
    )
    ids.update(partner["id"] for partner in partners)

    partner_children = await pool.fetch(
        """
        SELECT c.id FROM finance_transaction c
        JOIN finance_transaction p ON c.parent_id = p.id
        WHERE p.id = ANY($1::uuid[]) AND c.user_id = $2 AND NOT c.deleted
        """,
        [partner["id"] for partner in partners],
        user_id,
    )
    ids.update(child["id"] for child in partner_children)

    if await pool.fetchval(
        """
        SELECT bool_or(reconciled) FROM finance_transaction
        WHERE id = ANY($1::uuid[]) AND user_id = $2
        """,
        list(ids),
        user_id,
    ):
        raise FinanceError("a reconciled transaction is locked")

    await pool.execute(
        """
        UPDATE finance_transaction SET deleted = true, updated_at = now()
        WHERE id = ANY($1::uuid[]) AND user_id = $2
        """,
        list(ids),
        user_id,
    )


def _int_or_none(value) -> int | None:
    return int(value) if value is not None else None


def _parse_date(value: str) -> str:
    from datetime import datetime

    text = value.strip()
    for pattern in DATE_PATTERNS:
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise FinanceError(f"unrecognised date {text!r}")


def _map_columns(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, column in enumerate(header):
        if "date" not in mapping and DATE_HEADER_RE.search(column):
            mapping["date"] = idx
        elif "payee" not in mapping and PAYEE_HEADER_RE.search(column):
            mapping["payee"] = idx
        elif "amount" not in mapping and AMOUNT_HEADER_RE.search(column):
            mapping["amount"] = idx
        elif "debit" not in mapping and DEBIT_HEADER_RE.search(column):
            mapping["debit"] = idx
        elif "credit" not in mapping and CREDIT_HEADER_RE.search(column):
            mapping["credit"] = idx
        elif "notes" not in mapping and NOTES_HEADER_RE.search(column):
            mapping["notes"] = idx
        elif "imported_id" not in mapping and ID_HEADER_RE.search(column):
            mapping["imported_id"] = idx
    if "date" not in mapping or ("amount" not in mapping and "debit" not in mapping and "credit" not in mapping):
        raise FinanceError(
            "CSV needs a date column and an amount (or debit/credit) column; "
            f"recognised header: {header}"
        )
    return mapping


def parse_csv(text: str) -> list[dict]:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise FinanceError("CSV is empty")
    mapping = _map_columns([c.strip() for c in rows[0]])
    out = []
    for line in rows[1:]:
        if not any(cell.strip() for cell in line):
            continue
        def pick(field):
            idx = mapping.get(field)
            return line[idx].strip() if idx is not None and idx < len(line) else None
        amount = pick("amount")
        if amount is None:
            debit = pick("debit")
            credit = pick("credit")
            if debit:
                amount = f"-{debit}"
            elif credit:
                amount = credit
        out.append({
            "date": _parse_date(pick("date") or ""),
            "payee_name": pick("payee"),
            "amount": amount,
            "notes": pick("notes"),
            "imported_id": pick("imported_id"),
        })
    return out


async def load_rules(pool, user_id: UUID) -> list[rules_mod.Rule]:
    rows = await pool.fetch(
        """
        SELECT id, rank, conditions, actions, enabled FROM finance_rule
        WHERE user_id = $1 ORDER BY rank
        """,
        user_id,
    )
    rules = []
    for r in rows:
        conditions = r["conditions"]
        actions = r["actions"]
        if isinstance(conditions, str):
            conditions = json.loads(conditions)
        if isinstance(actions, str):
            actions = json.loads(actions)
        rules.append(
            rules_mod.Rule(
                id=str(r["id"]),
                rank=int(r["rank"]),
                conditions=conditions,
                actions=actions,
                enabled=bool(r["enabled"]),
            )
        )
    return rules


@finance_write
async def import_transactions(
    pool,
    user_id: UUID,
    *,
    account_id: UUID,
    raw_rows: list[dict],
    preview: bool = False,
) -> dict[str, Any]:
    owner = await pool.fetchval(
        "SELECT user_id FROM finance_account WHERE id = $1", account_id
    )
    if owner != user_id:
        raise FinanceError("account not found")

    normalised = [normalise_import_row(r) for r in raw_rows]
    rules = await load_rules(pool, user_id)
    category_rows = await pool.fetch(
        "SELECT id, name FROM finance_category WHERE user_id = $1 AND NOT tombstone",
        user_id,
    )
    categories = {normalised_string(r["name"]): r["id"] for r in category_rows}
    account_name = await pool.fetchval(
        "SELECT name FROM finance_account WHERE id = $1", account_id
    )
    payee_resolve: dict[str, dict] = {}
    for rule in rules:
        for action in rule.actions:
            if action.get("field") == "payee":
                pid = await resolve_payee(pool, user_id, action.get("value"))
                if pid:
                    payee_resolve[normalised_string(action["value"])] = {
                        "id": pid,
                        "valid": True,
                    }
    category_resolve = {
        key: {"id": str(cid), "valid": True} for key, cid in categories.items()
    }

    prepared = []
    skipped_by_rule = []
    for row in normalised:
        if row["payee_name"] is not None:
            pid = await resolve_payee(pool, user_id, row["payee_name"])
            if pid:
                row["payee_id"] = pid
        else:
            row["payee_id"] = None
        names = {
            "imported_payee": row["imported_payee"],
            "payee": row["payee_name"],
            "notes": row["notes"],
            "category": None,
            "account": account_name,
        }
        resolve = {"category": category_resolve, "payee": payee_resolve}
        tx, _applied = rules_mod.apply_rules(rules, row, names=names, resolve=resolve)
        if tx.get("tombstone"):
            skipped_by_rule.append({"row": {
                "date": tx.get("date"),
                "amount": tx.get("amount"),
                "imported_payee": tx.get("imported_payee"),
            }, "ignored": "deleted by rule"})
            continue
        prepared.append(tx)

    result = await reconcile(
        pool,
        user_id=user_id,
        account_id=account_id,
        rows=prepared,
        is_preview=preview,
    )
    result["preview"] = skipped_by_rule + result["preview"]
    if not preview:
        await pool.execute(
            """
            INSERT INTO finance_import (user_id, account_id, format, filename, added, updated)
            VALUES ($1, $2, 'csv', NULL, $3, $4)
            """,
            user_id,
            account_id,
            len(result["added"]),
            len(result["updated"]),
        )
    return result


async def budget_month(pool, user_id: UUID, month: str) -> dict:
    return await budget_mod.month_view(pool, user_id, budget_mod.month_of(month))


async def set_budget_amount(
    pool, user_id: UUID, *, month: str, category_id: UUID, amount: int,
    rollover_mode: str | None = None,
) -> None:
    category = await pool.fetchval(
        """
        SELECT id FROM finance_category
        WHERE id = $1 AND user_id = $2 AND NOT tombstone
        """,
        category_id,
        user_id,
    )
    if category is None:
        raise FinanceError("category not found")
    if amount < 0:
        raise FinanceError("budget amount cannot be negative")
    await pool.execute(
        """
        INSERT INTO finance_budget (user_id, month, category_id, amount, rollover_mode)
        VALUES ($1, $2, $3, $4, coalesce($5::text, 'rollover'))
        ON CONFLICT (user_id, month, category_id)
        DO UPDATE SET
            amount = excluded.amount,
            rollover_mode = coalesce($5::text, finance_budget.rollover_mode),
            updated_at = now()
        """,
        user_id,
        budget_mod.month_of(month),
        category_id,
        amount,
        rollover_mode,
    )
