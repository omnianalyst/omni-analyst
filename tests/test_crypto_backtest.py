"""The crypto backtest harness, and the three ways a crypto result flatters itself.

Every arithmetic expectation in this file is derived in the test, in a comment,
from the inputs -- never read back out of the implementation. A test that
asserts whatever the code returned proves the code is deterministic and nothing
else.

Three assertions carry the weight.

**A short collecting positive funding must earn.** Funding is the only signed
component of the cost model and the funding-carry producer's whole thesis is
receiving it. A sign error here reports the strategy as its own opposite, and
it would read as unprofitable exactly when it works. So the short's
`funding_collected_bps` is asserted positive *and* its net asserted better than
the same trade with funding switched off -- either alone would pass an
implementation that got half of it right.

**Hold time is wall-clock.** A 72-hour hold across a weekend must accrue the
same funding as a 72-hour hold mid-week. A business-day calendar would give the
weekend hold 3 settlements instead of 9, so the test spans a real Saturday and
asserts the weekdays it spans rather than trusting two dates to differ.

**Below the sample floor the hit rate is None.** Not 0.0, not 0.5. The floor is
what stops a gate opening on three outcomes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import pytest

from omni.ingest.protocol import Unavailable
from omni.trading.crypto_backtest import (
    Barrier,
    ClosedTrade,
    run_backtest,
)
from omni.venue.protocol import Capabilities, MarketType, Side

OPENED = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)


def _caps(
    *,
    maker: str = "10",
    taker: str = "10",
    spot: bool = True,
    perpetuals: bool = True,
    shorting: bool = True,
    margin: bool = True,
) -> Capabilities:
    return Capabilities(
        spot=spot,
        margin=margin,
        perpetuals=perpetuals,
        limit_orders=True,
        shorting=shorting,
        funding_data=True,
        maker_fee_bps=Decimal(maker),
        taker_fee_bps=Decimal(taker),
        min_notional=Decimal(10),
    )


def _trade(
    *,
    method: str = "trend.sma",
    symbol: str = "BTC/USDT:USDT",
    side: Side = Side.BUY,
    market_type: MarketType = MarketType.SPOT,
    quantity: str = "1",
    entry: str = "100",
    exit_price: str = "100",
    opened_at: datetime = OPENED,
    hold_hours: int = 24,
    barrier: Barrier = Barrier.VERTICAL,
) -> ClosedTrade:
    return ClosedTrade(
        method=method,
        symbol=symbol,
        side=side,
        market_type=market_type,
        quantity=Decimal(quantity),
        entry_price=Decimal(entry),
        exit_price=Decimal(exit_price),
        opened_at=opened_at,
        closed_at=opened_at + timedelta(hours=hold_hours),
        barrier=barrier,
    )


class TestHandDerivedExpectancy:
    """One trade set, one venue, arithmetic written out before it is asserted."""

    #  Venue: maker 10bps, taker 10bps, spread 4bps. Spot, so no funding.
    #  Round trip per trade = taker entry 10 + taker exit 10
    #                       + half-spread entry 2 + half-spread exit 2
    #                       = 24bps.
    #
    #  Trade            gross                                   net
    #  100 -> 103 up    (103-100)/100 * 10_000 =  +300      +300-24 = +276
    #  100 ->  98 down  ( 98-100)/100 * 10_000 =  -200      -200-24 = -224
    #  200 -> 206 up    (206-200)/200 * 10_000 =  +300      +300-24 = +276
    #   50 ->  49 down  ( 49- 50)/ 50 * 10_000 =  -200      -200-24 = -224
    #
    #  gross = 300 - 200 + 300 - 200            =  +200
    #  cost  = 4 x 24                           =    96
    #  net   = 200 - 96                         =  +104
    #  expectancy = 104 / 4                     =   +26 bps per trade
    #  hits = 2 upper barriers on two longs; hit rate = 2/4 = 0.5

    TRADES: ClassVar = (
        _trade(entry="100", exit_price="103", barrier=Barrier.UPPER),
        _trade(entry="100", exit_price="98", barrier=Barrier.LOWER),
        _trade(entry="200", exit_price="206", barrier=Barrier.UPPER),
        _trade(entry="50", exit_price="49", barrier=Barrier.LOWER),
    )

    def _run(self):
        return run_backtest(
            self.TRADES,
            capabilities=_caps(),
            spread_bps=Decimal(4),
            min_trades_for_rate=4,
        )

    def test_gross_cost_and_net_are_the_hand_derived_figures(self):
        result = self._run()
        assert result.gross_bps == Decimal(200)
        assert result.cost_bps == Decimal(96)
        assert result.net_bps == Decimal(104)

    def test_expectancy_is_net_per_trade(self):
        assert self._run().expectancy_bps == Decimal(26)

    def test_hits_count_only_the_barrier_the_direction_was_betting_on(self):
        result = self._run()
        assert result.n_trades == 4
        assert result.hits == 2
        assert result.hit_rate == Decimal("0.5")

    def test_a_vertical_exit_is_not_a_hit(self):
        # Same four trades, but the two winners timed out at the same prices.
        timed_out = [
            _trade(entry="100", exit_price="103", barrier=Barrier.VERTICAL),
            _trade(entry="100", exit_price="98", barrier=Barrier.LOWER),
            _trade(entry="200", exit_price="206", barrier=Barrier.VERTICAL),
            _trade(entry="50", exit_price="49", barrier=Barrier.LOWER),
        ]
        result = run_backtest(
            timed_out,
            capabilities=_caps(),
            spread_bps=Decimal(4),
            min_trades_for_rate=4,
        )
        # The money is identical; only the hit rate changes.
        assert result.net_bps == Decimal(104)
        assert result.hits == 0
        assert result.hit_rate == Decimal(0)

    def test_the_method_travels_with_the_trades(self):
        assert self._run().method == "trend.sma"

    def test_pooling_two_methods_is_refused(self):
        mixed = [
            _trade(method="trend.sma", entry="100", exit_price="103"),
            _trade(method="carry.funding", entry="100", exit_price="103"),
        ]
        with pytest.raises(ValueError, match="one method"):
            run_backtest(mixed, capabilities=_caps(), spread_bps=Decimal(4))

    def test_both_legs_are_charged(self):
        # Raising the taker fee by 5bps must cost 10bps per trade, not 5.
        # An implementation costing only the entry passes every other test here.
        cheap = self._run()
        dearer = run_backtest(
            self.TRADES,
            capabilities=_caps(taker="15"),
            spread_bps=Decimal(4),
            min_trades_for_rate=4,
        )
        assert dearer.cost_bps - cheap.cost_bps == Decimal(10) * len(self.TRADES)


class TestFundingIsSigned:
    """The headline. A short holding through positive funding is paid to wait."""

    # Zero fees and zero spread, so the only cost in play is funding and the
    # arithmetic cannot hide behind friction.
    #
    #   entry == exit == 100  ->  gross = 0bps
    #   hold 24h, settlement 8h  ->  floor(24/8) = 3 settlements
    #   funding rate 0.0001 per settlement (1bp), positive: longs pay shorts
    #   magnitude = 0.0001 * 3 * 10_000 = 3bps
    #
    #   short: funding is a credit of 3bps  -> cost -3, net = 0 - (-3) = +3
    #   long:  funding is a cost   of 3bps  -> cost +3, net = 0 -  (+3) = -3

    RATE = Decimal("0.0001")

    def _free_caps(self) -> Capabilities:
        return _caps(maker="0", taker="0")

    def _run(self, side: Side, *, funding: Decimal | None):
        trade = _trade(
            method="carry.funding",
            side=side,
            market_type=MarketType.PERPETUAL,
            entry="100",
            exit_price="100",
            hold_hours=24,
        )
        return run_backtest(
            [trade],
            capabilities=self._free_caps(),
            spread_bps=Decimal(0),
            funding_rates=funding,
            settlement_hours=8,
        )

    def test_a_short_through_positive_funding_collects_and_nets_better(self):
        with_funding = self._run(Side.SELL, funding=self.RATE)
        ignored = self._run(Side.SELL, funding=None)

        assert with_funding.funding_collected_bps == Decimal(3)
        assert with_funding.funding_collected_bps > 0
        assert with_funding.cost_bps == Decimal(-3)
        assert with_funding.net_bps == Decimal(3)

        # Ignoring funding is not neutral for a carry short -- it deletes the edge.
        assert ignored.funding_collected_bps == Decimal(0)
        assert ignored.net_bps == Decimal(0)
        assert with_funding.net_bps > ignored.net_bps

    def test_a_long_through_positive_funding_pays_and_nets_worse(self):
        with_funding = self._run(Side.BUY, funding=self.RATE)
        ignored = self._run(Side.BUY, funding=None)

        assert with_funding.funding_collected_bps == Decimal(-3)
        assert with_funding.cost_bps == Decimal(3)
        assert with_funding.net_bps == Decimal(-3)
        assert with_funding.net_bps < ignored.net_bps

    def test_the_two_sides_are_mirror_images(self):
        short = self._run(Side.SELL, funding=self.RATE)
        long = self._run(Side.BUY, funding=self.RATE)
        assert short.funding_collected_bps == -long.funding_collected_bps

    def test_a_negative_funding_rate_reverses_who_pays(self):
        # Negative funding means shorts pay longs. The long now collects.
        short = self._run(Side.SELL, funding=-self.RATE)
        long = self._run(Side.BUY, funding=-self.RATE)
        assert short.funding_collected_bps == Decimal(-3)
        assert long.funding_collected_bps == Decimal(3)

    def test_settlements_are_floored_not_rounded(self):
        # 23h at an 8h settlement is 2 settlements, not 3 -- the third has not
        # been paid yet. 2 x 1bp = 2bps collected by the short.
        trade = _trade(
            method="carry.funding",
            side=Side.SELL,
            market_type=MarketType.PERPETUAL,
            hold_hours=23,
        )
        result = run_backtest(
            [trade],
            capabilities=self._free_caps(),
            spread_bps=Decimal(0),
            funding_rates=self.RATE,
            settlement_hours=8,
        )
        assert result.funding_collected_bps == Decimal(2)

    def test_spot_accrues_no_funding_even_when_a_rate_is_supplied(self):
        # There is no funding leg on spot; charging one would invent a cost.
        trade = _trade(
            method="carry.funding",
            side=Side.SELL,
            market_type=MarketType.SPOT,
            hold_hours=24,
        )
        result = run_backtest(
            [trade],
            capabilities=self._free_caps(),
            spread_bps=Decimal(0),
            funding_rates=self.RATE,
        )
        assert result.funding_collected_bps == Decimal(0)

    def test_a_perpetual_with_no_rate_in_the_mapping_is_refused(self):
        trade = _trade(
            method="carry.funding",
            symbol="ETH/USDT:USDT",
            market_type=MarketType.PERPETUAL,
        )
        with pytest.raises(Unavailable, match="no funding rate"):
            run_backtest(
                [trade],
                capabilities=self._free_caps(),
                spread_bps=Decimal(0),
                funding_rates={"BTC/USDT:USDT": self.RATE},
            )

    def test_a_per_symbol_mapping_prices_each_symbol_with_its_own_rate(self):
        btc = _trade(
            method="carry.funding",
            symbol="BTC/USDT:USDT",
            side=Side.SELL,
            market_type=MarketType.PERPETUAL,
            hold_hours=24,
        )
        eth = _trade(
            method="carry.funding",
            symbol="ETH/USDT:USDT",
            side=Side.SELL,
            market_type=MarketType.PERPETUAL,
            hold_hours=24,
        )
        result = run_backtest(
            [btc, eth],
            capabilities=self._free_caps(),
            spread_bps=Decimal(0),
            funding_rates={
                "BTC/USDT:USDT": Decimal("0.0001"),   # 3 x 1bp = 3bps
                "ETH/USDT:USDT": Decimal("0.0002"),   # 3 x 2bp = 6bps
            },
        )
        assert result.funding_collected_bps == Decimal(9)


class TestTwentyFourSeven:
    """No session close, no weekend. Hold time is wall clock and nothing else."""

    def test_a_weekend_hold_accrues_the_same_funding_as_a_midweek_hold(self):
        friday = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
        tuesday = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
        # Assert the calendar rather than trusting two dates to differ: without
        # this the test could pass on two mid-week spans and prove nothing.
        assert friday.strftime("%a") == "Fri"
        assert (friday + timedelta(hours=72)).strftime("%a") == "Mon"
        assert tuesday.strftime("%a") == "Tue"
        assert (tuesday + timedelta(hours=72)).strftime("%a") == "Fri"

        def held_from(start: datetime):
            trade = _trade(
                method="carry.funding",
                side=Side.SELL,
                market_type=MarketType.PERPETUAL,
                opened_at=start,
                hold_hours=72,
            )
            return run_backtest(
                [trade],
                capabilities=_caps(maker="0", taker="0"),
                spread_bps=Decimal(0),
                funding_rates=Decimal("0.0001"),
                settlement_hours=8,
            )

        over_weekend = held_from(friday)
        mid_week = held_from(tuesday)

        # 72 wall-clock hours / 8 = 9 settlements, both. A business-day calendar
        # would give the weekend hold 24 hours and 3 settlements.
        assert over_weekend.funding_collected_bps == Decimal(9)
        assert mid_week.funding_collected_bps == Decimal(9)
        assert over_weekend.funding_collected_bps == mid_week.funding_collected_bps

    def test_a_hold_that_is_entirely_weekend_still_accrues(self):
        saturday = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
        assert saturday.strftime("%a") == "Sat"
        trade = _trade(
            method="carry.funding",
            side=Side.SELL,
            market_type=MarketType.PERPETUAL,
            opened_at=saturday,
            hold_hours=48,
        )
        result = run_backtest(
            [trade],
            capabilities=_caps(maker="0", taker="0"),
            spread_bps=Decimal(0),
            funding_rates=Decimal("0.0001"),
            settlement_hours=8,
        )
        # 48h / 8 = 6 settlements. A business calendar would report zero.
        assert result.funding_collected_bps == Decimal(6)


class TestTheVenueDecidesIt:
    """The same signal, funded at one venue and worthless at another."""

    #  10 spot longs from 100: six close at 103 (+300bps, upper barrier), four
    #  at 97 (-300bps, lower barrier).
    #
    #  gross = 6 x 300 - 4 x 300 = +600bps ; hit rate = 6/10 = 0.6
    #
    #  cheap venue  (2bps a side, 4bps spread):  2 + 2 + 2 + 2  =   8bps a trade
    #      cost = 10 x 8 = 80    net = 600 - 80   = +520   expectancy = +52
    #  costly venue (75bps a side, 4bps spread): 75 + 75 + 2 + 2 = 154bps a trade
    #      cost = 10 x 154 = 1540  net = 600 - 1540 = -940  expectancy = -94

    TRADES: ClassVar = tuple(
        [_trade(entry="100", exit_price="103", barrier=Barrier.UPPER)] * 6
        + [_trade(entry="100", exit_price="97", barrier=Barrier.LOWER)] * 4
    )

    def test_the_cheap_venue_funds_it(self):
        result = run_backtest(
            self.TRADES,
            capabilities=_caps(maker="2", taker="2"),
            spread_bps=Decimal(4),
        )
        assert result.gross_bps == Decimal(600)
        assert result.cost_bps == Decimal(80)
        assert result.net_bps == Decimal(520)
        assert result.expectancy_bps == Decimal(52)
        assert result.hit_rate == Decimal("0.6")

    def test_the_costly_venue_does_not(self):
        result = run_backtest(
            self.TRADES,
            capabilities=_caps(maker="75", taker="75"),
            spread_bps=Decimal(4),
        )
        assert result.gross_bps == Decimal(600)
        assert result.cost_bps == Decimal(1540)
        assert result.net_bps == Decimal(-940)
        assert result.expectancy_bps == Decimal(-94)
        # The signal did not change. Only the venue did.
        assert result.hit_rate == Decimal("0.6")

    def test_a_venue_that_cannot_place_the_trade_gives_no_number(self):
        perp = _trade(
            method="carry.funding",
            side=Side.SELL,
            market_type=MarketType.PERPETUAL,
        )
        spot_only = Capabilities(
            spot=True,
            margin=False,
            perpetuals=False,
            limit_orders=True,
            shorting=False,
            funding_data=False,
            maker_fee_bps=Decimal(2),
            taker_fee_bps=Decimal(5),
            min_notional=Decimal(10),
        )
        with pytest.raises(Unavailable, match="does not support perpetual"):
            run_backtest([perp], capabilities=spot_only, spread_bps=Decimal(4))

    def test_a_margin_short_is_refused_because_borrow_is_not_modelled(self):
        short = _trade(side=Side.SELL, market_type=MarketType.MARGIN)
        with pytest.raises(Unavailable, match="borrow"):
            run_backtest([short], capabilities=_caps(), spread_bps=Decimal(4))


class TestTheSampleFloor:
    """None is not zero. An unknown rate reported as a number opens a gate."""

    THREE: ClassVar = (
        _trade(entry="100", exit_price="103", barrier=Barrier.UPPER),
        _trade(entry="100", exit_price="103", barrier=Barrier.UPPER),
        _trade(entry="100", exit_price="97", barrier=Barrier.LOWER),
    )

    def test_below_the_floor_the_hit_rate_is_none(self):
        result = run_backtest(
            self.THREE, capabilities=_caps(), spread_bps=Decimal(4)
        )
        assert result.n_trades == 3
        assert result.hits == 2
        assert result.hit_rate is None
        # Explicitly not the numbers a fabricating implementation would return.
        assert result.hit_rate != Decimal(0)
        assert result.hit_rate != Decimal("0.5")

    def test_the_money_is_still_reported_below_the_floor(self):
        # An unknown hit rate does not make the realised P&L unknown.
        result = run_backtest(
            self.THREE, capabilities=_caps(), spread_bps=Decimal(4)
        )
        # 300 + 300 - 300 = 300 gross, 3 x 24 = 72 cost.
        assert result.gross_bps == Decimal(300)
        assert result.net_bps == Decimal(228)

    def test_at_the_floor_the_rate_appears(self):
        result = run_backtest(
            self.THREE,
            capabilities=_caps(),
            spread_bps=Decimal(4),
            min_trades_for_rate=3,
        )
        assert result.hit_rate == Decimal(2) / Decimal(3)

    def test_the_default_floor_matches_the_conviction_gate(self):
        from omni.conviction.gate import MIN_RESOLVED_FOR_CALIBRATION
        from omni.trading.crypto_backtest import MIN_TRADES_FOR_RATE

        assert MIN_TRADES_FOR_RATE == MIN_RESOLVED_FOR_CALIBRATION

    def test_an_empty_book_has_no_expectancy(self):
        with pytest.raises(Unavailable, match="no closed trades"):
            run_backtest([], capabilities=_caps(), spread_bps=Decimal(4))


class TestMaxDrawdown:
    """Drawdown on the cumulative net series, peak to trough, in bps."""

    #  Zero fees, zero spread, spot: net == gross, so the curve is exactly the
    #  five trade returns.
    #
    #  trade net   +500   -300   +200   -600   +100
    #  cumulative  +500   +200   +400   -200   -100
    #  peak so far  500    500    500    500    500
    #  drawdown       0    300    100    700    600
    #
    #  peak +500 after trade 1, trough -200 after trade 4  ->  700bps.

    CURVE: ClassVar = (
        _trade(entry="100", exit_price="105"),
        _trade(entry="100", exit_price="97"),
        _trade(entry="100", exit_price="102"),
        _trade(entry="100", exit_price="94"),
        _trade(entry="100", exit_price="101"),
    )

    def _run(self, trades):
        return run_backtest(
            trades,
            capabilities=_caps(maker="0", taker="0"),
            spread_bps=Decimal(0),
        )

    def test_drawdown_matches_the_hand_derived_value(self):
        result = self._run(self.CURVE)
        assert result.net_bps == Decimal(-100)
        assert result.max_drawdown_bps == Decimal(700)

    def test_a_monotonically_rising_curve_has_no_drawdown(self):
        rising = [
            _trade(entry="100", exit_price="101"),
            _trade(entry="100", exit_price="102"),
        ]
        assert self._run(rising).max_drawdown_bps == Decimal(0)

    def test_a_loss_on_the_first_trade_is_a_drawdown_from_the_start(self):
        # The peak starts at the opening equity, not at the first trade's
        # result: an opening loss of 300bps is a 300bps drawdown.
        opening_loss = [
            _trade(entry="100", exit_price="97"),
            _trade(entry="100", exit_price="105"),
        ]
        assert self._run(opening_loss).max_drawdown_bps == Decimal(300)

    def test_costs_deepen_the_drawdown(self):
        # Same price path, 24bps a trade of friction: the trough after trade 4
        # falls from -200 to -200 - 4x24 = -296, and the peak from +500 to +476,
        # so the drawdown widens from 700 to 772.
        costed = run_backtest(
            self.CURVE, capabilities=_caps(), spread_bps=Decimal(4)
        )
        assert costed.max_drawdown_bps == Decimal(772)


class TestRefusals:
    """Incoherent input produces an exception, never a labelled number."""

    def test_a_trade_that_closed_before_it_opened_raises(self):
        with pytest.raises(ValueError, match="before it opened"):
            ClosedTrade(
                method="trend.sma",
                symbol="BTC/USDT:USDT",
                side=Side.BUY,
                market_type=MarketType.PERPETUAL,
                quantity=Decimal(1),
                entry_price=Decimal(100),
                exit_price=Decimal(101),
                opened_at=OPENED,
                closed_at=OPENED - timedelta(hours=1),
                barrier=Barrier.UPPER,
            )

    def test_a_zero_length_hold_is_allowed_and_accrues_nothing(self):
        trade = _trade(
            method="carry.funding",
            side=Side.SELL,
            market_type=MarketType.PERPETUAL,
            hold_hours=0,
        )
        result = run_backtest(
            [trade],
            capabilities=_caps(maker="0", taker="0"),
            spread_bps=Decimal(0),
            funding_rates=Decimal("0.0001"),
        )
        assert result.funding_collected_bps == Decimal(0)

    @pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), float("nan")])
    def test_a_non_finite_price_raises(self, bad):
        with pytest.raises(ValueError, match="not finite"):
            _trade(entry="100", exit_price=bad)

    @pytest.mark.parametrize("bad", [Decimal("NaN"), float("inf")])
    def test_a_non_finite_quantity_raises(self, bad):
        with pytest.raises(ValueError, match="not finite"):
            _trade(quantity=bad)

    @pytest.mark.parametrize("bad", [Decimal("NaN"), float("nan"), Decimal("-Infinity")])
    def test_a_non_finite_spread_raises(self, bad):
        with pytest.raises(ValueError, match="not finite"):
            run_backtest(
                [_trade(entry="100", exit_price="103")],
                capabilities=_caps(),
                spread_bps=bad,
            )

    def test_a_non_finite_funding_rate_raises(self):
        trade = _trade(
            method="carry.funding",
            side=Side.SELL,
            market_type=MarketType.PERPETUAL,
        )
        with pytest.raises(ValueError, match="not finite"):
            run_backtest(
                [trade],
                capabilities=_caps(),
                spread_bps=Decimal(4),
                funding_rates=Decimal("NaN"),
            )

    def test_a_negative_spread_raises(self):
        with pytest.raises(ValueError, match="must not be negative"):
            run_backtest(
                [_trade(entry="100", exit_price="103")],
                capabilities=_caps(),
                spread_bps=Decimal(-1),
            )

    @pytest.mark.parametrize("bad", [0, -8])
    def test_a_non_positive_settlement_interval_raises(self, bad):
        with pytest.raises(ValueError, match="settlement_hours"):
            run_backtest(
                [_trade(entry="100", exit_price="103")],
                capabilities=_caps(),
                spread_bps=Decimal(4),
                settlement_hours=bad,
            )

    def test_a_zero_sample_floor_raises(self):
        with pytest.raises(ValueError, match="min_trades_for_rate"):
            run_backtest(
                [_trade(entry="100", exit_price="103")],
                capabilities=_caps(),
                spread_bps=Decimal(4),
                min_trades_for_rate=0,
            )

    @pytest.mark.parametrize("bad", ["0", "-1"])
    def test_a_non_positive_quantity_raises(self, bad):
        with pytest.raises(ValueError, match="quantity must be positive"):
            _trade(quantity=bad)

    @pytest.mark.parametrize(("entry", "exit_price"), [("0", "100"), ("100", "0")])
    def test_a_non_positive_price_raises(self, entry, exit_price):
        with pytest.raises(ValueError, match="prices must be positive"):
            _trade(entry=entry, exit_price=exit_price)
