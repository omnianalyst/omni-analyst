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

Marks are keyed by `(venue, symbol)`, because positions are. One symbol held at
two venues is two positions with two prices, and the whole point of a
cross-venue basis trade is that those two prices differ; marking both legs at
one price reports an unrealised P&L of exactly zero for the only position whose
P&L *is* the spread. A position with no mark for its own venue raises rather
than borrowing another venue's price, for the same reason an unmarked position
raises at all.

Cash settles in the portfolio's `base_currency` at the fill's venue, and only
base-currency rows count toward `cash`. A balance in another asset is not
converted here: that needs an FX rate, and inventing one would put a fabricated
number directly into NAV.

**The read paths are one snapshot.** Positions, cash rows and the ledger are
read in separate statements, and under READ COMMITTED each of those statements
takes its own snapshot -- so a fill committing between two of them is seen by
the later statement and not the earlier one. `snapshot_nav` would then persist
that half-state as an authoritative `nav_snapshot` row: positions from before
the fill, cash from after it, a NAV belonging to no moment. Every read path here
therefore opens a `repeatable_read` transaction, which fixes one snapshot for
all of its statements.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from omni.venue.protocol import Fill, MarketType, Position, Side


class UnknownPortfolio(Exception):
    """No portfolio row for the id given.

    Raised rather than returning an empty state, which would let a caller
    reconcile against a portfolio that does not exist and find it agrees.
    """


class UnmarkedPosition(Exception):
    """A held position has no usable mark, so NAV cannot be computed."""


class UnaccountedClose(Exception):
    """The order ledger cannot explain the book, so realised P&L is not derivable.

    Raised rather than returning the part that could be accounted for. The
    number feeds `risk.check`'s daily-loss kill switch; a realised loss that is
    short by one missing opening fill is a kill switch that does not fire.
    """


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

_CASH_ROWS = """
SELECT venue, asset, free, locked, updated_at
FROM cash_balance
WHERE portfolio_id = $1
ORDER BY venue COLLATE "C", asset COLLATE "C"
"""

# Ordered by the fill's own timestamp, not by when the event row landed: a
# venue reporting a fill late must still be replayed in the order it executed,
# or a close is replayed before the open it closes.
_LEDGER_FILLS = """
SELECT e.payload -> 'fill' ->> 'venue'            AS venue,
       e.payload -> 'fill' ->> 'symbol'           AS symbol,
       o.market_type                              AS market_type,
       e.payload -> 'fill' ->> 'side'             AS side,
       e.payload -> 'fill' ->> 'filled_quantity'  AS filled_quantity,
       e.payload -> 'fill' ->> 'average_price'    AS average_price,
       e.payload -> 'fill' ->> 'fee_paid'         AS fee_paid,
       (e.payload -> 'fill' ->> 'filled_at')::timestamptz AS filled_at
FROM order_event e
JOIN trade_order o ON o.id = e.order_id
WHERE o.portfolio_id = $1 AND e.payload ? 'fill'
ORDER BY filled_at, e.at, e.id
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
class CashPosition:
    """Cash *our book* says we hold at a venue. `free` is signed.

    Deliberately not `venue.protocol.Balance`, and the split is not cosmetic --
    the two model different quantities that happen to share a name.

    `Balance` is what a venue *reports*. An exchange does not report a negative
    available balance: what it reports is how much you may spend right now,
    which floors at zero, and a borrow appears as a separate liability the
    venue protocol does not carry. So `Balance` refuses a negative `free`, and
    relaxing that guard would let a venue adapter's sign error through as if it
    were a real reading.

    `CashPosition` is what migration 033 stores, where `free` is signed by
    design: a margin buy spends cash the account borrowed, and clamping it at
    zero would make the overdraw invisible rather than absent. Refusing the
    trade is the risk engine's job and it cannot do it from a clamped row.

    The consequence for reconciliation is explicit rather than papered over: a
    **negative total is our book saying we owe the venue that much**, and no
    value of a venue's non-negative `Balance` can agree with it. That is
    reported as a real divergence -- the venue's borrow figure is the missing
    piece and the protocol does not carry it -- not converted, clamped, or
    reconciled by inventing the difference.
    """

    venue: str
    asset: str
    free: Decimal
    locked: Decimal
    as_of: datetime

    def __post_init__(self) -> None:
        for name in ("free", "locked"):
            value = getattr(self, name)
            if not value.is_finite():
                # NUMERIC accepts 'NaN' and sorts it above every number, so the
                # schema's `locked >= 0` CHECK does not stop one, and every
                # ordering comparison below would raise InvalidOperation.
                raise ValueError(f"{name} is not a finite amount of cash: {value}")
        if self.locked < 0:
            raise ValueError(
                f"locked must not be negative, got {self.locked}; it is a "
                f"reservation against resting orders and a negative reservation "
                f"is not a state"
            )

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True)
class PortfolioState:
    """Positions, cash and cost-basis NAV as of a moment.

    `gross_exposure` and `net_exposure` are stated at average entry, because
    that is what the stored rows contain. The marked pair is written by
    `snapshot_nav`, which has the prices.

    `cash` is the base-currency total; `cash_positions` is every stored row in
    every asset, read in the same snapshot so a caller reconciling positions
    and cash against a venue compares one moment rather than two.
    """

    portfolio_id: UUID
    nav: Decimal
    cash: Decimal
    positions: tuple[Position, ...]
    as_of: datetime
    cash_positions: tuple[CashPosition, ...] = ()

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
    portfolio_id: UUID,
    cash: Decimal,
    positions: tuple[Position, ...],
    cash_positions: tuple[CashPosition, ...] = (),
) -> PortfolioState:
    book = sum((p.quantity * p.average_entry for p in positions), Decimal(0))
    return PortfolioState(
        portfolio_id=portfolio_id,
        nav=cash + book,
        cash=cash,
        positions=positions,
        as_of=datetime.now(UTC),
        cash_positions=cash_positions,
    )


async def _load_on_conn(conn, portfolio_id: UUID) -> PortfolioState:
    base_currency = await conn.fetchval(_PORTFOLIO, portfolio_id)
    if base_currency is None:
        raise UnknownPortfolio(f"no portfolio {portfolio_id}")

    rows = await conn.fetch(_POSITIONS, portfolio_id)
    cash_rows = await conn.fetch(_CASH_ROWS, portfolio_id)
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
    cash_positions = tuple(
        CashPosition(
            venue=row["venue"],
            asset=row["asset"],
            free=row["free"],
            locked=row["locked"],
            as_of=row["updated_at"],
        )
        for row in cash_rows
    )
    cash = sum(
        (c.total for c in cash_positions if c.asset == base_currency), Decimal(0)
    )
    return _state(portfolio_id, cash, positions, cash_positions)


async def load(pool, portfolio_id: UUID) -> PortfolioState:
    """Read the materialised state. Raises `UnknownPortfolio` if there is none.

    Read under `repeatable_read`: positions and cash come from separate
    statements, and under READ COMMITTED a fill committing between them is
    visible to one and not the other, producing a NAV that belongs to no
    moment.
    """
    async with (
        pool.acquire() as conn,
        conn.transaction(isolation="repeatable_read"),
    ):
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


def _marked_value(
    position: Position, marks: Mapping[tuple[str, str], Decimal]
) -> Decimal:
    where = f"{position.symbol} at {position.venue}"
    mark = marks.get((position.venue, position.symbol))
    if mark is None:
        raise UnmarkedPosition(
            f"no mark for {where}; NAV is not computable and valuing it at "
            f"entry would report zero P&L on it. Another venue's price for "
            f"{position.symbol} is not a substitute: the difference between "
            f"the two venues is exactly what a cross-venue basis position is"
        )
    if mark.is_nan() or mark.is_infinite():
        raise UnmarkedPosition(f"mark for {where} is not a number: {mark}")
    if mark <= 0:
        raise UnmarkedPosition(f"mark for {where} is not a price: {mark}")
    return position.quantity * mark


async def snapshot_nav(
    pool, portfolio_id: UUID, marks: Mapping[tuple[str, str], Decimal]
) -> Decimal:
    """Mark the book and record the NAV. Every held position needs its own mark.

    `marks` is keyed by `(venue, symbol)`, matching how positions are keyed. A
    missing, non-finite or non-positive mark raises `UnmarkedPosition` and no
    snapshot row is written. A partially-marked NAV is worse than no NAV: it
    reads as authoritative and understates exactly the exposure nobody could
    price.
    """
    async with (
        pool.acquire() as conn,
        conn.transaction(isolation="repeatable_read"),
    ):
        state = await _load_on_conn(conn, portfolio_id)

        values = [_marked_value(p, marks) for p in state.positions]
        net = sum(values, Decimal(0))
        gross = sum((abs(v) for v in values), Decimal(0))
        nav = state.cash + net

        await conn.execute(_INSERT_NAV, portfolio_id, nav, state.cash, gross, net)
        return nav


def _realised_leg(
    *,
    quantity: Decimal,
    average_entry: Decimal,
    side: Side,
    filled_quantity: Decimal,
    price: Decimal,
    fee: Decimal,
) -> Decimal:
    """What one fill realises against the position it lands on.

    Only the part of a fill that *reduces* an existing position realises
    anything. The part that opens a position, adds to one, or flips through
    into a new one is unrealised by definition and contributes nothing -- which
    is why a position still open at the end of the day contributes nothing at
    all, however far it has moved.
    """
    delta = filled_quantity if side is Side.BUY else -filled_quantity
    if quantity == 0 or (delta > 0) == (quantity > 0):
        return Decimal(0)

    closed = min(abs(delta), abs(quantity))
    gross = (
        closed * (price - average_entry)
        if quantity > 0
        else closed * (average_entry - price)
    )

    # A fill that only closes carries its whole fee against realised P&L. A
    # fill that closes and flips paid one fee for both legs, so only the
    # closing share of it is realised; the rest is a cost of the position now
    # open. The branch keeps the common case exact rather than routing it
    # through a division.
    share = fee if closed == abs(delta) else fee * closed / abs(delta)
    return gross - share


def _amount(raw: str | None, field: str) -> Decimal:
    """One money field out of a recorded fill's payload, or a refusal.

    `orders.py` writes these as strings so the audit record cannot disagree
    with the NUMERIC column beside it. Anything that does not read back as a
    finite number is corrupt ledger data, and the P&L derived from the rest of
    it would be short by whatever this fill did.
    """
    if raw is None:
        raise UnaccountedClose(f"a recorded fill carries no {field}")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise UnaccountedClose(f"a recorded fill has an unreadable {field}: {raw!r}") from exc
    if not value.is_finite():
        raise UnaccountedClose(f"a recorded fill has a non-finite {field}: {value}")
    return value


async def realised_pnl(
    pool,
    portfolio_id: UUID,
    *,
    since: datetime,
    until: datetime,
) -> Decimal:
    """Realised P&L from closed round trips with a fill in `[since, until)`.

    Derived from the order ledger, replayed under the same averaging rules the
    position rows are built with, so the two cannot drift apart. A position
    still open contributes nothing: only the quantity a fill *closes* realises,
    valued against the average entry it closes at.

    Fees paid on a closing leg count against it, pro-rated by the share of the
    fill that closed -- a fill that flips paid one fee for two legs, and the
    part that opened the new position has realised nothing yet. The fee paid to
    *open* is not attributed here: it was a cash cost at the moment it was
    paid, and charging it again on the close would count it twice.

    Stated in the portfolio's base currency, which is the unit `apply_fill`
    settles cash in. The ledger records no per-symbol quote currency, so a
    symbol quoted in something else would need an FX rate this module refuses
    to invent -- the same rule that keeps non-base-currency balances out of
    `cash`.

    Raises `UnaccountedClose` when the replayed ledger does not reproduce the
    stored position rows. That is the signature of a close whose opening fill
    is missing, and the number is not returned partially: it feeds
    `risk.check`'s daily-loss kill switch, and a loss that is short by one
    missing fill is a kill switch that does not fire.
    """
    for name, value in (("since", since), ("until", until)):
        if value.tzinfo is None:
            raise ValueError(
                f"{name} is naive ({value}); ledger timestamps are UTC and a "
                f"naive bound silently shifts the window"
            )
    if until < since:
        raise ValueError(f"until {until} is before since {since}")

    async with (
        pool.acquire() as conn,
        conn.transaction(isolation="repeatable_read"),
    ):
        if await conn.fetchval(_PORTFOLIO, portfolio_id) is None:
            raise UnknownPortfolio(f"no portfolio {portfolio_id}")

        fills = await conn.fetch(_LEDGER_FILLS, portfolio_id)
        stored_rows = await conn.fetch(_POSITIONS, portfolio_id)

    book: dict[tuple[str, str, str], tuple[Decimal, Decimal]] = {}
    realised = Decimal(0)

    for row in fills:
        venue, symbol, market_type = row["venue"], row["symbol"], row["market_type"]
        if not venue or not symbol:
            raise UnaccountedClose(
                f"a recorded fill on portfolio {portfolio_id} names no venue or "
                f"symbol ({venue!r}, {symbol!r}); it cannot be matched to a position"
            )
        filled_at = row["filled_at"]
        if filled_at is None:
            raise UnaccountedClose(
                "a recorded fill carries no filled_at, so it cannot be placed "
                "inside or outside the window"
            )

        key = (venue, symbol, market_type)
        quantity, average_entry = book.get(key, (Decimal(0), Decimal(0)))
        side = Side(row["side"])
        filled_quantity = _amount(row["filled_quantity"], "filled_quantity")
        price = _amount(row["average_price"], "average_price")
        fee = _amount(row["fee_paid"], "fee_paid")

        if since <= filled_at < until:
            realised += _realised_leg(
                quantity=quantity,
                average_entry=average_entry,
                side=side,
                filled_quantity=filled_quantity,
                price=price,
                fee=fee,
            )

        new_quantity, new_entry = _next_position(
            quantity=quantity,
            average_entry=average_entry,
            side=side,
            filled_quantity=filled_quantity,
            price=price,
        )
        if new_quantity == 0:
            book.pop(key, None)
        else:
            book[key] = (new_quantity, new_entry)

    stored = {
        (row["venue"], row["symbol"], row["market_type"]): (
            row["quantity"],
            row["average_entry"],
        )
        for row in stored_rows
    }
    if stored != book:
        # Rendered as mappings rather than sorted values: an average_entry that
        # is NaN would raise on the comparison a sort needs, inside the very
        # path that exists to report it.
        replayed = {key: book[key] for key in sorted(book)}
        held = {key: stored[key] for key in sorted(stored)}
        raise UnaccountedClose(
            f"replaying the order ledger for portfolio {portfolio_id} produces "
            f"{replayed} but the position rows hold {held}; a close whose "
            f"opening fill is not in the ledger cannot be valued, so no "
            f"realised P&L is reported for this window"
        )

    return realised


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
