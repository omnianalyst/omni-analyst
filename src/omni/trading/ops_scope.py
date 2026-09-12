"""Explicit deployment scope for operational trading scripts.

`SELECT id FROM portfolio LIMIT 1` picks an arbitrary book: on a
multi-portfolio install the ops scripts would operate one book under another
owner's audience. A scope that must be configured, and that must name an
active owner's own book, refuses instead of guessing.
"""

from __future__ import annotations

import os
from uuid import UUID


async def configured_book(pool):
    """Resolve (portfolio_id, created_at, owner) from the environment.

    Requires ``OMNI_TRADING_PORTFOLIO_ID`` and ``OMNI_TRADING_OWNER_ID`` and
    validates that the portfolio belongs to that active user. These variables
    name which book ops scripts may touch; they grant no trading permission
    of their own -- the explicit LIVE gate stays required everywhere.
    """
    try:
        pid = UUID(os.environ["OMNI_TRADING_PORTFOLIO_ID"])
        owner = UUID(os.environ["OMNI_TRADING_OWNER_ID"])
    except (KeyError, ValueError) as exc:
        raise ValueError(
            "OMNI_TRADING_PORTFOLIO_ID and OMNI_TRADING_OWNER_ID must name "
            "the trading book explicitly; refusing to pick one arbitrarily"
        ) from exc
    row = await pool.fetchrow(
        """
        SELECT p.id, p.created_at, p.user_id
        FROM portfolio p JOIN users u ON u.id = p.user_id
        WHERE p.id = $1 AND p.user_id = $2 AND u.active
        """,
        pid,
        owner,
    )
    if row is None:
        raise ValueError(
            "the configured trading book does not belong to that active owner"
        )
    return row["id"], row["created_at"], row["user_id"]
