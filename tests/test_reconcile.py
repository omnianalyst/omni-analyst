"""Reconciliation, tested for the two answers that are easy to confuse.

A reconciler that returns `True` whenever nothing threw passes every
happy-path test anybody writes, and it is worthless: the case it exists for is
the case where the venue is unreachable or holds something we never recorded.
So the weight here sits on the asymmetric pairs --

- unavailable venue versus matching book: both are "no divergence was found",
  and only one of them may reconcile;
- a position missing locally versus one missing at the venue: an implementation
  that walks only the local rows reports the second and is blind to the first,
  which is the direction where our book is most wrong;
- exactly at tolerance versus one tick past it: the boundary is asserted in
  both directions, because `>=` and `>` are indistinguishable to a test that
  only ever checks a difference of zero or a difference of ten.

The venue is a fake rather than a mock so that the protocol it satisfies is the
real one -- `test_fake_venue_satisfies_the_protocol` fails if the fake drifts
from `Venue`, which is what stops these tests from passing against an interface
production never presents.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from omni.portfolio.reconcile import (
    Discrepancy,
    Divergence,
    ReconciliationResult,
    reconcile,
)
from omni.venue.protocol import (
    Balance,
    Capabilities,
    MarketType,
    Position,
    Venue,
    VenueUnavailable,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
EXACT = Decimal(0)

CAPABILITIES = Capabilities(
    spot=True,
    margin=False,
    perpetuals=False,
    limit_orders=True,
    shorting=False,
    funding_data=False,
    maker_fee_bps=Decimal(1),
    taker_fee_bps=Decimal(5),
    min_notional=Decimal(10),
)


class FakeVenue:
    """A venue that answers with exactly what it was handed, or refuses to.

    `unavailable` is the whole point of the fake: a real adapter's outage is
    hard to schedule and this one raises the same `VenueUnavailable` the
    protocol documents.
    """

    def __init__(
        self,
        *,
        name: str = "paper",
        positions: tuple[Position, ...] = (),
        balances: tuple[Balance, ...] = (),
        unavailable: str | None = None,
    ) -> None:
        self.name = name
        self.capabilities = CAPABILITIES
        self._positions = positions
        self._balances = balances
        self._unavailable = unavailable
        self.calls: list[str] = []

    async def quote(self, intent):  # pragma: no cover - not exercised here
        raise NotImplementedError

    async def execute(self, intent):  # pragma: no cover - not exercised here
        raise NotImplementedError

    async def cancel(self, external_id: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def positions(self) -> list[Position]:
        self.calls.append("positions")
        if self._unavailable is not None:
            raise VenueUnavailable(self._unavailable)
        return list(self._positions)

    async def balances(self) -> list[Balance]:
        self.calls.append("balances")
        if self._unavailable is not None:
            raise VenueUnavailable(self._unavailable)
        return list(self._balances)


def _position(
    symbol: str,
    quantity: str,
    entry: str = "50000",
    *,
    venue: str = "paper",
    market_type: MarketType = MarketType.SPOT,
) -> Position:
    return Position(
        venue=venue,
        symbol=symbol,
        market_type=market_type,
        quantity=Decimal(quantity),
        average_entry=Decimal(entry),
        as_of=NOW,
    )


def _balance(
    asset: str, free: str, locked: str = "0", *, venue: str = "paper"
) -> Balance:
    return Balance(
        venue=venue,
        asset=asset,
        free=Decimal(free),
        locked=Decimal(locked),
        as_of=NOW,
    )


def _kinds(result: ReconciliationResult) -> list[Divergence]:
    return [d.kind for d in result.discrepancies]


async def test_matching_book_reconciles():
    held = (_position("BTC/USD", "1.5"),)
    cash = (_balance("USD", "10000"),)
    venue = FakeVenue(positions=held, balances=cash)

    result = await reconcile(held, cash, venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is True
    assert result.discrepancies == ()
    assert bool(result) is True
    assert result.venue == "paper"
    assert result.checked_at == NOW
    assert venue.calls == ["positions", "balances"]


async def test_quantity_difference_beyond_tolerance_diverges():
    local = (_position("BTC/USD", "1.5"),)
    remote = (_position("BTC/USD", "1.2"),)
    venue = FakeVenue(positions=remote, balances=())

    result = await reconcile(local, (), venue, tolerance=Decimal("0.01"), now=NOW)

    assert result.reconciled is False
    assert bool(result) is False
    assert _kinds(result) == [Divergence.POSITION_QUANTITY]

    only = result.discrepancies[0]
    assert only.symbol == "BTC/USD"
    assert only.local == Decimal("1.5")
    assert only.remote == Decimal("1.2")
    assert only.magnitude == Decimal("0.3")
    assert "BTC/USD" in only.detail


async def test_difference_exactly_at_tolerance_does_not_diverge():
    local = (_position("BTC/USD", "1.5"),)
    remote = (_position("BTC/USD", "1.4"),)
    venue = FakeVenue(positions=remote)

    at = await reconcile(local, (), venue, tolerance=Decimal("0.1"), now=NOW)

    assert at.reconciled is True, "a difference equal to the tolerance is inside it"

    past = await reconcile(
        local, (), FakeVenue(positions=remote), tolerance=Decimal("0.09"), now=NOW
    )

    assert past.reconciled is False
    assert _kinds(past) == [Divergence.POSITION_QUANTITY]
    assert past.discrepancies[0].magnitude == Decimal("0.1")


async def test_position_at_venue_missing_locally_is_reported():
    """The case a local-only walk cannot see -- and the one we are most wrong in."""
    remote = (_position("ETH/USD", "40"),)
    venue = FakeVenue(positions=remote)

    result = await reconcile((), (), venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is False
    assert _kinds(result) == [Divergence.POSITION_MISSING_LOCALLY]

    only = result.discrepancies[0]
    assert only.symbol == "ETH/USD"
    assert only.local is None
    assert only.remote == Decimal(40)
    assert only.magnitude == Decimal(40)


async def test_position_held_locally_but_absent_at_venue_is_reported():
    local = (_position("SOL/USD", "-12", "150"),)
    venue = FakeVenue(positions=())

    result = await reconcile(local, (), venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is False
    assert _kinds(result) == [Divergence.POSITION_MISSING_AT_VENUE]

    only = result.discrepancies[0]
    assert only.symbol == "SOL/USD"
    assert only.local == Decimal(-12)
    assert only.remote is None
    assert only.magnitude == Decimal(12)


async def test_cash_balance_divergence_is_reported():
    local = (_balance("USD", "10000"),)
    remote = (_balance("USD", "9500"),)
    venue = FakeVenue(balances=remote)

    result = await reconcile((), local, venue, tolerance=Decimal(1), now=NOW)

    assert result.reconciled is False
    assert _kinds(result) == [Divergence.CASH_BALANCE]

    only = result.discrepancies[0]
    assert only.symbol == "USD"
    assert only.local == Decimal(10000)
    assert only.remote == Decimal(9500)
    assert only.magnitude == Decimal(500)


async def test_locked_funds_count_toward_the_balance_compared():
    local = (_balance("USD", "10000"),)
    remote = (_balance("USD", "4000", "6000"),)
    venue = FakeVenue(balances=remote)

    result = await reconcile((), local, venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is True


async def test_venue_unavailable_is_a_divergence_not_an_exception():
    """"I could not check" must never be recorded as "I checked and it matched"."""
    local = (_position("BTC/USD", "1.5"),)
    venue = FakeVenue(positions=local, unavailable="socket closed by peer")

    result = await reconcile(local, (), venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is False
    assert bool(result) is False
    assert _kinds(result) == [Divergence.VENUE_UNAVAILABLE]
    assert "socket closed by peer" in result.discrepancies[0].detail
    assert result.venue == "paper"
    assert result.checked_at == NOW


async def test_balance_call_failing_alone_still_fails_closed():
    class HalfDeadVenue(FakeVenue):
        async def balances(self) -> list[Balance]:
            raise VenueUnavailable("balances endpoint timed out")

    local = (_position("BTC/USD", "1.5"),)
    venue = HalfDeadVenue(positions=local)

    result = await reconcile(local, (), venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is False
    assert _kinds(result) == [Divergence.VENUE_UNAVAILABLE]


async def test_every_divergence_is_reported_not_just_the_first():
    local = (
        _position("BTC/USD", "1.5"),
        _position("SOL/USD", "-12", "150"),
    )
    remote = (
        _position("BTC/USD", "1.2"),
        _position("ETH/USD", "40", "3000"),
    )
    venue = FakeVenue(
        positions=remote, balances=(_balance("USD", "9500"),)
    )

    result = await reconcile(
        local, (_balance("USD", "10000"),), venue, tolerance=EXACT, now=NOW
    )

    assert result.reconciled is False
    assert _kinds(result) == [
        Divergence.POSITION_QUANTITY,
        Divergence.POSITION_MISSING_LOCALLY,
        Divergence.POSITION_MISSING_AT_VENUE,
        Divergence.CASH_BALANCE,
    ]
    assert [d.symbol for d in result.discrepancies] == [
        "BTC/USD",
        "ETH/USD",
        "SOL/USD",
        "USD",
    ]


async def test_short_position_reconciles_against_a_negative_remote():
    local = (_position("SOL/USD", "-12", "150"),)
    remote = (_position("SOL/USD", "-12", "150"),)
    venue = FakeVenue(positions=remote)

    agreed = await reconcile(local, (), venue, tolerance=EXACT, now=NOW)

    assert agreed.reconciled is True, "a matching short is not a divergence"

    flipped = await reconcile(
        local,
        (),
        FakeVenue(positions=(_position("SOL/USD", "12", "150"),)),
        tolerance=EXACT,
        now=NOW,
    )

    assert flipped.reconciled is False, "a long where we booked a short is a divergence"
    assert _kinds(flipped) == [Divergence.POSITION_QUANTITY]
    assert flipped.discrepancies[0].magnitude == Decimal(24)


async def test_magnitude_treats_an_absent_side_as_zero():
    missing_local = Discrepancy(
        kind=Divergence.POSITION_MISSING_LOCALLY,
        venue="paper",
        symbol="ETH/USD",
        local=None,
        remote=Decimal("40.5"),
        detail="",
    )
    missing_remote = Discrepancy(
        kind=Divergence.POSITION_MISSING_AT_VENUE,
        venue="paper",
        symbol="ETH/USD",
        local=Decimal("-40.5"),
        remote=None,
        detail="",
    )
    neither = Discrepancy(
        kind=Divergence.VENUE_UNAVAILABLE,
        venue="paper",
        symbol="",
        local=None,
        remote=None,
        detail="",
    )

    assert missing_local.magnitude == Decimal("40.5")
    assert missing_remote.magnitude == Decimal("40.5")
    assert neither.magnitude == EXACT


async def test_spot_and_perpetual_legs_are_not_netted_against_each_other():
    local = (
        _position("BTC/USD", "1.5"),
        _position("BTC/USD", "-1.5", market_type=MarketType.PERPETUAL),
    )
    venue = FakeVenue(positions=(_position("BTC/USD", "1.5"),))

    result = await reconcile(local, (), venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is False, "the perpetual leg is unmatched at the venue"
    assert _kinds(result) == [Divergence.POSITION_MISSING_AT_VENUE]
    assert result.discrepancies[0].local == Decimal("-1.5")


async def test_rows_belonging_to_another_venue_are_out_of_scope():
    local = (
        _position("BTC/USD", "1.5"),
        _position("BTC/USD", "9", venue="binance"),
    )
    cash = (
        _balance("USD", "10000"),
        _balance("USD", "777", venue="binance"),
    )
    venue = FakeVenue(
        positions=(_position("BTC/USD", "1.5"),), balances=(_balance("USD", "10000"),)
    )

    result = await reconcile(local, cash, venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is True


async def test_a_flat_row_at_the_venue_is_not_a_divergence():
    venue = FakeVenue(positions=(_position("DOGE/USD", "0", "0"),))

    result = await reconcile((), (), venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is True


async def test_non_finite_quantity_is_reported_rather_than_raising():
    venue = FakeVenue(positions=(_position("BTC/USD", "NaN"),))

    result = await reconcile(
        (_position("BTC/USD", "1.5"),), (), venue, tolerance=EXACT, now=NOW
    )

    assert result.reconciled is False
    assert _kinds(result) == [Divergence.POSITION_QUANTITY]
    assert "non-finite" in result.discrepancies[0].detail


async def test_a_holding_with_no_usable_symbol_is_unknown():
    venue = FakeVenue(positions=(_position("   ", "3"),))

    result = await reconcile((), (), venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is False
    assert _kinds(result) == [Divergence.UNKNOWN_SYMBOL]


async def test_tolerance_must_be_supplied_and_sane():
    venue = FakeVenue()

    with pytest.raises(TypeError):
        await reconcile((), (), venue, now=NOW)  # type: ignore[call-arg]

    with pytest.raises(ValueError, match="negative"):
        await reconcile((), (), venue, tolerance=Decimal("-0.1"), now=NOW)

    with pytest.raises(ValueError, match="finite"):
        await reconcile((), (), venue, tolerance=Decimal("NaN"), now=NOW)

    assert venue.calls == [], "no venue was called before the parameters were checked"


async def test_naive_now_is_refused():
    with pytest.raises(ValueError, match="naive"):
        await reconcile(
            (), (), FakeVenue(), tolerance=EXACT, now=NOW.replace(tzinfo=None)
        )


def test_a_reconciled_result_cannot_carry_divergences():
    with pytest.raises(ValueError, match="cannot carry divergences"):
        ReconciliationResult(
            reconciled=True,
            discrepancies=(
                Discrepancy(
                    kind=Divergence.CASH_BALANCE,
                    venue="paper",
                    symbol="USD",
                    local=Decimal(1),
                    remote=Decimal(2),
                    detail="",
                ),
            ),
            checked_at=NOW,
            venue="paper",
        )

    with pytest.raises(ValueError, match="must name"):
        ReconciliationResult(
            reconciled=False, discrepancies=(), checked_at=NOW, venue="paper"
        )


def test_fake_venue_satisfies_the_protocol():
    assert isinstance(FakeVenue(), Venue)
