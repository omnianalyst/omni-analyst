"""Which names the book can afford, against a venue that quotes real spreads.

The defect this exists to prevent is not an error. It is a basket that looks
right: `select_carry_basket` ranks on gross funding, cannot import `venue`, and
therefore cannot know that the name paying most is the name costing most. On
Hyperliquid the two orderings disagreed by 4x, and nothing in the cycle would
have said so.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from omni.trading.tradeable import Affordability, affordability, affordable_ids
from omni.venue.protocol import MarketType, Quote, Side, VenueUnavailable

NOW = datetime(2026, 8, 10, 5, 0, tzinfo=UTC)


class _Venue:
    """Quotes a stated half-spread per symbol. Everything else is real shape."""

    name = "test"

    def __init__(self, spreads_bps: dict[str, Decimal], *, unquotable=(), unlisted=()):
        self._spreads = spreads_bps
        self._unquotable = set(unquotable)
        self._unlisted = set(unlisted)

    def symbol_for(self, asset, market_type):
        if asset in self._unlisted:
            return None
        if asset == "AMBIGUOUS":
            raise ValueError("cannot resolve without a quote asset")
        suffix = "" if market_type is MarketType.SPOT else ":USDC"
        return f"{asset}/USDC{suffix}"

    async def quote(self, intent):
        asset = intent.symbol.split("/")[0]
        if asset in self._unquotable:
            raise VenueUnavailable(f"no two-sided market for {intent.symbol}")
        mid = Decimal(100)
        half = mid * self._spreads[asset] / Decimal(10000) / 2
        expected = mid + half if intent.side is Side.BUY else mid - half
        return Quote(
            intent=intent, expected_price=expected,
            fee=expected * intent.quantity * Decimal(5) / Decimal(10000),
            slippage=half * intent.quantity, gas=Decimal(0), as_of=NOW,
        )


async def _measure(spreads, **kw):
    assets = {uuid4(): a for a in spreads}
    venue = _Venue(spreads, **kw)
    measured = await affordability(
        venue, assets=assets, notional_per_pair=Decimal(70), as_of=NOW
    )
    return {m.asset: m for m in measured}


class TestTheCostIsMeasuredPerName:
    async def test_a_tight_name_costs_far_less_than_a_wide_one(self):
        """The whole point: SOL at 0.1 bps and PURR at 40 bps are not
        interchangeable, and gross funding cannot tell them apart."""
        m = await _measure({"SOL": Decimal("0.1"), "PURR": Decimal(40)})

        assert m["SOL"].round_trip_bps < m["PURR"].round_trip_bps
        # Four legs, each charged half the spread plus 5 bps taker.
        assert m["SOL"].round_trip_bps == pytest.approx(Decimal("20.2"), abs=0.5)
        assert m["PURR"].round_trip_bps == pytest.approx(Decimal(100), abs=2)

    async def test_cost_is_expressed_in_bps_of_notional_not_dollars(self):
        """So the ceiling means the same thing at every book size."""
        assets = {uuid4(): "SOL"}
        venue = _Venue({"SOL": Decimal(10)})
        small = await affordability(venue, assets=assets,
                                    notional_per_pair=Decimal(70), as_of=NOW)
        large = await affordability(venue, assets=assets,
                                    notional_per_pair=Decimal(70000), as_of=NOW)
        assert small[0].round_trip_bps == large[0].round_trip_bps


class TestOneBadNameCannotRemoveTheUniverse:
    async def test_an_unquotable_name_is_recorded_not_raised(self):
        m = await _measure(
            {"SOL": Decimal("0.1"), "DEAD": Decimal(1)}, unquotable=("DEAD",)
        )
        assert m["SOL"].affordable
        assert not m["DEAD"].affordable
        assert "unquotable" in m["DEAD"].reason

    async def test_a_name_the_venue_does_not_list_is_recorded(self):
        m = await _measure(
            {"SOL": Decimal("0.1"), "GHOST": Decimal(1)}, unlisted=("GHOST",)
        )
        assert "no spot/perp pair" in m["GHOST"].reason

    async def test_an_ambiguous_asset_is_recorded_not_raised(self):
        m = await _measure({"AMBIGUOUS": Decimal(1), "SOL": Decimal("0.1")})
        assert "unresolvable" in m["AMBIGUOUS"].reason
        assert m["SOL"].affordable


class TestTheCeiling:
    def test_it_drops_the_expensive_name_and_keeps_the_cheap_one(self):
        cheap, dear = uuid4(), uuid4()
        measured = [
            Affordability(cheap, "SOL", Decimal(18)),
            Affordability(dear, "PURR", Decimal(121)),
        ]
        assert affordable_ids(measured, max_execution_bps=Decimal(40)) == [cheap]

    def test_an_unmeasurable_name_is_never_kept(self):
        ghost = uuid4()
        measured = [Affordability(ghost, "GHOST", None, "unquotable")]
        assert affordable_ids(measured, max_execution_bps=Decimal(1000)) == []

    def test_a_zero_ceiling_is_refused(self):
        """A ceiling of zero excludes everything tradeable, which would read as
        'the venue is unusable' rather than as a misconfiguration."""
        with pytest.raises(ValueError, match="must be positive"):
            affordable_ids([], max_execution_bps=Decimal(0))

    def test_the_boundary_is_inclusive_of_the_ceiling(self):
        at = uuid4()
        measured = [Affordability(at, "EDGE", Decimal(40))]
        assert affordable_ids(measured, max_execution_bps=Decimal(40)) == [at]
