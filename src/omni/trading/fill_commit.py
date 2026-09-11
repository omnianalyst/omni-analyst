"""Record a fill and apply it to the book in one transaction.

`orders.record_fill` and `state.apply_fill` each acquire their own connection
and transaction. Called back to back, a crash between them leaves a fill the
ledger says happened against a portfolio it never moved, and on restart the
order reads FILLED so nothing re-applies it. Here both run on one connection
inside one transaction, with the portfolio row locked first so concurrent fills
serialise. The inner `conn.transaction()` blocks of the callees become
savepoints on this transaction and roll back with it.
"""

from __future__ import annotations

from uuid import UUID

from omni.db_scope import BoundPool
from omni.portfolio import orders, state
from omni.portfolio.state import PortfolioState
from omni.venue.protocol import Fill, MarketType

_LOCK_PORTFOLIO = "SELECT 1 FROM portfolio WHERE id = $1 FOR UPDATE"


async def record_and_apply_fill(
    pool,
    order_id: UUID,
    portfolio_id: UUID,
    fill: Fill,
    market_type: MarketType,
) -> PortfolioState:
    async with pool.acquire() as conn, conn.transaction():
        if await conn.fetchval(_LOCK_PORTFOLIO, portfolio_id) is None:
            raise state.UnknownPortfolio(f"no portfolio {portfolio_id}")
        await orders.record_fill(BoundPool(conn), order_id, fill)
        return await state.apply_fill(BoundPool(conn), portfolio_id, fill, market_type)
