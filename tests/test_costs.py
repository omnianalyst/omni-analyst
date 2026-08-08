"""The cost model, and the venue-comparison arithmetic it exists to support.

Two groups of assertions carry weight here.

**The funding sign.** Funding is the only signed component, and the
funding-carry producer's whole thesis is receiving it rather than paying it. A
model that treated funding as an unconditional cost would price the carry
strategy as its own opposite and it would look unprofitable exactly when it
works. Both directions are asserted, not just that the number is non-zero.

**The worked example reproduces.** `AUTOTRADE_PLAN.md` section 12 states four
venue verdicts for one signal. If the module and the plan ever disagree, one of
them is wrong and this test says which. That is the point of pinning a document's
arithmetic in a test rather than trusting it stays true.

Gas is checked against notional rather than as a constant because gas as a
*fraction* is the entire reason small on-chain trades cannot carry a thin edge,
and a test asserting `gas_bps > 0` would pass for an implementation that
ignored trade size completely.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from omni.venue.costs import (
    BPS,
    CostBreakdown,
    carry_cost,
    entry_cost,
    gross_expectancy_bps,
    round_trip_cost,
    survives_costs,
)
from omni.venue.protocol import Capabilities, MarketType, OrderKind, Side, TradeIntent


def _caps(maker: str = "2", taker: str = "10", **overrides) -> Capabilities:
    base = {
        "spot": True,
        "margin": True,
        "perpetuals": True,
        "limit_orders": True,
        "shorting": True,
        "funding_data": True,
        "maker_fee_bps": Decimal(maker),
        "taker_fee_bps": Decimal(taker),
        "min_notional": Decimal(10),
    }
    return Capabilities(**{**base, **overrides})


def _intent(
    *,
    side: Side = Side.BUY,
    quantity: str = "1",
    price: str = "5000",
    market_type: MarketType = MarketType.SPOT,
) -> TradeIntent:
    return TradeIntent(
        venue="test",
        symbol="BTC/USD",
        side=side,
        market_type=market_type,
        quantity=Decimal(quantity),
        reference_price=Decimal(price),
        order_kind=OrderKind.MARKET,
    )


class TestGrossExpectancy:
    def test_the_plans_worked_example(self):
        # 0.73 x 300bps - 0.27 x 200bps = 219 - 54 = 165bps
        assert gross_expectancy_bps(
            hit_rate=Decimal("0.73"),
            target_bps=Decimal(300),
            stop_bps=Decimal(200),
        ) == Decimal(165)

    def test_a_coin_flip_on_symmetric_barriers_is_worth_nothing(self):
        assert gross_expectancy_bps(
            hit_rate=Decimal("0.5"),
            target_bps=Decimal(200),
            stop_bps=Decimal(200),
        ) == Decimal(0)

    def test_a_losing_edge_is_reported_negative_not_clamped(self):
        assert gross_expectancy_bps(
            hit_rate=Decimal("0.3"),
            target_bps=Decimal(100),
            stop_bps=Decimal(100),
        ) == Decimal(-40)

    def test_stop_stated_negative_is_refused_rather_than_absolved(self):
        # A caller passing -200 means the same loss, but silently accepting it
        # would ADD the loss to the expectancy instead of subtracting it.
        with pytest.raises(ValueError, match="stated as a magnitude"):
            gross_expectancy_bps(
                hit_rate=Decimal("0.73"),
                target_bps=Decimal(300),
                stop_bps=Decimal(-200),
            )

    def test_hit_rate_above_one_is_refused(self):
        with pytest.raises(ValueError, match="hit_rate out of range"):
            gross_expectancy_bps(
                hit_rate=Decimal("1.5"),
                target_bps=Decimal(300),
                stop_bps=Decimal(200),
            )


class TestEntryCost:
    def test_taker_pays_the_taker_fee_and_crosses_half_the_spread(self):
        cost = entry_cost(
            _intent(), _caps(taker="10"), spread_bps=Decimal(4), is_maker=False
        )
        assert cost.fee_bps == Decimal(10)
        assert cost.spread_bps == Decimal(2)

    def test_maker_pays_the_maker_fee_and_crosses_nothing(self):
        cost = entry_cost(
            _intent(), _caps(maker="2"), spread_bps=Decimal(4), is_maker=True
        )
        assert cost.fee_bps == Decimal(2)
        assert cost.spread_bps == Decimal(0)

    def test_gas_is_a_fraction_of_notional_not_a_constant(self):
        # The same $40 is 80bps on $5,000 and 0.8bps on $500,000. An
        # implementation ignoring size would return the same number twice.
        small = entry_cost(
            _intent(quantity="1", price="5000"), _caps(), gas_quote=Decimal(40)
        )
        large = entry_cost(
            _intent(quantity="100", price="5000"), _caps(), gas_quote=Decimal(40)
        )
        assert small.gas_bps == Decimal(80)
        assert large.gas_bps == Decimal("0.8")

    def test_zero_gas_on_a_cex_contributes_nothing(self):
        assert entry_cost(_intent(), _caps()).gas_bps == Decimal(0)

    def test_negative_spread_is_refused(self):
        with pytest.raises(ValueError, match="spread_bps"):
            entry_cost(_intent(), _caps(), spread_bps=Decimal(-1))


class TestCarryCostFundingSign:
    """Positive funding rate means longs pay shorts."""

    def test_a_long_paying_funding_accrues_a_cost(self):
        cost = carry_cost(
            _intent(side=Side.BUY, market_type=MarketType.PERPETUAL),
            funding_rate=Decimal("0.0001"),
            funding_periods=3,
        )
        assert cost.funding_bps == Decimal(3)
        assert cost.total_bps > 0

    def test_a_short_receiving_funding_accrues_a_credit(self):
        cost = carry_cost(
            _intent(side=Side.SELL, market_type=MarketType.PERPETUAL),
            funding_rate=Decimal("0.0001"),
            funding_periods=3,
        )
        assert cost.funding_bps == Decimal(-3)
        assert cost.total_bps < 0

    def test_negative_funding_inverts_both_sides(self):
        # When funding goes negative, shorts pay longs -- the carry trade flips.
        long_cost = carry_cost(
            _intent(side=Side.BUY, market_type=MarketType.PERPETUAL),
            funding_rate=Decimal("-0.0001"),
            funding_periods=3,
        )
        short_cost = carry_cost(
            _intent(side=Side.SELL, market_type=MarketType.PERPETUAL),
            funding_rate=Decimal("-0.0001"),
            funding_periods=3,
        )
        assert long_cost.funding_bps == Decimal(-3)
        assert short_cost.funding_bps == Decimal(3)

    def test_funding_scales_with_periods_held(self):
        one = carry_cost(
            _intent(market_type=MarketType.PERPETUAL),
            funding_rate=Decimal("0.0001"),
            funding_periods=1,
        )
        ten = carry_cost(
            _intent(market_type=MarketType.PERPETUAL),
            funding_rate=Decimal("0.0001"),
            funding_periods=10,
        )
        assert ten.funding_bps == one.funding_bps * 10

    def test_zero_periods_accrues_no_funding(self):
        assert carry_cost(
            _intent(market_type=MarketType.PERPETUAL),
            funding_rate=Decimal("0.01"),
            funding_periods=0,
        ).funding_bps == Decimal(0)


class TestCarryCostBorrow:
    def test_a_margin_short_pays_borrow(self):
        cost = carry_cost(
            _intent(side=Side.SELL, market_type=MarketType.MARGIN),
            borrow_rate_bps_per_period=Decimal(5),
            borrow_periods=4,
        )
        assert cost.borrow_bps == Decimal(20)

    def test_a_perpetual_short_pays_funding_not_borrow(self):
        # A perp short is synthetic -- nothing is located, nothing is borrowed.
        # Charging both would double-count the cost of being short.
        cost = carry_cost(
            _intent(side=Side.SELL, market_type=MarketType.PERPETUAL),
            borrow_rate_bps_per_period=Decimal(5),
            borrow_periods=4,
        )
        assert cost.borrow_bps == Decimal(0)

    def test_a_long_never_pays_borrow(self):
        cost = carry_cost(
            _intent(side=Side.BUY, market_type=MarketType.MARGIN),
            borrow_rate_bps_per_period=Decimal(5),
            borrow_periods=4,
        )
        assert cost.borrow_bps == Decimal(0)


class TestRoundTrip:
    def test_both_legs_are_charged(self):
        one_leg = entry_cost(_intent(), _caps(taker="10"))
        both = round_trip_cost(_intent(), _caps(taker="10"))
        assert both.fee_bps == one_leg.fee_bps * 2

    def test_exit_leg_can_be_taker_while_entry_is_maker(self):
        # Enter passively, stop out at market: the exit is a taker exit by
        # definition, and assuming otherwise understates the losing trades.
        cost = round_trip_cost(
            _intent(), _caps(maker="2", taker="10"), is_maker=True, exit_is_maker=False
        )
        assert cost.fee_bps == Decimal(12)

    def test_gas_is_charged_on_both_legs(self):
        cost = round_trip_cost(
            _intent(quantity="1", price="5000"), _caps(), gas_quote=Decimal(40)
        )
        assert cost.gas_bps == Decimal(160)

    def test_carry_is_charged_once_not_per_leg(self):
        cost = round_trip_cost(
            _intent(side=Side.BUY, market_type=MarketType.PERPETUAL),
            _caps(),
            funding_rate=Decimal("0.0001"),
            funding_periods=3,
        )
        assert cost.funding_bps == Decimal(3)


class TestCostBreakdown:
    def test_a_negative_fee_is_refused(self):
        with pytest.raises(ValueError, match="fee_bps must not be negative"):
            CostBreakdown(fee_bps=Decimal(-1))

    def test_a_negative_funding_is_permitted_because_it_is_a_credit(self):
        assert CostBreakdown(funding_bps=Decimal(-5)).total_bps == Decimal(-5)

    def test_addition_sums_componentwise(self):
        a = CostBreakdown(fee_bps=Decimal(2), gas_bps=Decimal(10))
        b = CostBreakdown(fee_bps=Decimal(3), funding_bps=Decimal(-4))
        total = a + b
        assert total.fee_bps == Decimal(5)
        assert total.gas_bps == Decimal(10)
        assert total.funding_bps == Decimal(-4)
        assert total.total_bps == Decimal(11)

    def test_as_fraction_converts_out_of_bps(self):
        assert CostBreakdown(fee_bps=Decimal(165)).as_fraction() == Decimal("0.0165")


class TestPlanSection12WorkedExample:
    """Pin AUTOTRADE_PLAN.md section 12. If these drift, one of them is wrong."""

    GROSS = Decimal(165)

    def test_gross_expectancy_matches_the_plan(self):
        assert gross_expectancy_bps(
            hit_rate=Decimal("0.73"),
            target_bps=Decimal(300),
            stop_bps=Decimal(200),
        ) == self.GROSS

    def test_cex_taker_ten_bps_per_side_nets_145bps(self):
        cost = round_trip_cost(_intent(), _caps(taker="10"), is_maker=False)
        assert cost.total_bps == Decimal(20)
        verdict = survives_costs(gross_bps=self.GROSS, cost=cost)
        assert verdict.net_bps == Decimal(145)
        assert verdict.survives

    def test_cex_maker_two_bps_per_side_nets_161bps(self):
        cost = round_trip_cost(_intent(), _caps(maker="2"), is_maker=True)
        assert cost.total_bps == Decimal(4)
        verdict = survives_costs(gross_bps=self.GROSS, cost=cost)
        assert verdict.net_bps == Decimal(161)
        assert verdict.survives

    def test_onchain_forty_dollars_gas_on_five_thousand_nets_5bps(self):
        cost = round_trip_cost(
            _intent(quantity="1", price="5000"),
            _caps(maker="0", taker="0"),
            gas_quote=Decimal(40),
        )
        assert cost.total_bps == Decimal(160)
        verdict = survives_costs(gross_bps=self.GROSS, cost=cost)
        assert verdict.net_bps == Decimal(5)

    def test_onchain_is_marginal_and_a_realistic_margin_rejects_it(self):
        cost = round_trip_cost(
            _intent(quantity="1", price="5000"),
            _caps(maker="0", taker="0"),
            gas_quote=Decimal(40),
        )
        assert survives_costs(gross_bps=self.GROSS, cost=cost).survives
        assert not survives_costs(
            gross_bps=self.GROSS, cost=cost, margin_bps=Decimal(25)
        ).survives

    def test_swap_service_at_seventy_five_bps_per_leg_nets_15bps(self):
        cost = round_trip_cost(
            _intent(), _caps(maker="75", taker="75"), is_maker=False
        )
        assert cost.total_bps == Decimal(150)
        verdict = survives_costs(
            gross_bps=self.GROSS, cost=cost, margin_bps=Decimal(25)
        )
        assert verdict.net_bps == Decimal(15)
        assert not verdict.survives

    def test_the_same_signal_survives_on_a_cex_and_fails_on_a_swap_service(self):
        cheap = round_trip_cost(_intent(), _caps(taker="10"))
        dear = round_trip_cost(_intent(), _caps(maker="75", taker="75"))
        margin = Decimal(25)
        assert survives_costs(gross_bps=self.GROSS, cost=cheap, margin_bps=margin).survives
        assert not survives_costs(gross_bps=self.GROSS, cost=dear, margin_bps=margin).survives


class TestViability:
    def test_explain_names_every_component(self):
        cost = round_trip_cost(
            _intent(market_type=MarketType.PERPETUAL, side=Side.SELL),
            _caps(taker="10"),
            gas_quote=Decimal(0),
            funding_rate=Decimal("0.0001"),
            funding_periods=3,
        )
        text = survives_costs(gross_bps=Decimal(165), cost=cost).explain()
        for token in ("gross", "fee", "spread", "gas", "borrow", "funding", "net"):
            assert token in text
        # A received credit must read as a credit, not as a cost.
        assert "funding -3.0" in text

    def test_a_zero_margin_accepts_any_positive_net(self):
        cost = CostBreakdown(fee_bps=Decimal(164))
        assert survives_costs(gross_bps=Decimal(165), cost=cost).survives

    def test_break_even_does_not_survive(self):
        cost = CostBreakdown(fee_bps=Decimal(165))
        verdict = survives_costs(gross_bps=Decimal(165), cost=cost)
        assert verdict.net_bps == Decimal(0)
        assert not verdict.survives

    def test_negative_margin_is_refused(self):
        with pytest.raises(ValueError, match="margin_bps"):
            survives_costs(
                gross_bps=Decimal(165),
                cost=CostBreakdown(),
                margin_bps=Decimal(-1),
            )

    def test_a_funding_credit_can_make_an_otherwise_dead_edge_viable(self):
        # The carry producer's entire thesis in one assertion.
        intent = _intent(side=Side.SELL, market_type=MarketType.PERPETUAL)
        caps = _caps(taker="10")
        without = round_trip_cost(intent, caps)
        with_credit = round_trip_cost(
            intent, caps, funding_rate=Decimal("0.0005"), funding_periods=6
        )
        gross = Decimal(10)
        assert not survives_costs(gross_bps=gross, cost=without).survives
        assert survives_costs(gross_bps=gross, cost=with_credit).survives


def test_bps_constant_is_ten_thousand():
    assert BPS == Decimal(10_000)
