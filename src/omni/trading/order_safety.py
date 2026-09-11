"""The boundary between an uncertain execution and a terminal one.

2026-08-19, live: a terse submit response left orders resting on Hyperliquid
while the ledger recorded them REJECTED, and a supposedly dead order filled
later outside the recorded book. A rejection is a terminal answer -- nothing
ever revisits it -- so recording one for an order whose fate is unknown is the
most expensive lie in this module's reach. The honest states are:

- the venue **refused or expired the order with nothing filled** -- a terminal
  empty fill whose raw carries `rejected`. REJECTED stays correct.
- the venue **raised `VenueUnavailable`**, including after a placement that may
  have reached the matching engine -- the order is transitioned to
  ACKNOWLEDGED, which is durable and non-terminal, and the cycle halts.
- the venue **answered with a resting order** -- an empty fill whose raw
  carries `resting` and an external id. Same ACKNOWLEDGED-and-halt.

`require_settled_orders` then keeps every later cycle from trading while any
order sits in submitted/acknowledged/partially_filled: an unresolved order is
an operator decision, not a state the loop is allowed to trade around, and the
cycle that resumes after one is resolved is the cycle that can trust its book.
"""

from __future__ import annotations

from uuid import UUID

from omni.portfolio import orders
from omni.portfolio.orders import OrderStatus
from omni.venue.protocol import Fill, TradeIntent, Venue, VenueUnavailable

_UNSETTLED = """
SELECT id, status, idempotency_key FROM trade_order
WHERE portfolio_id = $1
  AND status IN ('submitted', 'acknowledged', 'partially_filled')
ORDER BY created_at, id
"""


class ExecutionUncertain(Exception):
    """The outcome of a live order cannot be established, so trading halts."""


async def require_settled_orders(pool, portfolio_id: UUID) -> None:
    rows = await pool.fetch(_UNSETTLED, portfolio_id)
    if rows:
        named = ", ".join(
            f"{row['idempotency_key']} ({row['status']})" for row in rows[:5]
        )
        raise ExecutionUncertain(
            f"portfolio {portfolio_id} has {len(rows)} order(s) in a non-terminal "
            f"state ({named}); trading resumes only after an operator reconciles "
            f"them against the venue"
        )


async def execute_or_halt(
    pool, order_id: UUID, intent: TradeIntent, venue: Venue
) -> Fill:
    """Execute one intent, or park its order durably open and halt the cycle.

    A terminal empty fill (the venue declined the order) is returned as-is for
    the caller's own REJECTED recording. Everything uncertain raises
    `ExecutionUncertain` after transitioning the order to ACKNOWLEDGED, so the
    order keeps its external id and the halt has a durable record behind it.
    """
    try:
        fill = await venue.execute(intent)
    except VenueUnavailable as exc:
        await orders.transition(
            pool,
            order_id,
            OrderStatus.ACKNOWLEDGED,
            payload={"execution_uncertain": str(exc)},
        )
        raise ExecutionUncertain(
            f"{venue.name} could not be asked what became of order {order_id} "
            f"({intent.symbol}, client order id {intent.idempotency_key}): {exc}"
        ) from exc

    if fill.is_empty and fill.raw.get("resting"):
        await orders.transition(
            pool,
            order_id,
            OrderStatus.ACKNOWLEDGED,
            external_id=fill.external_id,
            payload={"resting": fill.raw},
        )
        raise ExecutionUncertain(
            f"{venue.name} left order {order_id} ({intent.symbol}, client order "
            f"id {intent.idempotency_key}) resting on the book"
            + (
                f" as external id {fill.external_id}"
                if fill.external_id is not None
                else ""
            )
            + "; it is recorded acknowledged, not rejected, and the cycle halts"
        )

    return fill
