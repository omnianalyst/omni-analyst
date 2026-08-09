"""Paper fills, and the four ways they flatter themselves if unguarded.

This venue's output decides whether real capital moves, so the assertions that
matter are the ones proving it fills *less* readily than reality. A paper venue
that is merely "reasonable" produces a track record that cannot be reproduced
live, and the discrepancy only surfaces after the money is committed.

The four guards, each with a test that fails if it is removed:

1. a fill never lands outside the bar's own [low, high];
2. a limit order the market never reached does not fill;
3. an order larger than the bar's volume fills partially, not fully;
4. a sub-minimum notional is rejected, not silently resized.

The date guard is here for the same reason: an intent with no `as_of` refuses
rather than filling against "now", because "now" in a backtest is a bar that
has not printed yet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from omni.venue.paper_venue import Bar, PaperVenue, RecordedBars
from omni.venue.protocol import (
    Capabilities,
    Fill,
    MarketType,
    OrderKind,
    Side,
    TradeIntent,
    Venue,
    VenueUnavailable,
)

AT = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _caps(**overrides) -> Capabilities:
    base = {
        "spot": True,
        "margin": False,
        "perpetuals": False,
        "limit_orders": True,
        "shorting": False,
        "funding_data": False,
        "maker_fee_bps": Decimal(2),
        "taker_fee_bps": Decimal(10),
        "min_notional": Decimal(10),
    }
    return Capabilities(**{**base, **overrides})


def _bar(
    *,
    symbol: str = "BTC/USD",
    open_: str = "100",
    high: str = "105",
    low: str = "95",
    close: str = "100",
    volume: str = "1000",
    at: datetime = AT,
) -> Bar:
    return Bar(
        symbol=symbol,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        at=at,
    )


def _market(*bars: Bar) -> RecordedBars:
    recorded = RecordedBars()
    for bar in bars or (_bar(),):
        recorded.add(bar)
    return recorded


def _intent(**overrides) -> TradeIntent:
    base = {
        "venue": "paper",
        "symbol": "BTC/USD",
        "side": Side.BUY,
        "market_type": MarketType.SPOT,
        "quantity": Decimal(1),
        "reference_price": Decimal(100),
        "order_kind": OrderKind.MARKET,
        "provenance": {"as_of": AT},
    }
    return TradeIntent(**{**base, **overrides})


def _venue(market=None, caps=None, **kwargs) -> PaperVenue:
    return PaperVenue(market or _market(), caps or _caps(), **kwargs)


class TestBarValidation:
    def test_a_close_outside_the_range_is_refused(self):
        with pytest.raises(ValueError, match="close .* outside"):
            _bar(low="95", high="105", close="110")

    def test_low_above_high_is_refused(self):
        with pytest.raises(ValueError, match="exceeds high"):
            _bar(low="110", high="105", open_="105", close="105")

    def test_negative_volume_is_refused(self):
        with pytest.raises(ValueError, match="negative volume"):
            _bar(volume="-1")


class TestConformsToProtocol:
    def test_paper_venue_is_a_venue(self):
        assert isinstance(_venue(), Venue)


class TestNeverFillsOutsideTheBar:
    async def test_a_huge_impact_cannot_push_the_fill_above_the_high(self):
        # Impact is set absurdly high so an unclamped model would price the
        # fill well above anything that traded.
        venue = _venue(impact_bps=Decimal(100_000), spread_bps=Decimal(10_000))
        fill = await venue.execute(_intent(quantity=Decimal(50)))
        assert fill.average_price <= Decimal(105)
        assert fill.average_price == Decimal(105)

    async def test_a_huge_impact_cannot_push_a_sell_below_the_low(self):
        venue = _venue(
            caps=_caps(shorting=True),
            impact_bps=Decimal(100_000),
            spread_bps=Decimal(10_000),
        )
        fill = await venue.execute(_intent(side=Side.SELL, quantity=Decimal(50)))
        assert fill.average_price >= Decimal(95)
        assert fill.average_price == Decimal(95)

    async def test_a_buy_pays_above_the_close_and_a_sell_receives_below_it(self):
        venue = _venue(caps=_caps(shorting=True))
        buy = await venue.execute(_intent(quantity=Decimal(1)))
        sell_venue = _venue(caps=_caps(shorting=True))
        sell = await sell_venue.execute(_intent(side=Side.SELL, quantity=Decimal(1)))
        assert buy.average_price > Decimal(100)
        assert sell.average_price < Decimal(100)


class TestLimitOrders:
    async def test_a_buy_limit_above_the_low_fills(self):
        venue = _venue(_market(_bar(low="95", high="105", open_="100")))
        fill = await venue.execute(
            _intent(order_kind=OrderKind.LIMIT, limit_price=Decimal(99))
        )
        assert not fill.is_empty
        assert fill.average_price <= Decimal(99)

    async def test_a_buy_limit_below_the_low_does_not_fill(self):
        venue = _venue(_market(_bar(low="95", high="105")))
        fill = await venue.execute(
            _intent(order_kind=OrderKind.LIMIT, limit_price=Decimal(90))
        )
        assert fill.is_empty
        assert "not reached" in fill.raw["rejected"]

    async def test_a_sell_limit_above_the_high_does_not_fill(self):
        venue = _venue(_market(_bar(low="95", high="105")), caps=_caps(shorting=True))
        fill = await venue.execute(
            _intent(
                side=Side.SELL, order_kind=OrderKind.LIMIT, limit_price=Decimal(110)
            )
        )
        assert fill.is_empty

    async def test_a_sell_limit_below_the_high_fills(self):
        venue = _venue(_market(_bar(low="95", high="105")), caps=_caps(shorting=True))
        fill = await venue.execute(
            _intent(
                side=Side.SELL, order_kind=OrderKind.LIMIT, limit_price=Decimal(101)
            )
        )
        assert not fill.is_empty
        assert fill.average_price >= Decimal(101)

    async def test_a_limit_fill_never_beats_the_limit_price(self):
        # Buying at 104 when the limit was 99 would be a free 5 points.
        venue = _venue(_market(_bar(low="95", high="105", open_="104")))
        fill = await venue.execute(
            _intent(order_kind=OrderKind.LIMIT, limit_price=Decimal(99))
        )
        assert fill.average_price <= Decimal(99)

    async def test_a_limit_order_pays_no_spread_or_impact(self):
        venue = _venue(
            _market(_bar(low="95", high="105", open_="100")),
            spread_bps=Decimal(10_000),
            impact_bps=Decimal(10_000),
        )
        fill = await venue.execute(
            _intent(order_kind=OrderKind.LIMIT, limit_price=Decimal(99))
        )
        assert fill.average_price == Decimal(99)


class TestVolumeCap:
    async def test_an_order_larger_than_the_participation_cap_fills_partially(self):
        venue = _venue(_market(_bar(volume="1000")), participation_cap=Decimal("0.10"))
        fill = await venue.execute(_intent(quantity=Decimal(500)))
        assert fill.filled_quantity == Decimal(100)
        assert fill.raw["partial"] is True

    async def test_an_order_within_the_cap_fills_completely(self):
        venue = _venue(_market(_bar(volume="1000")), participation_cap=Decimal("0.10"))
        fill = await venue.execute(_intent(quantity=Decimal(50)))
        assert fill.filled_quantity == Decimal(50)
        assert fill.raw["partial"] is False

    async def test_ten_times_the_volume_does_not_fill_at_the_touch(self):
        # The plan's acceptance criterion. Both halves matter: the size is
        # capped AND the price is worse than the close.
        venue = _venue(_market(_bar(volume="100", close="100")))
        fill = await venue.execute(_intent(quantity=Decimal(1000)))
        assert fill.filled_quantity < Decimal(1000)
        assert fill.average_price > Decimal(100)

    async def test_a_bar_with_no_volume_fills_nothing(self):
        venue = _venue(_market(_bar(volume="0")))
        fill = await venue.execute(_intent(quantity=Decimal(1)))
        assert fill.is_empty
        assert "no volume" in fill.raw["rejected"]

    async def test_larger_orders_pay_more_impact(self):
        small = await _venue(_market(_bar(volume="10000"))).execute(
            _intent(quantity=Decimal(1))
        )
        large = await _venue(_market(_bar(volume="10000"))).execute(
            _intent(quantity=Decimal(500))
        )
        assert large.average_price > small.average_price


class TestRejections:
    async def test_below_minimum_notional_is_rejected_not_resized(self):
        venue = _venue(caps=_caps(min_notional=Decimal(1000)))
        fill = await venue.execute(
            _intent(quantity=Decimal(1), reference_price=Decimal(100))
        )
        assert fill.is_empty
        assert "below venue minimum" in fill.raw["rejected"]
        assert venue.fills == []

    async def test_an_unsupported_market_type_raises(self):
        venue = _venue(caps=_caps(spot=True, perpetuals=False))
        with pytest.raises(VenueUnavailable, match="does not support perpetual"):
            await venue.execute(_intent(market_type=MarketType.PERPETUAL))

    async def test_selling_more_than_held_raises_on_a_venue_without_shorting(self):
        venue = _venue(caps=_caps(shorting=False))
        with pytest.raises(VenueUnavailable, match="cannot short"):
            await venue.execute(_intent(side=Side.SELL, quantity=Decimal(1)))

    async def test_selling_what_is_held_is_permitted_without_shorting(self):
        venue = _venue(_market(_bar(volume="10000")), caps=_caps(shorting=False))
        await venue.execute(_intent(side=Side.BUY, quantity=Decimal(5)))
        fill = await venue.execute(_intent(side=Side.SELL, quantity=Decimal(5)))
        assert not fill.is_empty

    async def test_a_symbol_with_no_recorded_bar_raises(self):
        venue = _venue(_market(_bar(symbol="ETH/USD")))
        with pytest.raises(VenueUnavailable, match="no recorded bar"):
            await venue.execute(_intent(symbol="BTC/USD"))

    async def test_an_intent_with_no_as_of_refuses_rather_than_using_now(self):
        venue = _venue()
        with pytest.raises(VenueUnavailable, match="had not printed yet"):
            await venue.execute(_intent(provenance={}))


class TestLookahead:
    async def test_a_bar_after_the_intents_stamp_is_not_visible(self):
        early = _bar(close="100", at=AT)
        late = _bar(
            open_="200", high="205", low="195", close="200", at=AT + timedelta(days=1)
        )
        venue = _venue(_market(early, late))
        fill = await venue.execute(_intent(provenance={"as_of": AT}))
        # Filling near 200 would mean the backtest saw tomorrow's bar.
        assert fill.average_price < Decimal(110)


class TestPositionTracking:
    async def test_buying_twice_averages_the_entry(self):
        market = _market(
            _bar(close="100", low="99", high="101", open_="100", volume="100000")
        )
        venue = _venue(market, impact_bps=Decimal(0), spread_bps=Decimal(0))
        await venue.execute(_intent(quantity=Decimal(1)))
        await venue.execute(_intent(quantity=Decimal(3)))
        positions = await venue.positions()
        assert positions[0].quantity == Decimal(4)
        assert positions[0].average_entry == Decimal(100)

    async def test_selling_the_whole_position_closes_it(self):
        venue = _venue(_market(_bar(volume="100000")), caps=_caps(shorting=True))
        await venue.execute(_intent(quantity=Decimal(2)))
        await venue.execute(_intent(side=Side.SELL, quantity=Decimal(2)))
        assert await venue.positions() == []

    async def test_a_partial_sell_keeps_the_original_entry(self):
        market = _market(_bar(close="100", low="99", high="101", open_="100", volume="100000"))
        venue = _venue(
            market,
            caps=_caps(shorting=True),
            impact_bps=Decimal(0),
            spread_bps=Decimal(0),
        )
        await venue.execute(_intent(quantity=Decimal(4)))
        entry = (await venue.positions())[0].average_entry
        await venue.execute(_intent(side=Side.SELL, quantity=Decimal(1)))
        remaining = (await venue.positions())[0]
        assert remaining.quantity == Decimal(3)
        assert remaining.average_entry == entry

    async def test_flipping_long_to_short_takes_the_new_entry(self):
        venue = _venue(_market(_bar(volume="100000")), caps=_caps(shorting=True))
        await venue.execute(_intent(quantity=Decimal(1)))
        await venue.execute(_intent(side=Side.SELL, quantity=Decimal(3)))
        position = (await venue.positions())[0]
        assert position.quantity == Decimal(-2)
        assert position.is_short


class TestCancel:
    async def test_cancel_reports_false_because_nothing_rests(self):
        assert await _venue().cancel("paper-0") is False


class TestQuote:
    async def test_quote_commits_nothing(self):
        venue = _venue()
        await venue.quote(_intent())
        assert venue.fills == []
        assert await venue.positions() == []

    async def test_quote_prices_the_fillable_size_not_the_requested_size(self):
        venue = _venue(_market(_bar(volume="100")), participation_cap=Decimal("0.10"))
        quote = await venue.quote(_intent(quantity=Decimal(1000)))
        # Fee is charged on 10 units (the cap), not 1000.
        assert quote.fee < Decimal(1000)

    async def test_quote_raises_for_a_limit_that_would_not_fill(self):
        venue = _venue(_market(_bar(low="95", high="105")))
        with pytest.raises(VenueUnavailable, match="did not trade through"):
            await venue.quote(
                _intent(order_kind=OrderKind.LIMIT, limit_price=Decimal(90))
            )


class TestPerpetualCashSettlement:
    """A perpetual does not settle in cash, and this venue must say so the same
    way `portfolio.state` does.

    The two are one question asked of two components -- "what did this fill do
    to cash" -- and `portfolio.reconcile` compares their answers directly. When
    they disagreed, they disagreed by the entire perpetual notional, and a
    reconcile-first trading loop halted on every cycle after the first.

    The figures below are the SAME worked example asserted in
    `tests/test_portfolio_state.py::TestPerpetualSettlement`. They are written
    as literals in both files on purpose: deriving either side from the other,
    or from shared code, would let one drift and take the test with it.
    """

    def _fill(self, side, qty, price, fee="0"):
        return Fill(
            intent_id="i-1",
            venue="paper",
            symbol="BTC/USD",
            side=side,
            filled_quantity=Decimal(qty),
            average_price=Decimal(price),
            fee_paid=Decimal(fee),
            filled_at=AT,
        )

    def test_opening_a_short_perp_costs_the_fee_and_nothing_else(self):
        # The defect: this credited +200, the notional, so the venue reported
        # cash the book had never received.
        venue = _venue(starting_balances={"USD": Decimal(100_000)})

        venue._apply(self._fill(Side.SELL, "2", "100", "1"), MarketType.PERPETUAL)

        assert venue._balances["USD"] == Decimal(99_999)

    def test_opening_a_long_perp_costs_the_fee_and_nothing_else(self):
        # The mirror, which the notional-settling version got wrong by -200.
        venue = _venue(starting_balances={"USD": Decimal(100_000)})

        venue._apply(self._fill(Side.BUY, "2", "100", "1"), MarketType.PERPETUAL)

        assert venue._balances["USD"] == Decimal(99_999)

    def test_closing_a_short_perp_realises_its_gain_into_cash(self):
        """Where the cash actually arrives. A close that realised nothing would
        make a profitable carry book show no profit at all."""
        venue = _venue(starting_balances={"USD": Decimal(100_000)})
        venue._apply(self._fill(Side.SELL, "2", "100"), MarketType.PERPETUAL)

        venue._apply(self._fill(Side.BUY, "2", "90"), MarketType.PERPETUAL)

        # Short 2 from 100 to 90: 2 * 10 = 20 realised.
        assert venue._balances["USD"] == Decimal(100_020)

    def test_closing_a_short_perp_at_a_loss_takes_cash(self):
        venue = _venue(starting_balances={"USD": Decimal(100_000)})
        venue._apply(self._fill(Side.SELL, "2", "100"), MarketType.PERPETUAL)

        venue._apply(self._fill(Side.BUY, "2", "110"), MarketType.PERPETUAL)

        assert venue._balances["USD"] == Decimal(99_980)

    def test_adding_to_a_perp_realises_nothing(self):
        # Same direction closes nothing. Realising here would book a profit on
        # a trade that is still open.
        venue = _venue(starting_balances={"USD": Decimal(100_000)})
        venue._apply(self._fill(Side.SELL, "2", "100"), MarketType.PERPETUAL)

        venue._apply(self._fill(Side.SELL, "2", "90"), MarketType.PERPETUAL)

        assert venue._balances["USD"] == Decimal(100_000)

    def test_spot_still_settles_in_cash_beside_a_perp_that_does_not(self):
        """The two market types must not be collapsed. A fix that made spot
        stop settling in cash would trade one accounting error for another, and
        this venue is where a spot leg of a carry pair is actually bought."""
        venue = _venue(starting_balances={"USD": Decimal(100_000)})

        venue._apply(self._fill(Side.BUY, "2", "100", "1"), MarketType.SPOT)
        after_spot = venue._balances["USD"]
        venue._apply(self._fill(Side.SELL, "2", "100", "1"), MarketType.PERPETUAL)

        assert after_spot == Decimal(99_799)          # notional AND fee
        assert venue._balances["USD"] == Decimal(99_798)  # fee only
