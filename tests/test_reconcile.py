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

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from omni.portfolio.reconcile import (
    Discrepancy,
    Divergence,
    ReconciliationResult,
    latest_by_venue,
    reconcile,
    record,
)
from omni.portfolio.state import CashPosition
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

    def symbol_for(self, asset: str, market_type):
        """Quoted in USD, and nothing unlisted.

        Part of the Venue protocol since M11: a strategy holds assets and a
        venue trades pairs, and the loop passing a bare ticker through is what        had fills settling in an asset called `MKR` (Finding 21). Reconciliation
        never calls it -- it compares what is already held -- but a fake that
        does not satisfy the protocol is a fake that stops proving the real
        adapter could stand in for it.
        """
        return asset if "/" in asset else f"{asset}/USD"

    def held_symbol_aliases(self, asset: str, market_type) -> tuple[str, ...]:
        """One spelling per market here; nothing to alias."""
        return ()

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
    """What the venue reports. Refuses a negative free by construction."""
    return Balance(
        venue=venue,
        asset=asset,
        free=Decimal(free),
        locked=Decimal(locked),
        as_of=NOW,
    )


def _local_cash(
    asset: str, free: str, locked: str = "0", *, venue: str = "paper"
) -> CashPosition:
    """What our own `cash_balance` row says. `free` may be negative."""
    return CashPosition(
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
    venue = FakeVenue(positions=held, balances=(_balance("USD", "10000"),))

    result = await reconcile(
        held, (_local_cash("USD", "10000"),), venue, tolerance=EXACT, now=NOW
    )

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
    local = (_local_cash("USD", "10000"),)
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


async def test_a_locked_split_diverges_even_when_the_totals_agree():
    """This asserted `reconciled is True`, and that was the defect.

    Local carries 10,000 free and nothing locked. The venue reports 4,000 free
    and 6,000 LOCKED. The totals match exactly, so a reconciler comparing only
    `free + locked` calls the books identical -- while 6,000 of capital sits
    committed at the venue in an order we have no record of.

    That is the precise state in which local order tracking has diverged, which
    makes it the one case a reconciler must not be blind to. Comparing the
    totals is necessary and it is not sufficient.
    """
    local = (_local_cash("USD", "10000"),)
    remote = (_balance("USD", "4000", "6000"),)
    venue = FakeVenue(balances=remote)

    result = await reconcile((), local, venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is False
    kinds = {d.kind for d in result.discrepancies}
    assert Divergence.CASH_LOCKED in kinds
    assert Divergence.CASH_BALANCE not in kinds, (
        "the totals DO agree; reporting a total divergence as well would send "
        "the operator looking for a missing fill instead of a missing order"
    )
    locked = next(d for d in result.discrepancies if d.kind is Divergence.CASH_LOCKED)
    assert locked.local == Decimal(0)
    assert locked.remote == Decimal(6000)
    assert "an order we do not know about" in locked.detail


async def test_matching_splits_still_reconcile():
    """The pair for the test above: same totals AND same split is agreement.

    Without this, a reconciler that flagged every locked balance would pass the
    test above while being useless.
    """
    local = (_local_cash("USD", "4000", locked="6000"),)
    venue = FakeVenue(balances=(_balance("USD", "4000", "6000"),))

    result = await reconcile((), local, venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is True


async def test_an_overdrawn_local_book_diverges_and_names_the_borrow():
    """The case the loop used to dodge by passing no balances at all.

    Local cash of -200 is a legitimate stored row (a margin buy). A venue's
    `Balance` floors at zero, so no reading of it agrees, and the honest answer
    is a divergence that says why -- not a clamp, and not a skipped check.
    """
    local = (_local_cash("USD", "-200"),)
    venue = FakeVenue(balances=(_balance("USD", "0"),))

    result = await reconcile((), local, venue, tolerance=Decimal(1), now=NOW)

    assert result.reconciled is False
    assert _kinds(result) == [Divergence.CASH_BALANCE]

    only = result.discrepancies[0]
    assert only.local == Decimal(-200)
    assert only.remote == Decimal(0)
    assert only.magnitude == Decimal(200)
    assert "overdrawn by 200" in only.detail
    assert "cannot be negative" in only.detail


async def test_a_positive_local_book_reconciles_cash_for_real():
    """Passing local cash must actually compare it, not be accepted and ignored.

    The pair is the whole test: an implementation that ignores `local_balances`
    reconciles both, and one that compares them reconciles only the first.
    """
    agreeing = await reconcile(
        (),
        (_local_cash("USD", "10000"), _local_cash("EUR", "250")),
        FakeVenue(balances=(_balance("USD", "10000"), _balance("EUR", "250"))),
        tolerance=EXACT,
        now=NOW,
    )
    assert agreeing.reconciled is True

    off_by_one_asset = await reconcile(
        (),
        (_local_cash("USD", "10000"), _local_cash("EUR", "250")),
        FakeVenue(balances=(_balance("USD", "10000"), _balance("EUR", "249"))),
        tolerance=EXACT,
        now=NOW,
    )
    assert off_by_one_asset.reconciled is False
    assert [d.symbol for d in off_by_one_asset.discrepancies] == ["EUR"]


async def test_holding_no_local_cash_is_a_claim_not_a_skip():
    """An empty local side means "we hold nothing here", so a venue that holds
    something diverges. Reading it as "do not check cash" is how the loop
    passed reconciliation while never comparing a balance."""
    venue = FakeVenue(balances=(_balance("USD", "10000"),))

    result = await reconcile((), (), venue, tolerance=EXACT, now=NOW)

    assert result.reconciled is False
    assert _kinds(result) == [Divergence.CASH_BALANCE]
    assert result.discrepancies[0].local is None
    assert result.discrepancies[0].remote == Decimal(10000)


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
        local, (_local_cash("USD", "10000"),), venue, tolerance=EXACT, now=NOW
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
        _local_cash("USD", "10000"),
        _local_cash("USD", "777", venue="binance"),
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


class TestPersistence:
    """Storing a verdict, and reading back the one that was stored.

    The weight here sits on the same asymmetry the rest of the file does, moved
    one layer out. A store that returns something for every venue asked about
    passes every round-trip test anybody writes and is worthless, because the
    case it exists for is the venue nobody has checked: absence has to survive
    the round trip as absence, or `never_run` stops being reachable and the page
    above reports a pass nobody took.

    The other three are the shapes that quietly lose information:

    - an absent side (`local`/`remote` of `None`) coming back as zero, which
      converts "the venue has never heard of this position" into "the venue
      reports it flat";
    - an older result outranking a newer one, which is a page reporting a
      divergence that was fixed or a pass that has since broken;
    - a result at one venue answering a question about another.
    """

    async def _book(self, db) -> UUID:
        return await db.pool.fetchval(
            """
            INSERT INTO portfolio (name, base_currency)
            VALUES ($1, 'USD') RETURNING id
            """,
            f"reconcile-{uuid4().hex[:8]}",
        )

    async def _diverged(
        self, *, venue: str = "paper", at: datetime = NOW
    ) -> ReconciliationResult:
        """A real reconciler run, so the stored shape is one production produces."""
        result = await reconcile(
            (_position("BTC/USD", "1.5", venue=venue),),
            (_local_cash("USD", "10000", venue=venue),),
            FakeVenue(name=venue, balances=(_balance("USD", "9000", venue=venue),)),
            tolerance=EXACT,
            now=at,
        )
        assert result.reconciled is False
        return result

    async def _reconciled(
        self, *, venue: str = "paper", at: datetime = NOW
    ) -> ReconciliationResult:
        result = await reconcile((), (), FakeVenue(name=venue), tolerance=EXACT, now=at)
        assert result.reconciled is True
        return result

    async def test_both_sides_of_every_disagreement_survive_the_round_trip(self, db):
        portfolio_id = await self._book(db)
        stored = await self._diverged()

        await record(db.pool, stored, portfolio_id=portfolio_id)
        loaded = (await latest_by_venue(db.pool, portfolio_id))["paper"]

        assert loaded.reconciled is False
        assert loaded.checked_at == NOW
        assert loaded.venue == "paper"
        assert _kinds(loaded) == _kinds(stored), "the order the reconciler produced"
        assert [d.detail for d in loaded.discrepancies] == [
            d.detail for d in stored.discrepancies
        ]

        missing = next(
            d
            for d in loaded.discrepancies
            if d.kind is Divergence.POSITION_MISSING_AT_VENUE
        )
        assert missing.local == Decimal("1.5")
        # Absent, and absent is not zero: the venue reported no such position
        # rather than a position of nothing.
        assert missing.remote is None

        cash = next(
            d for d in loaded.discrepancies if d.kind is Divergence.CASH_BALANCE
        )
        assert (cash.local, cash.remote) == (Decimal(10000), Decimal(9000))

    async def test_an_absent_side_is_stored_as_absent_and_not_as_zero(self, db):
        """Asserted against the column, not only against what the reader returns.

        A reader that mapped a stored 0 back to None would satisfy the round
        trip above while the row itself claimed the venue answered.
        """
        portfolio_id = await self._book(db)
        await record(db.pool, await self._diverged(), portfolio_id=portfolio_id)

        row = await db.pool.fetchrow(
            """
            SELECT kind, local, remote
            FROM reconciliation_discrepancy
            WHERE kind = 'position_missing_at_venue'
            """
        )
        assert row is not None, "the wire value of the kind, not just the enum member"
        assert row["local"] == Decimal("1.5")
        assert row["remote"] is None

    async def test_an_unavailable_venue_is_stored_as_the_divergence_it_is(self, db):
        """The case the whole module exists for, and the one with no numbers.

        Both sides are absent and the symbol is empty, so a schema that required
        either would refuse exactly the result that must never be lost.
        """
        portfolio_id = await self._book(db)
        stored = await reconcile(
            (),
            (),
            FakeVenue(unavailable="socket closed"),
            tolerance=EXACT,
            now=NOW,
        )

        await record(db.pool, stored, portfolio_id=portfolio_id)
        loaded = (await latest_by_venue(db.pool, portfolio_id))["paper"]

        assert loaded.reconciled is False
        assert _kinds(loaded) == [Divergence.VENUE_UNAVAILABLE]
        assert (loaded.discrepancies[0].local, loaded.discrepancies[0].remote) == (
            None,
            None,
        )
        assert "socket closed" in loaded.discrepancies[0].detail

    async def test_a_venue_with_no_stored_result_is_absent_from_the_read(self, db):
        """The one that keeps `never_run` reachable.

        `binance` was never checked, so nothing is known about it. A mapping
        that answered for it -- with a reconciled result, an empty result, any
        result -- would be reporting a verdict that was never reached.
        """
        portfolio_id = await self._book(db)
        await record(
            db.pool, await self._reconciled(venue="paper"), portfolio_id=portfolio_id
        )

        latest = await latest_by_venue(db.pool, portfolio_id)

        assert set(latest) == {"paper"}
        assert "binance" not in latest
        assert latest.get("binance") is None

    async def test_a_pass_at_one_venue_is_not_evidence_about_another(self, db):
        portfolio_id = await self._book(db)
        await record(
            db.pool, await self._reconciled(venue="paper"), portfolio_id=portfolio_id
        )
        await record(
            db.pool, await self._diverged(venue="binance"), portfolio_id=portfolio_id
        )

        latest = await latest_by_venue(db.pool, portfolio_id)

        assert latest["paper"].reconciled is True
        assert latest["binance"].reconciled is False
        assert latest["binance"].venue == "binance"

    async def test_the_read_returns_the_most_recent_result_per_venue(self, db):
        """Written newest-first, so an implementation returning the last row
        inserted, or the first, or an arbitrary one, disagrees with this."""
        portfolio_id = await self._book(db)
        newest = await self._reconciled(at=NOW)
        oldest = await self._diverged(at=NOW - timedelta(hours=3))
        middle = await self._diverged(at=NOW - timedelta(hours=1))

        await record(db.pool, newest, portfolio_id=portfolio_id)
        await record(db.pool, oldest, portfolio_id=portfolio_id)
        await record(db.pool, middle, portfolio_id=portfolio_id)

        loaded = (await latest_by_venue(db.pool, portfolio_id))["paper"]

        assert loaded.checked_at == NOW
        assert loaded.reconciled is True
        assert loaded.discrepancies == ()

    async def test_a_later_divergence_supersedes_an_earlier_pass(self, db):
        """The direction that matters: the fix must not be sticky either way."""
        portfolio_id = await self._book(db)
        await record(
            db.pool,
            await self._reconciled(at=NOW - timedelta(hours=2)),
            portfolio_id=portfolio_id,
        )
        await record(db.pool, await self._diverged(at=NOW), portfolio_id=portfolio_id)

        loaded = (await latest_by_venue(db.pool, portfolio_id))["paper"]

        assert loaded.reconciled is False
        assert loaded.checked_at == NOW
        assert _kinds(loaded) != []

    async def test_two_results_at_the_same_instant_report_the_divergent_one(self, db):
        """Genuinely ambiguous, so the reading that raises a flag wins.

        Showing a divergence a second reading cleared costs an operator a check.
        The reverse costs them the check they needed to make.
        """
        portfolio_id = await self._book(db)
        await record(db.pool, await self._reconciled(at=NOW), portfolio_id=portfolio_id)
        await record(db.pool, await self._diverged(at=NOW), portfolio_id=portfolio_id)

        loaded = (await latest_by_venue(db.pool, portfolio_id))["paper"]

        assert loaded.reconciled is False

    async def test_another_portfolios_result_is_not_this_ones(self, db):
        mine = await self._book(db)
        theirs = await self._book(db)
        await record(db.pool, await self._reconciled(), portfolio_id=theirs)

        assert await latest_by_venue(db.pool, mine) == {}
        assert set(await latest_by_venue(db.pool, theirs)) == {"paper"}

    async def test_a_stored_divergence_does_not_change_the_next_verdict(self, db):
        """Persisting is a side effect of checking, never an input to it.

        The books agree at this instant. A reconciler that consulted history --
        to short-circuit, to carry a divergence forward, to skip a venue it
        already flagged -- would answer about the stored past instead of the
        present books.
        """
        portfolio_id = await self._book(db)
        await record(
            db.pool,
            await self._diverged(at=NOW - timedelta(hours=1)),
            portfolio_id=portfolio_id,
        )

        held = (_position("BTC/USD", "1.5"),)
        again = await reconcile(
            held,
            (_local_cash("USD", "10000"),),
            FakeVenue(positions=held, balances=(_balance("USD", "10000"),)),
            tolerance=EXACT,
            now=NOW,
        )

        assert again.reconciled is True
        assert again.discrepancies == ()

    async def test_a_naive_checked_at_is_refused(self, db):
        portfolio_id = await self._book(db)
        stored = await self._reconciled()
        naive = ReconciliationResult(
            reconciled=True,
            discrepancies=(),
            checked_at=stored.checked_at.replace(tzinfo=None),
            venue="paper",
        )

        with pytest.raises(ValueError, match="naive"):
            await record(db.pool, naive, portfolio_id=portfolio_id)

        assert await latest_by_venue(db.pool, portfolio_id) == {}

    async def test_a_discrepancy_at_another_venue_is_refused(self, db):
        """A result for `paper` cannot carry `binance`'s disagreement.

        Stored under this result it would be reported as this venue's, which is
        the same substitution as reading one venue's pass as another's.
        """
        portfolio_id = await self._book(db)
        mixed = ReconciliationResult(
            reconciled=False,
            discrepancies=(
                Discrepancy(
                    kind=Divergence.CASH_BALANCE,
                    venue="binance",
                    symbol="USD",
                    local=Decimal(1),
                    remote=Decimal(2),
                    detail="a disagreement at another venue",
                ),
            ),
            checked_at=NOW,
            venue="paper",
        )

        with pytest.raises(ValueError, match="binance"):
            await record(db.pool, mixed, portfolio_id=portfolio_id)

        assert await latest_by_venue(db.pool, portfolio_id) == {}

    async def test_a_divergence_whose_evidence_is_gone_is_refused_on_load(self, db):
        """A divergent row with no discrepancies names nothing that diverged.

        Read back as `reconciled=False, discrepancies=()` it would render as a
        venue that failed for no stated reason; read back as reconciled it would
        render as a pass. Neither is a thing that happened, so the load refuses.
        """
        portfolio_id = await self._book(db)
        result_id = await record(
            db.pool, await self._diverged(), portfolio_id=portfolio_id
        )
        await db.pool.execute(
            "DELETE FROM reconciliation_discrepancy WHERE result_id = $1", result_id
        )

        with pytest.raises(ValueError, match="must name"):
            await latest_by_venue(db.pool, portfolio_id)

    async def test_the_result_and_its_evidence_commit_together(self, db):
        """A discrepancy the schema refuses takes the whole result down with it.

        A half-written result -- the verdict stored, the evidence lost -- is the
        row the load above has to raise on, and the transaction is what keeps it
        from ever existing.
        """
        portfolio_id = await self._book(db)
        stored = await self._diverged()
        unstorable = ReconciliationResult(
            reconciled=False,
            discrepancies=(
                *stored.discrepancies,
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

        with pytest.raises(Exception, match="says_what_happened"):
            await record(db.pool, unstorable, portfolio_id=portfolio_id)

        assert await latest_by_venue(db.pool, portfolio_id) == {}
        assert (
            await db.pool.fetchval(
                "SELECT count(*) FROM reconciliation_result WHERE portfolio_id = $1",
                portfolio_id,
            )
            == 0
        )


class TestOneBookAcrossSeveralVenues:
    """Reconciliation is scoped to the venue it is checking.

    A portfolio may hold positions at more than one venue -- the rows are keyed
    `(venue, symbol, market_type)` precisely so it can -- and both trading loops
    hand `reconcile` the WHOLE book. `reconcile` filters to `venue.name` before
    comparing anything, so a holding at another venue is not a divergence here.

    Without that filter, every position held elsewhere reads as
    `position_missing_at_venue` and the cycle halts on a book that is fine.
    Removing it fails two of the three tests below.

    These tests were written after mistaking the filter for absent: both loops
    pass unfiltered books, which looks like the omission, and the filter is 27
    lines further down than where the call sites suggest looking. It was there
    all along. What was NOT there was any test holding two venues at once, so
    the guarantee was real and undefended -- which is the state a refactor
    quietly deletes.
    """

    async def test_another_venues_positions_are_not_missing_from_this_one(self):
        local = [
            _position("BTC/USD", "1", venue="paper"),
            # Held at a different venue entirely. Not this venue's business.
            _position("ETH/USD", "50", venue="other"),
        ]
        venue = FakeVenue(
            positions=[_position("BTC/USD", "1", venue="paper")],
            balances=(_balance("USD", "10000"),),
        )

        result = await reconcile(
            local,
            [_local_cash("USD", "10000", venue="paper")],
            venue,
            tolerance=Decimal("0.01"),
            now=NOW,
        )

        assert result.reconciled is True
        assert result.discrepancies == ()

    async def test_another_venues_cash_is_not_a_divergence_here(self):
        venue = FakeVenue(
            positions=[], balances=(_balance("USD", "10000"),)
        )

        result = await reconcile(
            [],
            [
                _local_cash("USD", "10000", venue="paper"),
                # A second venue's cash. Counting it would report 60,000 local
                # against 10,000 remote and halt.
                _local_cash("USD", "50000", venue="other"),
            ],
            venue,
            tolerance=Decimal("0.01"),
            now=NOW,
        )

        assert result.reconciled is True

    async def test_this_venues_own_divergence_still_surfaces(self):
        """The pair test. A filter that dropped everything would satisfy both
        assertions above while reporting every book as reconciled, which is the
        worst possible outcome for a pre-trade check."""
        venue = FakeVenue(
            positions=[_position("BTC/USD", "1", venue="paper")],
            balances=(_balance("USD", "10000"),),
        )

        result = await reconcile(
            [
                _position("BTC/USD", "2", venue="paper"),
                _position("ETH/USD", "50", venue="other"),
            ],
            [_local_cash("USD", "10000", venue="paper")],
            venue,
            tolerance=Decimal("0.01"),
            now=NOW,
        )

        assert result.reconciled is False
        assert Divergence.POSITION_QUANTITY in _kinds(result)
        # And it is OUR venue's symbol that is named, not the other one's.
        assert [d.symbol for d in result.discrepancies] == ["BTC/USD"]
