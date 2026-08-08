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
Balances are compared on `free + locked`, since funds locked in a resting order
are still funds the venue says we have.

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

from omni.portfolio.state import CashPosition
from omni.venue.protocol import Balance, MarketType, Position, Venue, VenueUnavailable

ZERO = Decimal(0)


class Divergence(str, Enum):
    POSITION_QUANTITY = "position_quantity"
    POSITION_MISSING_LOCALLY = "position_missing_locally"
    POSITION_MISSING_AT_VENUE = "position_missing_at_venue"
    CASH_BALANCE = "cash_balance"
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
) -> dict[str, Decimal]:
    """Total per asset. Accepts either side's type -- both expose `total`.

    The types are not interchangeable at their edges (one may be negative, the
    other may not), which is exactly why they stay distinct up to this point
    and meet only as the numbers being compared.
    """
    book: dict[str, Decimal] = {}
    for balance in balances:
        if not balance.asset.strip():
            unknown.append(_unknown(venue, source, balance.asset))
            continue
        book[balance.asset] = book.get(balance.asset, ZERO) + balance.total
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


def _balance_discrepancies(
    local_book: dict[str, Decimal],
    remote_book: dict[str, Decimal],
    *,
    venue: str,
    tolerance: Decimal,
) -> list[Discrepancy]:
    found: list[Discrepancy] = []

    for asset in sorted(set(local_book) | set(remote_book)):
        local = local_book.get(asset)
        remote = remote_book.get(asset)

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

    local_book = _index_positions(mine, venue=name, source="locally", unknown=unknown)
    remote_book = _index_positions(
        remote_positions, venue=name, source="by the venue", unknown=unknown
    )
    local_cash = _index_balances(my_cash, venue=name, source="locally", unknown=unknown)
    remote_cash = _index_balances(
        remote_balances, venue=name, source="by the venue", unknown=unknown
    )

    discrepancies = [
        *unknown,
        *_position_discrepancies(
            local_book, remote_book, venue=name, tolerance=tolerance
        ),
        *_balance_discrepancies(
            local_cash, remote_cash, venue=name, tolerance=tolerance
        ),
    ]

    return ReconciliationResult(
        reconciled=not discrepancies,
        discrepancies=tuple(discrepancies),
        checked_at=now,
        venue=name,
    )
