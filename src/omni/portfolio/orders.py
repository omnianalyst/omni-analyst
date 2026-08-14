"""The order ledger: what was instructed, what the venue said, what filled.

Portfolio state is derived from this table, so the ledger is the source of
truth and everything downstream is a projection. Two properties make that
tenable, and both are enforced here rather than left to callers:

**Recording an intent is idempotent on the intent's own key.** A trading loop
that crashes between the ledger write and the venue call restarts and records
the same intent again; a scheduler tick that overlaps its predecessor does the
same. Either produces a second order for one decision -- double size, unnoticed
-- unless the second write collapses onto the first. `record_intent` uses
`ON CONFLICT` on the named unique constraint, not a SELECT followed by an
INSERT, because the check-then-act version has a window between the two in
which a concurrent writer inserts and both callers proceed.

**A fill is accumulated, never assigned.** `average_fill_price` is recomputed
as a quantity-weighted average across every fill the order has received.
Overwriting it with the latest fill's price is silent on a fully filled order
and wrong on every partially filled one -- a 6-at-100 then 4-at-150 order
whose average reads 150 misstates its own entry by 25%, and every P&L, stop
distance and reconciliation check computed from it inherits the error.

The status enum mirrors the SQL type in migration 034 exactly;
`tests/test_order_ledger.py` asserts the two member lists are identical so an
addition to one fails loudly instead of drifting. The legal transition map is
declared rather than inferred: an order may not move backwards (a filled order
cannot become submitted again) and the three terminal states absorb. A fill on
an order that was never submitted is refused for the same reason -- the trail
would claim an execution that has no record of being sent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from omni.venue.protocol import Fill, MarketType, OrderKind, Side, TradeIntent


class OrderLedgerError(Exception):
    """The ledger refused to record something."""


class IllegalTransition(OrderLedgerError):
    """A status change the lifecycle does not permit.

    Raised rather than recorded. A ledger that accepts `filled -> submitted`
    cannot be replayed into a position, because the replay has no way to know
    which of the two contradictory states was real.
    """


class UnknownOrder(OrderLedgerError):
    """No order exists with that id.

    Raised rather than treated as a no-op: a transition or fill against an
    order the ledger has never seen is a lost write somewhere upstream, and
    swallowing it hides exactly the thing reconciliation exists to find.
    """


class OrderStatus(str, Enum):
    INTENT = "intent"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


OPEN_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.INTENT,
        OrderStatus.SUBMITTED,
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.PARTIALLY_FILLED,
    }
)

# Declared, not derived from an ordering. `partially_filled -> partially_filled`
# is deliberate: a second partial fill is a real transition and gets its own
# event. The three terminal states map to the empty set, which is what makes a
# fill on a cancelled order raise instead of resurrecting it.
LEGAL_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.INTENT: frozenset(
        {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.ACKNOWLEDGED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class Order:
    """A row of the ledger, with money as `Decimal` all the way through."""

    id: UUID
    portfolio_id: UUID
    idempotency_key: str
    venue: str
    symbol: str
    side: Side
    market_type: MarketType
    order_kind: OrderKind
    quantity: Decimal
    reference_price: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    expires_at: datetime | None
    status: OrderStatus
    external_id: str | None
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    fee_paid: Decimal
    provenance: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


_ORDER_COLUMNS = """
    id, portfolio_id, idempotency_key, venue, symbol, side, market_type,
    order_kind, quantity, reference_price, limit_price, stop_price,
    take_profit_price, expires_at, status, external_id, filled_quantity,
    average_fill_price, fee_paid, provenance, created_at, updated_at
"""

# ON CONFLICT DO NOTHING rather than DO UPDATE: a retry must not touch the
# original row at all. The constraint is named because inferring it from the
# column list would silently start matching a different index if one were ever
# added on idempotency_key.
_INSERT_ORDER = f"""
INSERT INTO trade_order (
    portfolio_id, idempotency_key, venue, symbol, side, market_type, order_kind,
    quantity, reference_price, limit_price, stop_price, take_profit_price,
    expires_at, provenance
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb)
ON CONFLICT ON CONSTRAINT trade_order_idempotency_key_unique DO NOTHING
RETURNING {_ORDER_COLUMNS}
"""

_SELECT_BY_KEY = f"SELECT {_ORDER_COLUMNS} FROM trade_order WHERE idempotency_key = $1"

_SELECT_BY_ID = f"SELECT {_ORDER_COLUMNS} FROM trade_order WHERE id = $1"

_SELECT_OPEN = f"""
SELECT {_ORDER_COLUMNS} FROM trade_order
WHERE portfolio_id = $1
  AND status IN ('intent','submitted','acknowledged','partially_filled')
ORDER BY created_at, id
"""

_LOCK_ORDER = """
SELECT id, status, quantity, filled_quantity, average_fill_price, fee_paid
FROM trade_order WHERE id = $1 FOR UPDATE
"""

_INSERT_EVENT = """
INSERT INTO order_event (order_id, status, external_id, payload)
VALUES ($1, $2::order_status, $3, $4::jsonb)
"""

_UPDATE_STATUS = """
UPDATE trade_order
SET status = $2::order_status,
    external_id = COALESCE($3, external_id),
    updated_at = now()
WHERE id = $1
"""

_UPDATE_FILL = """
UPDATE trade_order
SET status = $2::order_status,
    filled_quantity = $3,
    average_fill_price = $4,
    fee_paid = $5,
    external_id = COALESCE($6, external_id),
    updated_at = now()
WHERE id = $1
"""


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        # str, not float: the payload is the audit record of a price, and
        # binary rounding it on the way in makes the record disagree with the
        # NUMERIC column beside it.
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"{type(value).__name__} is not recordable in an order payload")


def _dump(payload: Any) -> str:
    return json.dumps(payload, default=_json_default)


def _load_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (str, bytes)):
        return json.loads(raw)
    return raw


def _row_to_order(row: Any) -> Order:
    return Order(
        id=row["id"],
        portfolio_id=row["portfolio_id"],
        idempotency_key=row["idempotency_key"],
        venue=row["venue"],
        symbol=row["symbol"],
        side=Side(row["side"]),
        market_type=MarketType(row["market_type"]),
        order_kind=OrderKind(row["order_kind"]),
        quantity=row["quantity"],
        reference_price=row["reference_price"],
        limit_price=row["limit_price"],
        stop_price=row["stop_price"],
        take_profit_price=row["take_profit_price"],
        expires_at=row["expires_at"],
        status=OrderStatus(row["status"]),
        external_id=row["external_id"],
        filled_quantity=row["filled_quantity"],
        average_fill_price=row["average_fill_price"],
        fee_paid=row["fee_paid"],
        provenance=_load_json(row["provenance"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _intent_payload(intent: TradeIntent) -> dict[str, Any]:
    return {
        "idempotency_key": intent.idempotency_key,
        "venue": intent.venue,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "market_type": intent.market_type.value,
        "order_kind": intent.order_kind.value,
        "quantity": str(intent.quantity),
        "reference_price": str(intent.reference_price),
        "limit_price": None if intent.limit_price is None else str(intent.limit_price),
        "stop_price": None if intent.stop_price is None else str(intent.stop_price),
        "take_profit_price": (
            None if intent.take_profit_price is None else str(intent.take_profit_price)
        ),
        "expires_at": None if intent.expires_at is None else intent.expires_at.isoformat(),
        "reduce_only": intent.reduce_only,
    }


def _check_transition(current: OrderStatus, target: OrderStatus) -> None:
    if target not in LEGAL_TRANSITIONS[current]:
        raise IllegalTransition(
            f"{current.value} -> {target.value} is not a legal order transition; "
            f"from {current.value} the ledger permits "
            f"{sorted(s.value for s in LEGAL_TRANSITIONS[current])}"
        )


async def record_intent(pool, portfolio_id: UUID, intent: TradeIntent) -> UUID:
    """Write the intent to the ledger before it reaches a venue. Idempotent.

    Returns the order id. Called twice with the same `intent.idempotency_key`
    it returns the id of the first order and writes nothing at all -- no second
    row, no second `intent` event. That is the property a restarted trading
    loop depends on: it re-records what it was about to do, sees the same id,
    and continues instead of doubling the size.

    The uniqueness is resolved by the database, not by a preceding SELECT. Two
    workers racing on the same key both reach the INSERT; one wins and the
    other's `ON CONFLICT DO NOTHING` returns no row, at which point it reads
    back the committed winner.
    """
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            _INSERT_ORDER,
            portfolio_id,
            intent.idempotency_key,
            intent.venue,
            intent.symbol,
            intent.side.value,
            intent.market_type.value,
            intent.order_kind.value,
            intent.quantity,
            intent.reference_price,
            intent.limit_price,
            intent.stop_price,
            intent.take_profit_price,
            intent.expires_at,
            _dump(intent.provenance),
        )
        if row is None:
            existing = await conn.fetchrow(_SELECT_BY_KEY, intent.idempotency_key)
            if existing is None:
                # The conflicting insert rolled back after we lost the race, so
                # neither order exists. Refuse rather than return an id that is
                # not in the ledger; the caller retries.
                raise OrderLedgerError(
                    f"idempotency_key {intent.idempotency_key!r} conflicted but no "
                    f"order is present; the competing write did not commit"
                )
            return existing["id"]

        await conn.execute(
            _INSERT_EVENT,
            row["id"],
            OrderStatus.INTENT.value,
            None,
            _dump({"intent": _intent_payload(intent), "provenance": intent.provenance}),
        )
        return row["id"]


async def transition(
    pool,
    order_id: UUID,
    status: OrderStatus | str,
    *,
    external_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Move an order to `status`, recording the event and the raw payload.

    The event insert and the status update share one transaction under a row
    lock, so the log can never disagree with the current state -- a reader
    seeing `filled` always finds the event that made it so.

    `payload` is whatever the venue actually said: the submission body, the
    acknowledgement, the rejection reason. It is stored verbatim because a
    rejection summarised into a status is a rejection whose cause is gone.
    """
    target = OrderStatus(status)
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(_LOCK_ORDER, order_id)
        if row is None:
            raise UnknownOrder(f"no order {order_id} in the ledger")

        _check_transition(OrderStatus(row["status"]), target)

        await conn.execute(
            _INSERT_EVENT,
            order_id,
            target.value,
            external_id,
            None if payload is None else _dump(payload),
        )
        await conn.execute(_UPDATE_STATUS, order_id, target.value, external_id)


async def record_fill(pool, order_id: UUID, fill: Fill) -> None:
    """Accumulate a fill into the order, weighting the average entry price.

    `average_fill_price` becomes the quantity-weighted mean over every fill so
    far, not the price of this one. The status becomes `filled` once the
    accumulated quantity equals the order's quantity and `partially_filled`
    below it. A fill that would take the accumulated quantity above the order
    quantity is refused before the ledger is mutated.

    No tolerance is applied to that comparison: `filled_quantity` is an exact
    Decimal sum of exact Decimal fills, so a shortfall of 1e-18 is a real
    shortfall reported by the venue and not the residue of binary arithmetic.

    Non-finite quantities are refused before anything is read. A Decimal NaN
    raises on any ordered comparison, so `Fill` already rejects it at
    construction -- but an infinity satisfies every one of its guards
    (`inf < 0` is false, `inf > 0` is true, `inf <= 0` is false) and would
    reach the weighted average, where it turns the order's entry price into
    NaN and every P&L derived from it with it.
    """
    for name in ("filled_quantity", "average_price", "fee_paid"):
        value = getattr(fill, name)
        if not value.is_finite():
            raise ValueError(f"fill {name} is not a finite quantity: {value}")

    if fill.filled_quantity <= 0:
        raise ValueError(
            f"an empty fill is not a fill: filled_quantity={fill.filled_quantity}; "
            f"a venue that executed nothing has nothing to record against the order"
        )

    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(_LOCK_ORDER, order_id)
        if row is None:
            raise UnknownOrder(f"no order {order_id} in the ledger")

        prior_quantity: Decimal = row["filled_quantity"]
        prior_average: Decimal | None = row["average_fill_price"]
        order_quantity: Decimal = row["quantity"]

        accumulated = prior_quantity + fill.filled_quantity
        target = (
            OrderStatus.FILLED
            if accumulated >= order_quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        _check_transition(OrderStatus(row["status"]), target)
        if accumulated > order_quantity:
            raise OrderLedgerError(
                f"fill of {fill.filled_quantity} would overfill order {order_id}: "
                f"{prior_quantity} already filled of {order_quantity} ordered"
            )

        prior_notional = (
            Decimal(0) if prior_average is None else prior_average * prior_quantity
        )
        average = (
            prior_notional + fill.average_price * fill.filled_quantity
        ) / accumulated

        await conn.execute(
            _INSERT_EVENT,
            order_id,
            target.value,
            fill.external_id,
            _dump(
                {
                    "fill": {
                        "filled_quantity": fill.filled_quantity,
                        "average_price": fill.average_price,
                        "fee_paid": fill.fee_paid,
                        "filled_at": fill.filled_at,
                        "venue": fill.venue,
                        "symbol": fill.symbol,
                        "side": fill.side.value,
                    },
                    "raw": fill.raw,
                }
            ),
        )
        await conn.execute(
            _UPDATE_FILL,
            order_id,
            target.value,
            accumulated,
            average,
            row["fee_paid"] + fill.fee_paid,
            fill.external_id,
        )


async def get(pool, order_id: UUID) -> Order | None:
    row = await pool.fetchrow(_SELECT_BY_ID, order_id)
    return None if row is None else _row_to_order(row)


async def open_orders(pool, portfolio_id: UUID) -> list[Order]:
    """Every order for the portfolio that a venue could still act on."""
    rows = await pool.fetch(_SELECT_OPEN, portfolio_id)
    return [_row_to_order(row) for row in rows]
