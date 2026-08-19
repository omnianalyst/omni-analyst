"""Does the book we keep still match the book the venue keeps?

`state.py` materialises positions from fills we recorded. Nothing in that path
verifies that the venue agrees. A fill written for an order the venue rejected,
a fill the venue executed while our process was down, a manual trade placed in
the venue's own UI -- each leaves the two books disagreeing, and each subsequent
size, exposure check and stop is computed against the wrong one. The
disagreement does not announce itself; it compounds.

So this module is the announcement. It is the only producer of the verdict
`risk.check(reconciled=...)` consumes, and the three values that verdict can
take are deliberately distinct:

- `True` -- checked, and the books agree inside the stated tolerance.
- `False` -- checked, and they do not. `RECONCILIATION_DIVERGENCE`.
- `None` -- never checked. `RECONCILIATION_UNKNOWN`.

**A venue that cannot answer produces the second, never the first.** A
`VenueUnavailable` is caught and turned into a divergent result carrying
`VENUE_UNAVAILABLE`, because "I could not look" and "I looked and it matched"
must not be the same value to a caller. Letting the exception escape would be
almost as bad: some caller eventually wraps this in a `try` and treats the quiet
path as fine.

Four properties are load-bearing:

- **Both directions are checked.** A position the venue holds that we have no
  row for is at least as serious as the reverse -- it is the case where our book
  is most wrong, and an implementation that walks only local rows cannot see it.
  The comparison is over the union of both key sets.
- **Tolerance is absolute, on the magnitude of the difference.** A percentage
  tolerance scales with the position, so on a large holding it would admit an
  absolute error bigger than an entire small holding. An absent side counts as
  zero, which is also what makes a venue reporting a flat row for a market we
  do not trade a non-event rather than a halt.
- **Every disagreement is reported.** No short-circuit: an operator fixing one
  divergence at a time, being told about the next only after the first is
  cleared, is an operator who does not know how far the books have drifted.
- **A quantity that is not a number is a divergence, not an exception.**
  `Decimal` NaN raises `InvalidOperation` on every ordering comparison, so an
  unscreened NaN from a venue adapter would blow up inside the tolerance check
  instead of being reported as the unusable answer it is.

Scope is one venue. Local rows stamped with another venue's name are not this
venue's business and are skipped; the caller reconciles each venue in turn.
Balances are compared on the TOTAL and on the LOCKED leg independently.
Comparing only `free + locked` cannot see an unrecorded resting order: a venue
holding 4,000 free and 6,000 locked against a local book of 10,000 free agrees
on the total while 6,000 of capital sits committed in an order we have no record
of. Totals matching is necessary and it is not sufficient, and the case where
they match is exactly the case worth catching.

**Deciding and recording are two functions, and only one of them touches the
database.** `reconcile` takes no pool and no portfolio: a comparison that read
stored history could be influenced by it, and the one thing this verdict must
depend on is the two books in front of it. `record` persists a result that has
already been decided, and `latest_by_venue` reads the most recent one per venue
back. A failed write therefore loses the record and never the verdict, and the
absence of a row means nobody checked -- which is why nothing here writes a row
it did not receive a result for.

**Cash is compared, and the two sides are different types on purpose.** The
local side is `state.CashPosition`, whose `free` is signed because a margin buy
legitimately overdraws it; the venue side is `protocol.Balance`, whose `free`
cannot be negative because that is not a thing an exchange reports. They are not
bridged by relaxing either guard. They are compared as the two readings they
are, and a local total below zero is reported as a divergence naming the borrow
the venue protocol does not carry -- which is the honest answer, because
nothing in either book says what the venue thinks that borrow is.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from omni.portfolio.state import CashPosition
from omni.venue.protocol import Balance, MarketType, Position, Venue, VenueUnavailable

ZERO = Decimal(0)


class Divergence(str, Enum):
    POSITION_QUANTITY = "position_quantity"
    POSITION_MISSING_LOCALLY = "position_missing_locally"
    POSITION_MISSING_AT_VENUE = "position_missing_at_venue"
    CASH_BALANCE = "cash_balance"
    # Totals agree, the split does not. That is an unrecorded resting order:
    # capital already committed at the venue that the local book still counts
    # as spendable. A reconciler comparing only free + locked is blind to it.
    CASH_LOCKED = "cash_locked"
    UNKNOWN_SYMBOL = "unknown_symbol"
    VENUE_UNAVAILABLE = "venue_unavailable"


@dataclass(frozen=True)
class Discrepancy:
    """One disagreement, with both sides of it and the name it is about.

    `local` and `remote` are `None` for a side that reported nothing at all,
    which is not the same statement as reporting zero: the first says the book
    has no row, the second says the book has a row that is flat.
    """

    kind: Divergence
    venue: str
    symbol: str
    local: Decimal | None
    remote: Decimal | None
    detail: str

    @property
    def magnitude(self) -> Decimal:
        """How far apart the two sides are, with an absent side read as zero."""
        local = ZERO if self.local is None else self.local
        remote = ZERO if self.remote is None else self.remote
        return abs(local - remote)


@dataclass(frozen=True)
class ReconciliationResult:
    """The verdict, and everything it was based on.

    `reconciled=True` alongside discrepancies is refused at construction. The
    trading loop reads the boolean and the operator reads the list, and a value
    where those two disagree would have one of them acting on the other's
    evidence.
    """

    reconciled: bool
    discrepancies: tuple[Discrepancy, ...]
    checked_at: datetime
    venue: str

    def __post_init__(self) -> None:
        if self.reconciled and self.discrepancies:
            raise ValueError(
                f"a reconciled result cannot carry divergences: "
                f"{[d.kind.value for d in self.discrepancies]}"
            )
        if not self.reconciled and not self.discrepancies:
            raise ValueError("a divergent result must name what diverged")

    def __bool__(self) -> bool:
        return self.reconciled


def _usable(value: Decimal | None) -> bool:
    return value is None or (isinstance(value, Decimal) and value.is_finite())


def _within(local: Decimal | None, remote: Decimal | None, tolerance: Decimal) -> bool:
    a = ZERO if local is None else local
    b = ZERO if remote is None else remote
    return abs(a - b) <= tolerance


def _unknown(venue: str, source: str, held: object) -> Discrepancy:
    return Discrepancy(
        kind=Divergence.UNKNOWN_SYMBOL,
        venue=venue,
        symbol="",
        local=None,
        remote=None,
        detail=(
            f"a holding reported {source} carries no usable identifier ({held!r}); "
            f"it cannot be matched against the other book"
        ),
    )


def _index_positions(
    positions: Iterable[Position],
    *,
    venue: str,
    source: str,
    unknown: list[Discrepancy],
) -> dict[tuple[str, MarketType], Decimal]:
    """Net quantity per (symbol, market type) -- the key `state.py` stores on.

    Netting spot against a perpetual of the same symbol would report a hedge as
    flat and reconcile it against a venue that holds both legs.
    """
    book: dict[tuple[str, MarketType], Decimal] = {}
    for position in positions:
        if not position.symbol.strip():
            unknown.append(_unknown(venue, source, position.symbol))
            continue
        key = (position.symbol, position.market_type)
        book[key] = book.get(key, ZERO) + position.quantity
    return book


def _index_balances(
    balances: Iterable[Balance | CashPosition],
    *,
    venue: str,
    source: str,
    unknown: list[Discrepancy],
) -> dict[str, tuple[Decimal, Decimal]]:
    """`(free, locked)` per asset. Accepts either side's type.

    The split is kept rather than summed. Comparing only `free + locked` cannot
    see an unrecorded resting order: a venue holding 600 free and 400 locked
    against a local book of 1,000 free and nothing locked agrees on the total
    and disagrees about 400 of capital that is already committed. That is
    precisely the state in which local order tracking has diverged, so it is the
    one a reconciler must not be blind to.

    The types are not interchangeable at their edges -- a local `free` may be
    negative where a venue's may not -- which is why they stay distinct up to
    this point and meet only as the numbers being compared.
    """
    book: dict[str, tuple[Decimal, Decimal]] = {}
    for balance in balances:
        if not balance.asset.strip():
            unknown.append(_unknown(venue, source, balance.asset))
            continue
        free, locked = book.get(balance.asset, (ZERO, ZERO))
        book[balance.asset] = (free + balance.free, locked + balance.locked)
    return book


def _position_discrepancies(
    local_book: dict[tuple[str, MarketType], Decimal],
    remote_book: dict[tuple[str, MarketType], Decimal],
    *,
    venue: str,
    tolerance: Decimal,
) -> list[Discrepancy]:
    found: list[Discrepancy] = []
    keys = sorted(set(local_book) | set(remote_book), key=lambda k: (k[0], k[1].value))

    for key in keys:
        symbol, market_type = key
        local = local_book.get(key)
        remote = remote_book.get(key)
        where = f"{symbol} ({market_type.value}) at {venue}"

        if not _usable(local) or not _usable(remote):
            found.append(
                Discrepancy(
                    kind=Divergence.POSITION_QUANTITY,
                    venue=venue,
                    symbol=symbol,
                    local=local,
                    remote=remote,
                    detail=(
                        f"{where}: local={local} remote={remote}; a non-finite "
                        f"quantity cannot be shown to be within tolerance"
                    ),
                )
            )
            continue

        if _within(local, remote, tolerance):
            continue

        if local is None:
            kind = Divergence.POSITION_MISSING_LOCALLY
            note = (
                f"{where}: the venue holds {remote} and we have no position row "
                f"for it"
            )
        elif remote is None:
            kind = Divergence.POSITION_MISSING_AT_VENUE
            note = f"{where}: we hold {local} and the venue reports no such position"
        else:
            kind = Divergence.POSITION_QUANTITY
            note = (
                f"{where}: we hold {local}, the venue holds {remote}, a difference "
                f"of {abs(local - remote)} against a tolerance of {tolerance}"
            )

        found.append(
            Discrepancy(
                kind=kind,
                venue=venue,
                symbol=symbol,
                local=local,
                remote=remote,
                detail=note,
            )
        )

    return found


def _spot_holding_discrepancies(
    local: dict[str, Decimal],
    remote: dict[str, Decimal],
    *,
    venue: str,
    tolerance: Decimal,
) -> list[Discrepancy]:
    """Compare spot holdings across the position/balance namespace seam.

    One comparison per asset: the wrapped spot the local book holds as a
    position row against the token balance the venue reports. A divergence
    here is the same fact as a position divergence -- a holding one side has
    and the other does not -- and is named with both spellings so an operator
    reads one item, not two half-items.
    """
    found: list[Discrepancy] = []
    for asset in sorted(set(local) | set(remote)):
        local_qty = local.get(asset)
        remote_qty = remote.get(asset)
        if (
            _usable(local_qty)
            and _usable(remote_qty)
            and _within(local_qty, remote_qty, tolerance)
        ):
            continue
        if not _usable(local_qty) or not _usable(remote_qty):
            detail = (
                f"{asset} (spot holding) at {venue}: local={local_qty} "
                f"remote={remote_qty}; a non-finite quantity cannot be shown "
                f"to be within tolerance"
            )
        elif local_qty is None:
            detail = (
                f"{asset} (spot holding) at {venue}: the venue reports "
                f"{remote_qty} and we hold no spot position for it"
            )
        elif remote_qty is None:
            detail = (
                f"{asset} (spot holding) at {venue}: we hold {local_qty} and "
                f"the venue reports no such token balance"
            )
        else:
            detail = (
                f"{asset} (spot holding) at {venue}: we hold {local_qty}, the "
                f"venue reports {remote_qty}, a difference of "
                f"{abs(local_qty - remote_qty)} against a tolerance of "
                f"{tolerance}"
            )
        found.append(
            Discrepancy(
                kind=Divergence.POSITION_QUANTITY,
                venue=venue,
                symbol=asset,
                local=local_qty,
                remote=remote_qty,
                detail=detail,
            )
        )
    return found


def _balance_discrepancies(
    local_book: dict[str, tuple[Decimal, Decimal]],
    remote_book: dict[str, tuple[Decimal, Decimal]],
    *,
    venue: str,
    tolerance: Decimal,
) -> list[Discrepancy]:
    found: list[Discrepancy] = []

    for asset in sorted(set(local_book) | set(remote_book)):
        local_pair = local_book.get(asset)
        remote_pair = remote_book.get(asset)
        local = None if local_pair is None else local_pair[0] + local_pair[1]
        remote = None if remote_pair is None else remote_pair[0] + remote_pair[1]

        # The locked leg is checked first and independently of the total,
        # because the case it exists for is one where the TOTALS AGREE. Folding
        # it into the total comparison would let exactly that case pass.
        if local_pair is not None and remote_pair is not None:
            local_locked, remote_locked = local_pair[1], remote_pair[1]
            if (
                _usable(local_locked)
                and _usable(remote_locked)
                and not _within(local_locked, remote_locked, tolerance)
            ):
                found.append(
                    Discrepancy(
                        kind=Divergence.CASH_LOCKED,
                        venue=venue,
                        symbol=asset,
                        local=local_locked,
                        remote=remote_locked,
                        detail=(
                            f"{asset} at {venue}: we carry {local_locked} locked, "
                            f"the venue reports {remote_locked}. Totals "
                            f"{'agree' if _within(local, remote, tolerance) else 'also differ'}"
                            f", so this is capital already committed at the venue "
                            f"that our book still counts as spendable -- an order "
                            f"we do not know about"
                        ),
                    )
                )

        if not _usable(local) or not _usable(remote):
            note = (
                f"{asset} at {venue}: local={local} remote={remote}; a non-finite "
                f"balance cannot be shown to be within tolerance"
            )
        elif _within(local, remote, tolerance):
            continue
        elif local is not None and local < ZERO:
            # A venue's Balance floors at zero, so no reading of it can agree
            # with a book that is overdrawn. Name the borrow rather than
            # reporting a bare difference an operator would read as a lost fill.
            note = (
                f"{asset} at {venue}: our book is overdrawn by {-local} and the "
                f"venue reports {ZERO if remote is None else remote}; a venue "
                f"balance cannot be negative, so the borrow behind this is a "
                f"figure neither book carries and the two cannot be shown to agree"
            )
        elif local is None:
            note = (
                f"{asset} at {venue}: the venue holds {remote} and we carry no "
                f"balance row for it"
            )
        elif remote is None:
            note = (
                f"{asset} at {venue}: we carry {local} and the venue reports no "
                f"such balance"
            )
        else:
            note = (
                f"{asset} at {venue}: we carry {local}, the venue reports {remote}, "
                f"a difference of {abs(local - remote)} against a tolerance of "
                f"{tolerance}"
            )

        found.append(
            Discrepancy(
                kind=Divergence.CASH_BALANCE,
                venue=venue,
                symbol=asset,
                local=local,
                remote=remote,
                detail=note,
            )
        )

    return found


async def reconcile(
    local_positions: Iterable[Position],
    local_balances: Iterable[CashPosition],
    venue: Venue,
    *,
    tolerance: Decimal,
    now: datetime,
) -> ReconciliationResult:
    """Compare local state against what the venue says, and name every gap.

    `local_balances` are `state.PortfolioState.cash_positions` -- the stored
    rows, signed -- not `protocol.Balance`. Passing an empty tuple is still
    legal and still means "we hold no cash at this venue", so a venue that
    reports any balance diverges; it is not a way to skip the cash check.

    `tolerance` has no default because a tolerance is a risk parameter: how far
    the books may drift before trading stops is a decision the operator makes,
    not one this module invents. `now` is passed in for the same reason the
    rest of the tier takes its clock from the caller -- a reconciliation result
    is evidence, and evidence stamped from a hidden clock cannot be replayed.
    """
    if not isinstance(tolerance, Decimal) or not tolerance.is_finite():
        raise ValueError(f"tolerance must be a finite Decimal, got {tolerance!r}")
    if tolerance < ZERO:
        raise ValueError(
            f"tolerance must not be negative, got {tolerance}; a negative "
            f"tolerance diverges on books that agree exactly"
        )
    if now.tzinfo is None:
        raise ValueError(
            f"now is naive ({now}); reconciliation is stamped in UTC and a naive "
            f"reading silently shifts it"
        )

    name = venue.name

    try:
        remote_positions = await venue.positions()
        remote_balances = await venue.balances()
    except VenueUnavailable as exc:
        return ReconciliationResult(
            reconciled=False,
            discrepancies=(
                Discrepancy(
                    kind=Divergence.VENUE_UNAVAILABLE,
                    venue=name,
                    symbol="",
                    local=None,
                    remote=None,
                    detail=(
                        f"{name} could not be read, so local state has not been "
                        f"verified against it: {exc}"
                    ),
                ),
            ),
            checked_at=now,
            venue=name,
        )

    unknown: list[Discrepancy] = []
    mine = [p for p in local_positions if p.venue == name]
    my_cash = [b for b in local_balances if b.venue == name]

    # The spot-token namespace. Some venues do not report wrapped spot as a
    # position at all -- Hyperliquid reports it as a plain token BALANCE
    # (`ETH 0.0365`) while the local book holds it as a position row under
    # the market's held spelling (`UETH/USDC`). Compared in the position and
    # cash spaces separately, one hedged holding reads as two divergences
    # (live carry halt 2026-08-19). Normalised here into its own space: a
    # local spot position whose held spelling the venue can name as an asset,
    # and the venue balance for that same asset, meet as one comparison. The
    # venue's own balance sheet decides the namespace -- a spot position is
    # promoted only when the venue actually reports that asset as a balance,
    # and a consumed balance leaves the cash comparison, so neither side can
    # silently disappear from every check.
    spot_asset_of = getattr(venue, "spot_holding_asset", None)
    remote_assets = {b.asset for b in remote_balances if b.asset.strip()}
    local_holdings: dict[str, Decimal] = {}
    as_positions: list[Position] = []
    for p in mine:
        canonical = (
            spot_asset_of(p.symbol)
            if spot_asset_of is not None and p.market_type is MarketType.SPOT
            else None
        )
        if canonical is not None and canonical in remote_assets:
            local_holdings[canonical] = (
                local_holdings.get(canonical, ZERO) + p.quantity
            )
        else:
            as_positions.append(p)

    remote_holdings: dict[str, Decimal] = {}
    as_cash: list[Balance] = []
    held_assets = set(local_holdings)
    for b in remote_balances:
        if b.asset in held_assets:
            remote_holdings[b.asset] = remote_holdings.get(b.asset, ZERO) + b.free
        else:
            as_cash.append(b)

    local_book = _index_positions(as_positions, venue=name, source="locally", unknown=unknown)
    remote_book = _index_positions(
        remote_positions, venue=name, source="by the venue", unknown=unknown
    )
    local_cash = _index_balances(my_cash, venue=name, source="locally", unknown=unknown)
    remote_cash = _index_balances(as_cash, venue=name, source="by the venue", unknown=unknown)

    discrepancies = [
        *unknown,
        *_position_discrepancies(
            local_book, remote_book, venue=name, tolerance=tolerance
        ),
        *_balance_discrepancies(
            local_cash, remote_cash, venue=name, tolerance=tolerance
        ),
        *_spot_holding_discrepancies(
            local_holdings, remote_holdings, venue=name, tolerance=tolerance
        ),
    ]

    return ReconciliationResult(
        reconciled=not discrepancies,
        discrepancies=tuple(discrepancies),
        checked_at=now,
        venue=name,
    )


_INSERT_RESULT = """
INSERT INTO reconciliation_result (portfolio_id, venue, reconciled, checked_at)
VALUES ($1, $2, $3, $4)
RETURNING id
"""

_INSERT_DISCREPANCY = """
INSERT INTO reconciliation_discrepancy
    (result_id, seq, kind, venue, symbol, local, remote, detail)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""

# One row per venue, the most recent by the moment the books were compared.
#
# The tie-break is not cosmetic. Two results stamped the same instant are
# genuinely ambiguous, so `reconciled` ascending puts a divergent reading ahead
# of a clean one: the answer that raises a flag wins, because the cost of
# showing a divergence that a second reading cleared is an operator checking,
# and the cost of the reverse is an operator not checking. `recorded_at` breaks
# what remains, and `id` makes the order total so the same rows cannot answer
# differently on two calls.
_LATEST_PER_VENUE = """
SELECT DISTINCT ON (venue) id, venue, reconciled, checked_at
FROM reconciliation_result
WHERE portfolio_id = $1
ORDER BY venue, checked_at DESC, reconciled, recorded_at DESC, id
"""

_DISCREPANCIES_FOR = """
SELECT result_id, kind, venue, symbol, local, remote, detail
FROM reconciliation_discrepancy
WHERE result_id = ANY($1::uuid[])
ORDER BY result_id, seq
"""


async def record(
    pool, result: ReconciliationResult, *, portfolio_id: UUID
) -> UUID:
    """Store a verdict that has already been reached. Returns the row id.

    This is a side effect of checking and never an input to it. It takes a
    finished `ReconciliationResult` rather than the books, so there is no path
    by which what is on disk can move what the reconciler decided.

    The result and every discrepancy commit together. A result row without its
    evidence would read back as a divergence naming nothing, and one written
    without the row it belongs to would be evidence about no verdict; the
    transaction makes both unrepresentable rather than unlikely.

    A discrepancy naming a venue other than the result's is refused outright. A
    reading about one venue is not evidence about another, and stored under this
    result it would be reported as though it were.
    """
    if result.checked_at.tzinfo is None:
        raise ValueError(
            f"checked_at is naive ({result.checked_at}); a reconciliation is "
            f"stored in UTC and a naive stamp is silently shifted by the "
            f"session's timezone, which moves when it goes stale"
        )

    foreign = sorted({d.venue for d in result.discrepancies if d.venue != result.venue})
    if foreign:
        raise ValueError(
            f"a result for {result.venue} carries discrepancies at "
            f"{', '.join(repr(v) for v in foreign)}; another venue's "
            f"disagreement is not evidence about this one"
        )

    async with pool.acquire() as conn, conn.transaction():
        result_id = await conn.fetchval(
            _INSERT_RESULT,
            portfolio_id,
            result.venue,
            result.reconciled,
            result.checked_at,
        )
        for seq, discrepancy in enumerate(result.discrepancies):
            await conn.execute(
                _INSERT_DISCREPANCY,
                result_id,
                seq,
                discrepancy.kind.value,
                discrepancy.venue,
                discrepancy.symbol,
                discrepancy.local,
                discrepancy.remote,
                discrepancy.detail,
            )
    return result_id


async def latest_by_venue(pool, portfolio_id: UUID) -> dict[str, ReconciliationResult]:
    """The most recent stored result for each venue this portfolio has checked.

    A venue with no stored result is **absent from the mapping**, and callers
    must read that absence as "never checked". It is not represented by a
    reconciled result, an empty result, or any other value a caller could
    mistake for a pass -- a `ReconciliationResult` cannot even express one,
    since a clean verdict here would be a claim that the books were compared.

    Every row is rebuilt through `ReconciliationResult`, so a stored pair that
    does not cohere -- a reconciled row carrying divergences, a divergent row
    naming none -- raises rather than being reported. The alternative is a page
    that renders whatever the table happens to hold.
    """
    async with (
        pool.acquire() as conn,
        conn.transaction(isolation="repeatable_read"),
    ):
        rows = await conn.fetch(_LATEST_PER_VENUE, portfolio_id)
        if not rows:
            return {}
        evidence_rows = await conn.fetch(
            _DISCREPANCIES_FOR, [row["id"] for row in rows]
        )

    evidence: dict[UUID, list[Discrepancy]] = {}
    for row in evidence_rows:
        evidence.setdefault(row["result_id"], []).append(
            Discrepancy(
                kind=Divergence(row["kind"]),
                venue=row["venue"],
                symbol=row["symbol"],
                local=row["local"],
                remote=row["remote"],
                detail=row["detail"],
            )
        )

    return {
        row["venue"]: ReconciliationResult(
            reconciled=row["reconciled"],
            discrepancies=tuple(evidence.get(row["id"], ())),
            checked_at=row["checked_at"],
            venue=row["venue"],
        )
        for row in rows
    }
