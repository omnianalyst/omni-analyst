"""Transfer mirroring, ported from Actual Budget (MIT).

Source: packages/loot-core/src/server/transactions/transfer.ts.
Copyright (c) James Long and Actual Budget contributors, MIT license.

A payee whose transfer_acct points at another account marks a transfer:
the opposite side is written as the negated amount, both rows link via
transfer_id, and the category is cleared when both sides sit on the same
side of the budget (on/on or off/off) -- a category there would double
count.
"""

from __future__ import annotations

from uuid import UUID


async def _payee_for_account(pool, user_id: UUID, account_id: UUID) -> UUID | None:
    existing = await pool.fetchval(
        """
        SELECT id FROM finance_payee
        WHERE user_id = $1 AND transfer_acct = $2 AND NOT tombstone
        """,
        user_id,
        account_id,
    )
    if existing:
        return existing
    name = await pool.fetchval(
        "SELECT name FROM finance_account WHERE id = $1", account_id
    )
    if name is None:
        return None
    return await pool.fetchval(
        """
        INSERT INTO finance_payee (user_id, name, transfer_acct)
        VALUES ($1, $2, $3) RETURNING id
        """,
        user_id,
        name,
        account_id,
    )


async def create_transfer_side(
    pool, *, user_id: UUID, tx_row: dict
) -> UUID | None:
    """Write the mirror transaction for a transfer-marked tx. Returns
    the mirror id, or None when the tx has no transfer payee."""
    transfer_acct = tx_row.get("transfer_acct")
    if transfer_acct is None:
        return None

    mirror_payee = await _payee_for_account(pool, user_id, tx_row["account_id"])
    if mirror_payee is None:
        return None

    mirror_id = await pool.fetchval(
        """
        INSERT INTO finance_transaction
            (user_id, account_id, payee_id, category_id, date, amount, notes,
             cleared, transfer_id, sort_order)
        VALUES ($1, $2, $3, NULL, $4, $5, $6, false, $7, 0)
        RETURNING id
        """,
        user_id,
        transfer_acct,
        mirror_payee,
        tx_row["date"],
        -tx_row["amount"],
        tx_row.get("notes"),
        tx_row["id"],
    )
    await pool.execute(
        "UPDATE finance_transaction SET transfer_id = $1 WHERE id = $2",
        mirror_id,
        tx_row["id"],
    )

    from_offbudget = await pool.fetchval(
        "SELECT offbudget FROM finance_account WHERE id = $1", tx_row["account_id"]
    )
    to_offbudget = await pool.fetchval(
        "SELECT offbudget FROM finance_account WHERE id = $1", transfer_acct
    )
    if from_offbudget == to_offbudget:
        await pool.execute(
            "UPDATE finance_transaction SET category_id = NULL WHERE id = $1",
            tx_row["id"],
        )
    return mirror_id
