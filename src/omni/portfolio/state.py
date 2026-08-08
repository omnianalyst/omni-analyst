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

`nav` on `PortfolioState` is a **cost-basis** figure: cash plus what each
position contributes at its own average entry. It is exact and needs no market
data, which is what makes it safe to return from a write path. The marked NAV
comes from `snapshot_nav`, which requires a mark for every position and raises
when one is missing -- valuing an unmarked position at its entry reports an
unrealised P&L of exactly zero for the position most likely to have moved, and a
NAV that is wrong in a direction nobody can see.

**A perpetual is a contract for difference, not an asset**, and it is settled
here as one. Opening a perpetual moves no cash: margin is posted against the
position rather than spent, so the only cash an open costs is the fee. Its
contribution to NAV is therefore its *unrealised P&L*,
`quantity * (mark - average_entry)`, which at cost basis -- where the mark is
the entry -- is exactly zero. Spot and margin are unchanged and are still cash
settlements: a buy pays out the notional and takes delivery, a sell hands the
asset over and takes the notional in.

The two halves are one change because either alone is worse than neither. The
predecessor of this rule credited `+q*p` to cash on a short perpetual open while
the position booked `-q*p` at entry, so `nav = cash + book` came out right
through two errors that cancelled -- and every figure between them was wrong.
`cash` overstated by the whole perpetual notional, and `cash` is what a margin
check, a buying-power calculation and an operator reading the book all look at.
Correcting the cash leg on its own would drop NAV by the full notional on every
open.

Closing a perpetual realises, and that realisation *is* the cash the contract
returns -- there is no asset to sell. It is the closed quantity against its own
entry, `closed * (price - average_entry)` carried by the position's sign. A
close that moved no cash would take the unrealised P&L away with the position
row and put nothing in its place, so a carry book that made money would show
none.

**Exposure is a different question from NAV** and still counts the full
notional. Two BTC short as a perpetual is two BTC of exposure however little it
contributes to NAV, and `risk.py` sizes against exposure; netting a perpetual
out of `gross_exposure` would make a levered book read as flat to the limits
that exist to stop it.

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

**Funding is a cash flow, not a fill.** A perpetual pays or receives every eight
hours for as long as it is held, and no trade happens when it settles -- the
quantity does not move and the entry does not change, only cash. So it comes in
through `apply_funding` rather than `apply_fill`, and it is deliberately absent
from `rebuild_from_fills`: replaying the fills of a carry book reproduces its
positions exactly and its cash not at all, which is the same statement that
docstring already makes about deposits. A delta-neutral carry book earns nothing
except funding, so a portfolio with no concept of it reports a NAV that never
moves on the only strategy here with a measured edge.

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
from enum import Enum
from uuid import UUID

from omni.venue.protocol import Fill, MarketType, Position, Side


class UnknownPortfolio(Exception):
    """No portfolio row for the id given.

    Raised rather than returning an empty state, which would let a caller
    reconcile against a portfolio that does not exist and find it agrees.
    """


class UnmarkedPosition(Exception):
    """A held position has no usable mark, so NAV cannot be computed."""


class DuplicatePortfolio(Exception):
    """The owner already holds a portfolio under this name.

    Raised rather than opening a second book. One account holding two
    identically-named portfolios cannot be read: every audience-scoped path
    resolves by owner and then refuses to pick between them, so the second
    creation takes the operator's working endpoint away rather than adding
    anything.
    """


class UnaccountedClose(Exception):
    """The order ledger cannot explain the book, so realised P&L is not derivable.

    Raised rather than returning the part that could be accounted for. The
    number feeds `risk.check`'s daily-loss kill switch; a realised loss that is
    short by one missing opening fill is a kill switch that does not fire.
    """


_PORTFOLIO = "SELECT base_currency FROM portfolio WHERE id = $1"

# Serialises creations for one (owner, name) so the existence check below cannot
# be overtaken between reading and inserting. There is no unique index to lean
# on: migration 033 does not carry one, and adding it is not this module's to
# do, so the exclusion has to be held for the length of the transaction.
_LOCK_OWNER_NAME = "SELECT pg_advisory_xact_lock(hashtextextended($1::text || $2::text, 0))"

_PORTFOLIO_BY_OWNER_NAME = "SELECT id FROM portfolio WHERE user_id = $1 AND name = $2"

_INSERT_PORTFOLIO = """
INSERT INTO portfolio (user_id, name, base_currency)
VALUES ($1, $2, $3)
RETURNING id
"""

_INSERT_OPENING_CASH = """
INSERT INTO cash_balance (portfolio_id, venue, asset, free)
VALUES ($1, $2, $3, $4)
"""

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

# DO NOTHING on the settlement's own primary key, so a replayed cycle is refused
# by the database and not by a caller who remembered to look first. The returned
# row is the whole decision: nothing back means the settlement was already in the
# ledger and no cash may move for it a second time.
_INSERT_FUNDING = """
INSERT INTO funding_accrual (portfolio_id, venue, symbol, funding_time,
                             funding_rate, quantity, mark, amount)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (portfolio_id, venue, symbol, funding_time) DO NOTHING
RETURNING recorded_at
"""

_FUNDING_ROW = """
SELECT funding_rate, quantity, mark, amount
FROM funding_accrual
WHERE portfolio_id = $1 AND venue = $2 AND symbol = $3 AND funding_time = $4
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
    that is what the stored rows contain, and they count a perpetual's full
    notional -- exposure is what the risk limits bind on, and a perpetual is
    exposure whatever it contributes to NAV. `nav` is therefore not
    `cash + net_exposure` on a book holding one. The marked pair is written by
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


class FundingOutcome(str, Enum):
    """What one funding settlement did to the book.

    `NO_POSITION` is not `ACCRUED` with an amount of zero, and the two are kept
    apart everywhere -- here, in the returned amount, and in the stored row. A
    settlement that lands while nothing is held accrues nothing; a settlement
    that lands on a real perpetual leg at a rate of exactly zero accrues zero.
    Collapsing them reports a position that was not held as one that earned
    nothing, and the carry a strategy is graded on is an average over
    settlements it actually had exposure to.
    """

    ACCRUED = "accrued"
    NO_POSITION = "no_position"
    ALREADY_SETTLED = "already_settled"


@dataclass(frozen=True)
class FundingAccrual:
    """One settlement, as it stands in the ledger after `apply_funding`.

    `quantity`, `mark` and `amount` are `None` together and only when no
    perpetual leg was held. On `ALREADY_SETTLED` they are read back from the
    stored row rather than recomputed from the arguments, because the stored row
    is what actually moved the cash: a replay carrying a different rate is a
    disagreement the caller can see rather than one that quietly overwrites.
    """

    portfolio_id: UUID
    venue: str
    symbol: str
    funding_time: datetime
    funding_rate: Decimal
    outcome: FundingOutcome
    quantity: Decimal | None
    mark: Decimal | None
    amount: Decimal | None

    def __post_init__(self) -> None:
        if self.outcome is FundingOutcome.NO_POSITION and self.amount is not None:
            raise ValueError(
                f"a settlement with no position accrued {self.amount}; nothing "
                f"held accrues nothing, which is not an amount of zero"
            )
        if self.outcome is FundingOutcome.ACCRUED and self.amount is None:
            raise ValueError("an accrual with no amount is not an accrual")


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


def _closed_quantity(
    *, quantity: Decimal, side: Side, filled_quantity: Decimal
) -> Decimal:
    """How much of a fill reduces the position it lands on.

    Zero when the fill opens a position or adds to one. A fill that flips the
    sign closes only what was there; the rest opens the new side.

    Shared by the cash settlement and the realised-P&L derivation so the two
    cannot disagree about what a fill closed -- a divergence there would show up
    as cash and P&L telling different stories about the same trade.
    """
    delta = filled_quantity if side is Side.BUY else -filled_quantity
    if quantity == 0 or (delta > 0) == (quantity > 0):
        return Decimal(0)
    return min(abs(delta), abs(quantity))


def _closed_pnl(
    *,
    quantity: Decimal,
    average_entry: Decimal,
    side: Side,
    filled_quantity: Decimal,
    price: Decimal,
) -> Decimal:
    """Gross P&L on the part of a fill that reduces a position, before fees.

    A long realises what it gained above its entry, a short what it gained below
    it. The opening part of a flip realises nothing, so this is zero for any fill
    that only opens or adds.
    """
    closed = _closed_quantity(
        quantity=quantity, side=side, filled_quantity=filled_quantity
    )
    if quantity > 0:
        return closed * (price - average_entry)
    return closed * (average_entry - price)


def _cash_delta(
    fill: Fill,
    market_type: MarketType,
    *,
    quantity: Decimal,
    average_entry: Decimal,
) -> Decimal:
    """What one fill does to cash, given the position it lands on.

    Two settlements, not one:

    - **Spot and margin** settle in cash. A buy pays out the notional and takes
      delivery, a sell hands the asset over and takes the notional in.
    - **A perpetual settles nothing on open.** Margin is posted against the
      contract rather than spent, so an open costs the fee and nothing else. A
      close realises the closed quantity against its entry, and that is the only
      cash the contract ever returns.

    Fees always cost, on both, and the whole fee leaves the account at the moment
    it is paid -- including on a fill that closes and flips, where only the
    closing share of it is *attributable* to realised P&L but all of it is gone
    from cash.

    The position the fill lands on is a parameter rather than something this
    function reads, because both derivations already hold it: `apply_fill` from
    the locked row, `rebuild_from_fills` from the replay.
    """
    if market_type is MarketType.PERPETUAL:
        realised = _closed_pnl(
            quantity=quantity,
            average_entry=average_entry,
            side=fill.side,
            filled_quantity=fill.filled_quantity,
            price=fill.average_price,
        )
        return realised - fill.fee_paid
    proceeds = fill.notional if fill.side is Side.SELL else -fill.notional
    return proceeds - fill.fee_paid


def _marked_equity(position: Position, marked_value: Decimal) -> Decimal:
    """What a position contributes to NAV, given its signed value at some price.

    A cash-settled holding contributes the whole of it: the cash that bought it
    has already left the account, so the asset stands in for it.

    A perpetual contributes its **unrealised P&L** -- that same value less the
    same quantity at entry -- because no cash left the account to open it.
    Contributing its notional instead would count a long twice and cancel a short
    to nothing.

    At cost basis the price *is* the entry, so a perpetual contributes exactly
    zero and `_state` reaches that through this same rule rather than restating
    it.
    """
    if position.market_type is MarketType.PERPETUAL:
        return marked_value - position.quantity * position.average_entry
    return marked_value


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
    book = sum(
        (_marked_equity(p, p.quantity * p.average_entry) for p in positions),
        Decimal(0),
    )
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


async def create_portfolio(
    pool,
    *,
    user_id: UUID,
    name: str,
    base_currency: str,
    opening_cash: Decimal,
    cash_venue: str,
) -> PortfolioState:
    """Open a book for an owner, funded with a stated balance.

    **The owner is required and has no default.** `portfolio.user_id` is
    nullable in the schema, and every portfolio written before this function
    existed left it NULL -- which every audience-scoped read path treats as
    belonging to nobody, so the rows exist and no operator can reach them. A
    `user_id=None` parameter would put that state one forgotten argument away
    again, so `None` is refused here rather than inserted.

    **The opening balance is required too, and is a `Decimal`.** A default of
    zero would let a caller open a portfolio that looks funded and holds
    nothing; a `float` would carry a binary error into the NAV of every fill
    applied on top of it. Zero is accepted when it is stated, because an
    unfunded book waiting on a deposit is a real thing to want -- what is
    refused is arriving at zero by omission.

    Cash needs a venue because `cash_balance` is keyed by one, and it is the
    caller's to state: choosing a venue on their behalf would put a fabricated
    location on real money, and `reconcile` would then check that book against
    an account holding none of it. The balance is denominated in the
    portfolio's own `base_currency`, which is the only unit `load` counts
    toward `cash`.

    **One owner may hold several portfolios, but not two under one name.** A
    book per strategy is the normal case and `_resolve_portfolio` in the API
    already takes an explicit `portfolio_id` for it. A silent duplicate is the
    case worth refusing: the second one is indistinguishable from the first at
    every call site that names a book by its name, and it turns the owner's
    unqualified `/trading/portfolio` from an answer into an ambiguity.

    Returns the state `load` would return, read inside the creating
    transaction.
    """
    if user_id is None:
        raise ValueError(
            "a portfolio must name its owner; a NULL user_id is a row every "
            "audience-scoped read path steps over, so the book would exist and "
            "no operator could reach it"
        )
    if not isinstance(opening_cash, Decimal):
        raise TypeError(
            f"opening_cash must be a Decimal, got {type(opening_cash).__name__}; "
            f"a float opening balance carries a binary error into the NAV of "
            f"every fill applied on top of it"
        )
    if not opening_cash.is_finite():
        raise ValueError(f"opening_cash is not an amount of cash: {opening_cash}")
    if opening_cash < 0:
        raise ValueError(
            f"opening_cash is negative ({opening_cash}); an opening balance is a "
            f"deposit, and a borrow against a venue is a fill, not a starting point"
        )
    for field, value in (
        ("name", name),
        ("base_currency", base_currency),
        ("cash_venue", cash_venue),
    ):
        if not value or not value.strip():
            raise ValueError(f"{field} must be stated, got {value!r}")

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(_LOCK_OWNER_NAME, str(user_id), name)

        existing = await conn.fetchval(_PORTFOLIO_BY_OWNER_NAME, user_id, name)
        if existing is not None:
            raise DuplicatePortfolio(
                f"user {user_id} already holds a portfolio named {name!r} "
                f"({existing}); name the new one differently or use that id"
            )

        portfolio_id = await conn.fetchval(
            _INSERT_PORTFOLIO, user_id, name, base_currency
        )
        await conn.execute(
            _INSERT_OPENING_CASH,
            portfolio_id,
            cash_venue,
            base_currency,
            opening_cash,
        )
        return await _load_on_conn(conn, portfolio_id)


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
            _cash_delta(
                fill, resolved, quantity=quantity, average_entry=average_entry
            ),
            fill.filled_at,
        )

        return await _load_on_conn(conn, portfolio_id)


def _funding_amount(
    *, quantity: Decimal, mark: Decimal, funding_rate: Decimal
) -> Decimal:
    """Cash flow to the portfolio from one settlement. Negative is paid away.

    The convention, which everything else here follows from: **a positive
    funding rate means longs pay shorts.** It is what `venue/costs.py::carry_cost`
    prices and what `conviction/carry.py` sells.

    Derivation, both cases, from that one statement:

    - A **long** of `q > 0` at mark `m` holds a notional of `q * m` and, at a
      positive rate `r`, *pays* `q * m * r`. Its cash flow is `-q * m * r`.
    - A **short** of `q < 0` holds a notional of `|q| * m = -q * m` and, at the
      same positive `r`, *receives* it: `+(-q) * m * r`, which is `-q * m * r`
      again.

    So one expression covers both: `amount = -q * m * r`, with the sign carried
    entirely by the signed quantity and the signed rate. A negative rate falls
    out correctly without a second rule -- shorts pay longs, and `-q * m * r`
    flips with `r`.

    Worked, so the direction is checkable rather than asserted: short 2 at a mark
    of 50,000 with `r = +0.0001` gives `-(-2) * 50000 * 0.0001 = +10`, received.
    The same rate against a long of 2 gives `-10`, paid. That is the whole
    strategy's sign: a carry book is short the perp precisely to be on the
    receiving side, and inverting this expression would make it earn the exact
    negative of what it reports while every total still reads as a plausible P&L.
    """
    return -quantity * mark * funding_rate


async def apply_funding(
    pool,
    portfolio_id: UUID,
    *,
    venue: str,
    symbol: str,
    funding_time: datetime,
    funding_rate: Decimal,
    mark: Decimal,
) -> FundingAccrual:
    """Settle one funding period against the perpetual leg, once.

    **Perpetual only.** A spot holding of the same symbol at the same venue
    accrues nothing and is not looked at. The position this exists to support is
    a delta-neutral pair -- long spot, short perp -- and charging both legs would
    count the carry twice on the one book whose entire return *is* the carry.

    **Applicable exactly once.** The ledger row is keyed on
    `(portfolio, venue, symbol, funding_time)`, which is the settlement's own
    identity and contains nothing from the clock of the process applying it. A
    re-run, a replayed day or two overlapping schedulers all present that key and
    the second one is refused by the primary key, so no cash moves and the result
    comes back `ALREADY_SETTLED` carrying the values that did move.

    **`mark` is required and has no default.** Funding settles against the
    position's notional at the settlement's mark, which this module has no way to
    know; falling back to `average_entry` would value every settlement at a price
    the position has not traded at since it opened, and the error grows with
    exactly the holding period a carry trade depends on. `snapshot_nav` refuses
    an unmarked position for the same reason.

    **The settlement lands on the book as it stands now.** Position rows carry no
    history, so applying a settlement long after its `funding_time` values it
    against whatever is held today rather than what was held then. Settlements
    must therefore be applied in order and promptly; `funding_time` is stored so
    an out-of-order application is at least visible afterwards.

    Returns the ledger row. `amount` is `None` -- never zero -- when no
    perpetual leg was held.
    """
    for field, value in (("venue", venue), ("symbol", symbol)):
        if not value or not value.strip():
            raise ValueError(f"{field} must be stated, got {value!r}")
    if funding_time.tzinfo is None:
        raise ValueError(
            f"funding_time is naive ({funding_time}); settlements are UTC and a "
            f"naive one silently shifts which eight-hour period this is, which "
            f"is also the key that stops it being applied twice"
        )
    for field, value in (("funding_rate", funding_rate), ("mark", mark)):
        if not isinstance(value, Decimal):
            raise TypeError(
                f"{field} must be a Decimal, got {type(value).__name__}; a float "
                f"is encoded into NUMERIC as its full binary expansion, so the "
                f"accrual stored is not the accrual computed"
            )
        if not value.is_finite():
            raise ValueError(f"{field} is not a number: {value}")
    if mark <= 0:
        raise ValueError(
            f"mark {mark} is not a price; a settlement valued at a non-positive "
            f"mark reports a carry the position could not have earned"
        )

    async with pool.acquire() as conn, conn.transaction():
        base_currency = await conn.fetchval(_PORTFOLIO_LOCKED, portfolio_id)
        if base_currency is None:
            raise UnknownPortfolio(f"no portfolio {portfolio_id}")

        held = await conn.fetchrow(
            _LOCK_POSITION, portfolio_id, venue, symbol, MarketType.PERPETUAL.value
        )
        quantity = held["quantity"] if held is not None else None
        amount = (
            None
            if quantity is None
            else _funding_amount(
                quantity=quantity, mark=mark, funding_rate=funding_rate
            )
        )

        recorded = await conn.fetchval(
            _INSERT_FUNDING,
            portfolio_id,
            venue,
            symbol,
            funding_time,
            funding_rate,
            quantity,
            None if quantity is None else mark,
            amount,
        )
        if recorded is None:
            settled = await conn.fetchrow(
                _FUNDING_ROW, portfolio_id, venue, symbol, funding_time
            )
            return FundingAccrual(
                portfolio_id=portfolio_id,
                venue=venue,
                symbol=symbol,
                funding_time=funding_time,
                funding_rate=settled["funding_rate"],
                outcome=FundingOutcome.ALREADY_SETTLED,
                quantity=settled["quantity"],
                mark=settled["mark"],
                amount=settled["amount"],
            )

        if amount is not None:
            await conn.execute(
                _UPSERT_CASH,
                portfolio_id,
                venue,
                base_currency,
                amount,
                funding_time,
            )

        return FundingAccrual(
            portfolio_id=portfolio_id,
            venue=venue,
            symbol=symbol,
            funding_time=funding_time,
            funding_rate=funding_rate,
            outcome=(
                FundingOutcome.NO_POSITION if amount is None else FundingOutcome.ACCRUED
            ),
            quantity=quantity,
            mark=None if quantity is None else mark,
            amount=amount,
        )


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

    The recorded exposures are the full marked notional of every position,
    perpetuals included, while NAV takes a perpetual's unrealised P&L instead --
    so `nav` is not `cash + net_exposure` for a book holding one, and the two
    columns are answering different questions rather than disagreeing.
    """
    async with (
        pool.acquire() as conn,
        conn.transaction(isolation="repeatable_read"),
    ):
        state = await _load_on_conn(conn, portfolio_id)

        values = [_marked_value(p, marks) for p in state.positions]
        net = sum(values, Decimal(0))
        gross = sum((abs(v) for v in values), Decimal(0))
        nav = state.cash + sum(
            (
                _marked_equity(position, value)
                for position, value in zip(state.positions, values, strict=True)
            ),
            Decimal(0),
        )

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
    closed = _closed_quantity(
        quantity=quantity, side=side, filled_quantity=filled_quantity
    )
    if closed == 0:
        return Decimal(0)

    gross = _closed_pnl(
        quantity=quantity,
        average_entry=average_entry,
        side=side,
        filled_quantity=filled_quantity,
        price=price,
    )

    # A fill that only closes carries its whole fee against realised P&L. A
    # fill that closes and flips paid one fee for both legs, so only the
    # closing share of it is realised; the rest is a cost of the position now
    # open. The branch keeps the common case exact rather than routing it
    # through a division.
    share = (
        fee
        if closed == abs(filled_quantity)
        else fee * closed / abs(filled_quantity)
    )
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
        cash += _cash_delta(
            fill, resolved, quantity=quantity, average_entry=average_entry
        )

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
