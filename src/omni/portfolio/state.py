"""What is held, what it cost, and what it is worth -- materialised from fills.

The position rows are a *derivation*, never an independent record. Every rule
that moves them lives in `_next_position`, and `rebuild_from_fills` replays the
same rule over the same fills without touching the database, so the two can be
compared. When they disagree the rows are wrong and the fills are right; that
ordering is the whole reason this module exists rather than a table of
positions somebody updates by hand.

Three rules decide an entry price, and the third is the one that gets written
wrong:

- **Adding** to a position averages the entry, weighted by quantity.
- **Reducing** keeps the original entry. The realised P&L of the closed part
  belongs to the trade ledger, not to the entry of what is still open.
- **Flipping** the sign takes the new fill's price. Averaging across a flip
  produces an entry the position never had -- a long at 100 sold through to a
  short would carry a blended entry that sits on the wrong side of the market,
  and every stop, mark and unrealised P&L computed from it is wrong by the
  size of the flip.

`nav` on `PortfolioState` is a **cost-basis** figure: cash plus positions at
their average entry. It is exact and needs no market data, which is what makes
it safe to return from a write path. The marked NAV comes from `snapshot_nav`,
which requires a mark for every position and raises when one is missing --
valuing an unmarked position at its entry reports an unrealised P&L of exactly
zero for the position most likely to have moved, and a NAV that is wrong in a
direction nobody can see.

Cash settles in the portfolio's `base_currency` at the fill's venue, and only
base-currency rows count toward `cash`. A balance in another asset is not
converted here: that needs an FX rate, and inventing one would put a fabricated
number directly into NAV.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from omni.venue.protocol import Fill, MarketType, Position, Side


class UnknownPortfolio(Exception):
    """No portfolio row for the id given.

    Raised rather than returning an empty state, which would let a caller
    reconcile against a portfolio that does not exist and find it agrees.
    """


class UnmarkedPosition(Exception):
    """A held position has no usable mark, so NAV cannot be computed."""


_PORTFOLIO = "SELECT base_currency FROM portfolio WHERE id = $1"

_PORTFOLIO_LOCKED = "SELECT base_currency FROM portfolio WHERE id = $1 FOR UPDATE"

# COLLATE "C" so the stored order is byte order, which is the order Python's
# str comparison gives. Under a locale collation "BTC-PERP" and "BTCPERP" sort
# by a rule that ignores the punctuation, and the replayed tuple would differ
# from the loaded one by ordering alone -- a reconciliation failure with no
# underlying disagreement.
_POSITIONS = """
SELECT venue, symbol, market_type, quantity, average_entry, updated_at
FROM position
WHERE portfolio_id = $1
ORDER BY venue COLLATE "C", symbol COLLATE "C", market_type COLLATE "C"
"""

_CASH = """
SELECT COALESCE(SUM(free + locked), 0)
FROM cash_balance
WHERE portfolio_id = $1 AND asset = $2
"""

_LOCK_POSITION = """
SELECT quantity, average_entry
FROM position
WHERE portfolio_id = $1 AND venue = $2 AND symbol = $3 AND market_type = $4
FOR UPDATE
"""

_UPSERT_POSITION = """
INSERT INTO position (portfolio_id, venue, symbol, market_type,
                      quantity, average_entry, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (portfolio_id, venue, symbol, market_type)
DO UPDATE SET quantity = EXCLUDED.quantity,
              average_entry = EXCLUDED.average_entry,
              updated_at = EXCLUDED.updated_at
"""

_DELETE_POSITION = """
DELETE FROM position
WHERE portfolio_id = $1 AND venue = $2 AND symbol = $3 AND market_type = $4
"""

_UPSERT_CASH = """
INSERT INTO cash_balance (portfolio_id, venue, asset, free, updated_at)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (portfolio_id, venue, asset)
DO UPDATE SET free = cash_balance.free + EXCLUDED.free,
              updated_at = EXCLUDED.updated_at
"""

_INSERT_NAV = """
INSERT INTO nav_snapshot (portfolio_id, nav, cash, gross_exposure, net_exposure)
VALUES ($1, $2, $3, $4, $5)
"""


@dataclass(frozen=True)
class PortfolioState:
    """Positions, cash and cost-basis NAV as of a moment.

    `gross_exposure` and `net_exposure` are stated at average entry, because
    that is what the stored rows contain. The marked pair is written by
    `snapshot_nav`, which has the prices.
    """

    portfolio_id: UUID
    nav: Decimal
    cash: Decimal
    positions: tuple[Position, ...]
    as_of: datetime

    @property
    def gross_exposure(self) -> Decimal:
        return sum((p.notional for p in self.positions), Decimal(0))

    @property
    def net_exposure(self) -> Decimal:
        return sum(
            (p.quantity * p.average_entry for p in self.positions), Decimal(0)
        )

    def position_for(
        self, venue: str, symbol: str, market_type: MarketType
    ) -> Position | None:
        for p in self.positions:
            if p.venue == venue and p.symbol == symbol and p.market_type == market_type:
                return p
        return None


def _next_position(
    *,
    quantity: Decimal,
    average_entry: Decimal,
    side: Side,
    filled_quantity: Decimal,
    price: Decimal,
) -> tuple[Decimal, Decimal]:
    """The one place the averaging rules live, shared by both derivations.

    Returns `(quantity, average_entry)` after the fill. A returned quantity of
    zero means the position is closed and its row must be deleted, so the
    entry returned with it carries no meaning.

    The `== 0` comparisons are exact: quantities are `Decimal`, and addition
    and subtraction of `Decimal` are exact, so a position closed by fills that
    sum to zero lands on exactly zero rather than near it.
    """
    delta = filled_quantity if side is Side.BUY else -filled_quantity
    new_quantity = quantity + delta

    if new_quantity == 0:
        return new_quantity, Decimal(0)

    if quantity == 0 or (quantity > 0) != (new_quantity > 0):
        return new_quantity, price

    if (delta > 0) == (quantity > 0):
        blended = abs(quantity) * average_entry + abs(delta) * price
        return new_quantity, blended / abs(new_quantity)

    return new_quantity, average_entry


def _cash_delta(fill: Fill) -> Decimal:
    """Cash-account settlement: a buy pays out, a sell takes in, fees always cost."""
    proceeds = fill.notional if fill.side is Side.SELL else -fill.notional
    return proceeds - fill.fee_paid


def _checked(fill: Fill, market_type: MarketType) -> MarketType:
    if fill.filled_at.tzinfo is None:
        raise ValueError(
            f"fill {fill.intent_id} has a naive filled_at ({fill.filled_at}); "
            f"the stored timestamp is UTC and a naive one silently shifts it"
        )
    return MarketType(market_type)


def _state(
    portfolio_id: UUID, cash: Decimal, positions: tuple[Position, ...]
) -> PortfolioState:
    book = sum((p.quantity * p.average_entry for p in positions), Decimal(0))
    return PortfolioState(
        portfolio_id=portfolio_id,
        nav=cash + book,
        cash=cash,
        positions=positions,
        as_of=datetime.now(UTC),
    )


async def _load_on_conn(conn, portfolio_id: UUID) -> PortfolioState:
    base_currency = await conn.fetchval(_PORTFOLIO, portfolio_id)
    if base_currency is None:
        raise UnknownPortfolio(f"no portfolio {portfolio_id}")

    rows = await conn.fetch(_POSITIONS, portfolio_id)
    cash = await conn.fetchval(_CASH, portfolio_id, base_currency)
    positions = tuple(
        Position(
            venue=row["venue"],
            symbol=row["symbol"],
            market_type=MarketType(row["market_type"]),
            quantity=row["quantity"],
            average_entry=row["average_entry"],
            as_of=row["updated_at"],
        )
        for row in rows
    )
    return _state(portfolio_id, cash, positions)


async def load(pool, portfolio_id: UUID) -> PortfolioState:
    """Read the materialised state. Raises `UnknownPortfolio` if there is none."""
    async with pool.acquire() as conn, conn.transaction():
        return await _load_on_conn(conn, portfolio_id)


async def apply_fill(
    pool, portfolio_id: UUID, fill: Fill, market_type: MarketType
) -> PortfolioState:
    """Move the position and the cash for one fill, in one transaction.

    Both sides commit together or neither does. A position updated without its
    cash leg is a portfolio that gained an asset for free, and the NAV computed
    from it is wrong by the notional of the trade.

    The portfolio row is locked for the duration, so two fills against the same
    portfolio serialise rather than both reading the pre-fill quantity and one
    overwriting the other.
    """
    resolved = _checked(fill, market_type)

    async with pool.acquire() as conn, conn.transaction():
        base_currency = await conn.fetchval(_PORTFOLIO_LOCKED, portfolio_id)
        if base_currency is None:
            raise UnknownPortfolio(f"no portfolio {portfolio_id}")

        key = (portfolio_id, fill.venue, fill.symbol, resolved.value)
        held = await conn.fetchrow(_LOCK_POSITION, *key)
        quantity = held["quantity"] if held is not None else Decimal(0)
        average_entry = held["average_entry"] if held is not None else Decimal(0)

        new_quantity, new_entry = _next_position(
            quantity=quantity,
            average_entry=average_entry,
            side=fill.side,
            filled_quantity=fill.filled_quantity,
            price=fill.average_price,
        )

        if new_quantity == 0:
            if held is not None:
                await conn.execute(_DELETE_POSITION, *key)
        else:
            await conn.execute(
                _UPSERT_POSITION, *key, new_quantity, new_entry, fill.filled_at
            )

        await conn.execute(
            _UPSERT_CASH,
            portfolio_id,
            fill.venue,
            base_currency,
            _cash_delta(fill),
            fill.filled_at,
        )

        return await _load_on_conn(conn, portfolio_id)


def _marked_value(position: Position, marks: dict[str, Decimal]) -> Decimal:
    mark = marks.get(position.symbol)
    if mark is None:
        raise UnmarkedPosition(
            f"no mark for {position.symbol} at {position.venue}; NAV is not "
            f"computable and valuing it at entry would report zero P&L on it"
        )
    if mark.is_nan() or mark.is_infinite():
        raise UnmarkedPosition(f"mark for {position.symbol} is not a number: {mark}")
    if mark <= 0:
        raise UnmarkedPosition(f"mark for {position.symbol} is not a price: {mark}")
    return position.quantity * mark


async def snapshot_nav(
    pool, portfolio_id: UUID, marks: dict[str, Decimal]
) -> Decimal:
    """Mark the book and record the NAV. Every held symbol needs a mark.

    A missing, non-finite or non-positive mark raises `UnmarkedPosition` and no
    snapshot row is written. A partially-marked NAV is worse than no NAV: it
    reads as authoritative and understates exactly the exposure nobody could
    price.
    """
    async with pool.acquire() as conn, conn.transaction():
        state = await _load_on_conn(conn, portfolio_id)

        values = [_marked_value(p, marks) for p in state.positions]
        net = sum(values, Decimal(0))
        gross = sum((abs(v) for v in values), Decimal(0))
        nav = state.cash + net

        await conn.execute(_INSERT_NAV, portfolio_id, nav, state.cash, gross, net)
        return nav


async def rebuild_from_fills(
    pool,
    portfolio_id: UUID,
    fills: Iterable[tuple[Fill, MarketType]],
) -> PortfolioState:
    """Replay fills into state without touching the stored rows.

    Each fill is paired with its market type because a `Fill` does not carry
    one, and a default would silently merge a spot holding with a perpetual of
    the same symbol -- netting a hedge into nothing.

    The cash returned is the net cash *movement* the fills imply, starting from
    zero. It equals the stored cash only for a portfolio whose entire history
    is the fills given: deposits, withdrawals and funding payments are not
    fills and are not replayed here.
    """
    async with pool.acquire() as conn:
        if await conn.fetchval(_PORTFOLIO, portfolio_id) is None:
            raise UnknownPortfolio(f"no portfolio {portfolio_id}")

    held: dict[tuple[str, str, MarketType], tuple[Decimal, Decimal, datetime]] = {}
    cash = Decimal(0)

    for fill, market_type in fills:
        resolved = _checked(fill, market_type)
        key = (fill.venue, fill.symbol, resolved)
        quantity, average_entry, _ = held.get(key, (Decimal(0), Decimal(0), None))

        new_quantity, new_entry = _next_position(
            quantity=quantity,
            average_entry=average_entry,
            side=fill.side,
            filled_quantity=fill.filled_quantity,
            price=fill.average_price,
        )
        cash += _cash_delta(fill)

        if new_quantity == 0:
            held.pop(key, None)
        else:
            held[key] = (new_quantity, new_entry, fill.filled_at)

    positions = tuple(
        Position(
            venue=venue,
            symbol=symbol,
            market_type=market_type,
            quantity=quantity,
            average_entry=average_entry,
            as_of=updated_at,
        )
        for (venue, symbol, market_type), (
            quantity,
            average_entry,
            updated_at,
        ) in sorted(held.items(), key=lambda item: (item[0][0], item[0][1], item[0][2].value))
    )
    return _state(portfolio_id, cash, positions)
