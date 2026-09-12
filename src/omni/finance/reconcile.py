"""Import reconciliation, ported from Actual Budget (MIT).

Source: packages/loot-core/src/server/accounts/sync.ts
(reconcileTransactions, matchTransactions, normalizeBankSyncTransactions).
Copyright (c) James Long and Actual Budget contributors, MIT license.

The algorithm, kept exactly:

1. Exact pass -- match on (imported_id, account). Highest fidelity.
2. Fuzzy dataset -- same amount, same account, +/- 7 days, sorted by
   distance from the incoming date. Under strict id checking, an incoming
   row that carries an imported_id may only fuzzy-match existing rows
   whose imported_id is NULL (both-id rows that failed the exact pass are
   distinct transactions, never duplicates).
3. Payee pass -- fuzzy match on payee id, then first-unmatched. The
   hasMatched set makes matching one-to-one across the whole batch.
4. Merge -- reconciled rows are locked and ignored; existing values win
   for payee/category/notes, incoming wins for imported_id/imported_payee
   when it carries one (a missing incoming id keeps the stored identity),
   cleared ORs, raw data keeps whichever exists. No write when nothing
   changed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

FUZZY_WINDOW_DAYS = 7
TRANSACTION_SORT_INCREMENT = 5


class ReconcileError(ValueError):
    pass


@dataclass
class MatchedRow:
    row: dict
    match: dict | None = None
    fuzzy_dataset: list[dict] | None = None
    ignored_reason: str | None = None


async def _exact_match(pool, account_id: UUID, imported_id: str | None, *, reimport_deleted: bool) -> dict | None:
    if imported_id is None:
        return None
    row = await pool.fetchrow(
        """
        SELECT id, payee_id, category_id, notes, cleared, reconciled,
               imported_id, imported_payee, date, amount
        FROM finance_transaction
        WHERE account_id = $1 AND imported_id = $2 AND ($3 OR NOT deleted)
        """,
        account_id,
        imported_id,
        reimport_deleted,
    )
    return dict(row) if row else None


async def _fuzzy_dataset(pool, account_id: UUID, row: dict, *, strict: bool) -> list[dict]:
    tx_date = date.fromisoformat(str(row["date"]))
    before = tx_date - timedelta(days=FUZZY_WINDOW_DAYS)
    after = tx_date + timedelta(days=FUZZY_WINDOW_DAYS)
    rows = await pool.fetch(
        """
        SELECT id, payee_id, category_id, notes, cleared, reconciled,
               imported_id, imported_payee, date, amount
        FROM finance_transaction
        WHERE account_id = $1
          AND amount = $2
          AND date >= $3 AND date <= $4
          AND NOT deleted
          AND (imported_id IS NULL OR $5::text IS NULL)
        """,
        account_id,
        row["amount"],
        before,
        after,
        row.get("imported_id") if strict else None,
    )
    dataset = [dict(r) for r in rows]
    dataset.sort(
        key=lambda r: abs((r["date"] - tx_date).days)
    )
    return dataset


async def match_transactions(
    pool,
    account_id: UUID,
    rows: list[dict],
    *,
    strict_id_checking: bool = True,
    reimport_deleted: bool = False,
) -> list[MatchedRow]:
    has_matched: set[UUID] = set()
    step1: list[MatchedRow] = []

    for row in rows:
        entry = MatchedRow(row=row)
        if row.get("imported_id"):
            entry.match = await _exact_match(
                pool, account_id, row["imported_id"], reimport_deleted=reimport_deleted
            )
            if entry.match is not None:
                has_matched.add(entry.match["id"])
        if entry.match is None:
            entry.fuzzy_dataset = await _fuzzy_dataset(
                pool, account_id, row, strict=strict_id_checking
            )
        step1.append(entry)

    step2 = []
    for entry in step1:
        if entry.match is None and entry.fuzzy_dataset is not None:
            payee_id = entry.row.get("payee_id")
            for candidate in entry.fuzzy_dataset:
                if candidate["id"] not in has_matched and candidate["payee_id"] == payee_id:
                    entry.match = candidate
                    has_matched.add(candidate["id"])
                    break
        step2.append(entry)

    step3 = []
    for entry in step2:
        if entry.match is None and entry.fuzzy_dataset is not None:
            for candidate in entry.fuzzy_dataset:
                if candidate["id"] not in has_matched:
                    entry.match = candidate
                    has_matched.add(candidate["id"])
                    break
        step3.append(entry)

    return step3


def _merge_updates(match: dict, row: dict, *, update_dates: bool) -> dict:
    updates = {
        # An incoming row with no id must not erase the strongest dedup key
        # the stored row has: nulling imported_id/imported_payee here would
        # make the next bank sync unable to exact-match, so a missing incoming
        # value keeps the stored one. Deliberate identity correction is a
        # separate operation.
        "imported_id": row.get("imported_id") or match.get("imported_id"),
        "payee_id": match["payee_id"] or row.get("payee_id") or None,
        "category_id": match["category_id"] or row.get("category_id") or None,
        "imported_payee": row.get("imported_payee") or match.get("imported_payee"),
        "notes": match["notes"] or row.get("notes") or None,
        "cleared": bool(match["cleared"]) or bool(row.get("cleared", True)),
    }
    if update_dates and row.get("date"):
        updates["date"] = row["date"]
    changed = any(match.get(k) != v for k, v in updates.items())
    return updates if changed else None


async def reconcile(
    pool,
    *,
    user_id: UUID,
    account_id: UUID,
    rows: list[dict],
    strict_id_checking: bool = True,
    reimport_deleted: bool = False,
    update_dates: bool = False,
    is_preview: bool = False,
) -> dict[str, Any]:
    """rows: normalised import rows, payee_id/category_id already resolved."""
    matched = await match_transactions(
        pool,
        account_id,
        rows,
        strict_id_checking=strict_id_checking,
        reimport_deleted=reimport_deleted,
    )

    added: list[dict] = []
    updated: list[dict] = []
    preview: list[dict] = []

    for entry in matched:
        row = entry.row
        if entry.match is not None and not row.get("force_add"):
            match = entry.match
            if match["reconciled"]:
                preview.append({"row": _preview_row(row), "ignored": "reconciled"})
                continue
            updates = _merge_updates(match, row, update_dates=update_dates)
            if updates is not None:
                updates["id"] = match["id"]
                updated.append(updates)
                preview.append({"row": _preview_row(row), "existing_id": str(match["id"]), "updated": True})
            else:
                preview.append({"row": _preview_row(row), "ignored": "unchanged"})
        else:
            new_tx = {
                "id": uuid4(),
                "user_id": user_id,
                "account_id": account_id,
                "date": row["date"],
                "amount": row["amount"],
                "payee_id": row.get("payee_id"),
                "category_id": row.get("category_id"),
                "imported_id": row.get("imported_id"),
                "imported_payee": row.get("imported_payee"),
                "notes": row.get("notes"),
                "cleared": bool(row.get("cleared", True)),
                "raw_import": row.get("raw"),
            }
            added.append(new_tx)
            preview.append({"row": _preview_row(row), "added": True})

    now_ms = int(time.time() * 1000)
    for idx, tx in enumerate(added):
        tx["sort_order"] = now_ms - idx * TRANSACTION_SORT_INCREMENT

    if not is_preview:
        for updates in updated:
            sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(
                [k for k in updates if k != "id"]
            ))
            values = [updates["id"]] + [updates[k] for k in updates if k != "id"]
            await pool.execute(
                f"UPDATE finance_transaction SET {sets}, updated_at = now() WHERE id = $1",
                *values,
            )
        for tx in added:
            await pool.execute(
                """
                INSERT INTO finance_transaction
                    (id, user_id, account_id, payee_id, category_id, date, amount,
                     notes, cleared, imported_id, imported_payee, sort_order, raw_import)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb)
                """,
                tx["id"], tx["user_id"], tx["account_id"], tx["payee_id"],
                tx["category_id"],
                date.fromisoformat(str(tx["date"])[:10]),
                tx["amount"], tx["notes"],
                tx["cleared"], tx["imported_id"], tx["imported_payee"],
                tx["sort_order"],
                _json_dumps(tx.get("raw_import")),
            )

    return {
        "added": [str(t["id"]) for t in added],
        "updated": [str(u["id"]) for u in updated],
        "preview": preview,
    }


def _preview_row(row: dict) -> dict:
    return {
        "date": row.get("date"),
        "amount": row.get("amount"),
        "imported_id": row.get("imported_id"),
        "imported_payee": row.get("imported_payee"),
        "payee_name": row.get("payee_name"),
    }


def _json_dumps(value) -> str | None:
    import json

    if value is None:
        return None
    return json.dumps(value) if not isinstance(value, str) else value