"""Behaviour tests for the execution-analytics capabilities.

Every assertion is arithmetic on a constructed fill set with a known answer,
not a shape check. Every failure path (empty fills, zero-size fill, zero window
volume, fills outside the benchmark window, zero decision price, zero order
quantity) raises ``Unavailable`` -- v1's structure would have divided by a
smuggled-in scalar or returned a fabricated zero, which is how a flattering
"zero cost" enters a report. There is no v1 test file for this module; these
tests are the oracle.

The scheduler/recommender half of v1 (``pre_trade_analytics``,
``_generate_execution_schedule``, ``_generate_execution_recommendations`` and
the pre-trade risk aggregators) is refused per the work order and not imported
here -- ``test_module_decides_nothing`` pins that mechanically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

import numpy as np
import pytest

import omni.capabilities.execution_analytics as ea
from omni.capabilities.execution_analytics import (
    BenchmarkBar,
    Fill,
    ImplementationShortfall,
    arrival_slippage_bps,
    assess_execution_quality,
    benchmark_slippage_bps,
    estimate_linear_impact,
    estimate_power_law_impact,
    estimate_square_root_impact,
    fill_vwap,
    identify_outliers,
    implementation_shortfall,
    interval_twap,
    interval_vwap,
    participation_rate,
    slippage_summary,
    twap_slippage_bps,
    vwap_slippage_bps,
)
from omni.ingest.protocol import Unavailable

_T0 = datetime(2026, 7, 28, 9, 30, tzinfo=UTC)
_T1 = datetime(2026, 7, 28, 10, 30, tzinfo=UTC)
_T2 = datetime(2026, 7, 28, 11, 30, tzinfo=UTC)
_T3 = datetime(2026, 7, 28, 12, 30, tzinfo=UTC)


def _bars(prices: list[float], volumes: list[float], ts=None) -> list[BenchmarkBar]:
    ts = ts or [_T0, _T1, _T2, _T3]
    return [BenchmarkBar(timestamp=t, price=p, volume=v) for t, p, v in zip(ts, prices, volumes)]


# ---------------------------------------------------------------------------
# fill_vwap
# ---------------------------------------------------------------------------

class TestFillVwap:
    def test_size_weighted_average(self):
        fills = [Fill(price=100.0, size=10.0, timestamp=_T0),
                 Fill(price=110.0, size=30.0, timestamp=_T1)]
        # (100*10 + 110*30) / 40 = 4300/40 = 107.5
        assert fill_vwap(fills) == pytest.approx(107.5)

    def test_single_fill_returns_its_price(self):
        assert fill_vwap([Fill(price=42.0, size=5.0, timestamp=_T0)]) == pytest.approx(42.0)


# ---------------------------------------------------------------------------
# interval_vwap / interval_twap
# ---------------------------------------------------------------------------

class TestIntervalBenchmarks:
    def test_interval_vwap_volume_weighted(self):
        bars = _bars([100.0, 110.0], [10.0, 30.0], ts=[_T0, _T1])
        # (100*10 + 110*30)/40 = 4300/40 = 107.5
        assert interval_vwap(bars) == pytest.approx(107.5)

    def test_interval_twap_equal_weighted_mean(self):
        bars = _bars([100.0, 110.0, 120.0], [0.0, 0.0, 0.0], ts=[_T0, _T1, _T2])
        # TWAP ignores volume -> simple mean
        assert interval_twap(bars) == pytest.approx(110.0)

    def test_interval_twap_independent_of_volume(self):
        b_low_vol = _bars([100.0, 110.0], [1.0, 1.0], ts=[_T0, _T1])
        b_high_vol = _bars([100.0, 110.0], [999.0, 1.0], ts=[_T0, _T1])
        assert interval_twap(b_low_vol) == pytest.approx(interval_twap(b_high_vol))


# ---------------------------------------------------------------------------
# participation_rate -- required outcome
# ---------------------------------------------------------------------------

class TestParticipationRate:
    def test_order_equals_whole_interval_volume_is_exactly_one(self):
        bars = _bars([100.0, 101.0], [40.0, 60.0], ts=[_T0, _T1])
        fills = [Fill(price=100.5, size=40.0, timestamp=_T0),
                 Fill(price=100.8, size=60.0, timestamp=_T1)]
        assert participation_rate(fills, bars) == pytest.approx(1.0)

    def test_half_the_volume_is_half(self):
        bars = _bars([100.0, 101.0], [50.0, 50.0], ts=[_T0, _T1])
        fills = [Fill(price=100.5, size=50.0, timestamp=_T0)]
        assert participation_rate(fills, bars) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# vwap_slippage / twap_slippage / arrival_slippage -- required outcomes
# ---------------------------------------------------------------------------

class TestSlippage:
    def test_fills_at_interval_vwap_have_zero_vwap_slippage(self):
        # Construct bars whose VWAP is 105; fills priced so their size-weighted
        # average is also exactly 105 -> zero VWAP slippage.
        bars = _bars([100.0, 110.0], [30.0, 70.0], ts=[_T0, _T1])
        # interval VWAP = (100*30 + 110*70)/100 = 107
        assert interval_vwap(bars) == pytest.approx(107.0)
        fills = [Fill(price=107.0, size=25.0, timestamp=_T0),
                 Fill(price=107.0, size=75.0, timestamp=_T1)]
        assert vwap_slippage_bps(fills, bars, "buy") == pytest.approx(0.0, abs=1e-9)

    def test_buy_vwap_slippage_positive_when_buying_above_vwap(self):
        bars = _bars([100.0, 100.0], [50.0, 50.0], ts=[_T0, _T1])
        fills = [Fill(price=101.0, size=100.0, timestamp=_T0)]  # bought at 101 vs vwap 100
        # (101-100)/100 * 10000 = 100 bps
        assert vwap_slippage_bps(fills, bars, "buy") == pytest.approx(100.0)

    def test_sell_slippage_signs_flip(self):
        # A sell that prints below the benchmark is costly (positive slippage).
        bars = _bars([100.0, 100.0], [50.0, 50.0], ts=[_T0, _T1])
        fills = [Fill(price=99.0, size=100.0, timestamp=_T0)]  # sold at 99 vs vwap 100
        # sell: -(99-100)/100 * 10000 = +100 bps (costly)
        assert vwap_slippage_bps(fills, bars, "sell") == pytest.approx(100.0)

    def test_arrival_slippage_zero_at_arrival(self):
        fills = [Fill(price=100.0, size=10.0, timestamp=_T0)]
        assert arrival_slippage_bps(fills, 100.0, "buy") == pytest.approx(0.0, abs=1e-9)

    def test_twap_slippage_uses_equal_weighted_mean(self):
        # bars: prices 100, 110 -> TWAP 105. Fill VWAP 110 -> +ve slippage for buy.
        bars = _bars([100.0, 110.0], [0.0, 0.0], ts=[_T0, _T1])
        fills = [Fill(price=110.0, size=10.0, timestamp=_T0)]
        # (110 - 105)/105 * 10000 = 47.619... bps
        assert twap_slippage_bps(fills, bars, "buy") == pytest.approx(
            (110.0 - 105.0) / 105.0 * 10_000.0
        )

    def test_benchmark_slippage_bps_zero_when_fill_equals_benchmark(self):
        fills = [Fill(price=50.0, size=5.0, timestamp=_T0)]
        assert benchmark_slippage_bps(fills, 50.0, "buy") == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# implementation_shortfall -- required outcomes
# ---------------------------------------------------------------------------

class TestImplementationShortfall:
    def test_single_fill_at_arrival_price_is_zero_shortfall(self):
        # Decision price == fill price, fully filled -> zero shortfall by
        # construction, regardless of market_price_at_execution / close.
        fills = [Fill(price=100.0, size=100.0, timestamp=_T1)]
        is_ = implementation_shortfall(
            fills,
            decision_price=100.0,
            market_price_at_execution=103.0,  # market moved; irrelevant when E == D
            close_price=99.0,
            order_quantity=100.0,
            side="buy",
        )
        assert is_.total_bps == pytest.approx(0.0, abs=1e-9)
        assert is_.delay_cost_bps == pytest.approx(-is_.trading_cost_bps, abs=1e-9)
        assert is_.opportunity_cost_bps == pytest.approx(0.0, abs=1e-9)

    def test_components_sum_to_total_to_floating_tolerance(self):
        # Partial fill across moving market: components are nonzero and must
        # reconstruct the independent total.
        fills = [Fill(price=105.0, size=60.0, timestamp=_T1)]
        is_ = implementation_shortfall(
            fills,
            decision_price=100.0,
            market_price_at_execution=102.0,
            close_price=108.0,
            order_quantity=100.0,
            side="buy",
        )
        is_.check_additivity(atol=1e-12)
        reconstructed = (
            is_.delay_cost_bps + is_.trading_cost_bps + is_.opportunity_cost_bps
        )
        assert reconstructed == pytest.approx(is_.total_bps, abs=1e-12)

    def test_total_matches_independent_formula(self):
        # Verify the headline number against the closed-form Perold total,
        # not just against the component sum.
        fills = [Fill(price=105.0, size=60.0, timestamp=_T1)]
        d, m, c = 100.0, 102.0, 108.0
        q = 100.0
        filled = 60.0
        f = filled / q
        is_ = implementation_shortfall(
            fills,
            decision_price=d,
            market_price_at_execution=m,
            close_price=c,
            order_quantity=q,
            side="buy",
        )
        expected_rel = (f * (105.0 - d) + (1 - f) * (c - d)) / d
        assert is_.total_bps == pytest.approx(expected_rel * 10_000.0, abs=1e-9)

    def test_sell_side_signs_flip(self):
        fills = [Fill(price=95.0, size=100.0, timestamp=_T1)]
        is_ = implementation_shortfall(
            fills,
            decision_price=100.0,
            market_price_at_execution=98.0,
            close_price=92.0,
            order_quantity=100.0,
            side="sell",
        )
        # Sell at 95 vs decision 100 -> costly. sign=-1, total_rel = -1*(100*(95-100))/100 = +5%
        assert is_.total_bps == pytest.approx(500.0, abs=1e-9)

    def test_opportunity_cost_zero_when_fully_filled(self):
        fills = [Fill(price=105.0, size=100.0, timestamp=_T1)]
        is_ = implementation_shortfall(
            fills,
            decision_price=100.0,
            market_price_at_execution=102.0,
            close_price=200.0,  # would matter only if unfilled
            order_quantity=100.0,
            side="buy",
        )
        assert is_.opportunity_cost_bps == pytest.approx(0.0, abs=1e-12)
        assert is_.fill_rate == pytest.approx(1.0)

    def test_unfilled_portion_contributes_opportunity_cost(self):
        # Fill only 60 of 100; close above decision -> opportunity cost for buys.
        fills = [Fill(price=100.0, size=60.0, timestamp=_T1)]
        is_ = implementation_shortfall(
            fills,
            decision_price=100.0,
            market_price_at_execution=100.0,
            close_price=110.0,
            order_quantity=100.0,
            side="buy",
        )
        # f=0.6, E=D=M=100 -> delay=trading=0; opportunity = 0.4*(110-100)/100 *10000 = 400 bps
        assert is_.opportunity_cost_bps == pytest.approx(400.0, abs=1e-9)
        assert is_.total_bps == pytest.approx(400.0, abs=1e-9)

    def test_check_additivity_raises_if_desynchronised(self):
        is_ = ImplementationShortfall(
            total_bps=10.0,
            delay_cost_bps=1.0,
            trading_cost_bps=2.0,
            opportunity_cost_bps=3.0,  # 1+2+3 = 6 != 10
            fill_rate=1.0,
            side="buy",
            decision_price=100.0,
            market_price_at_execution=100.0,
            close_price=100.0,
            fill_vwap=100.0,
            filled_quantity=100.0,
            order_quantity=100.0,
            n_fills=1,
        )
        with pytest.raises(AssertionError, match="not additive"):
            is_.check_additivity()

    def test_to_dict_round_trips_additive_components(self):
        fills = [Fill(price=105.0, size=60.0, timestamp=_T1)]
        is_ = implementation_shortfall(
            fills,
            decision_price=100.0,
            market_price_at_execution=102.0,
            close_price=108.0,
            order_quantity=100.0,
            side="buy",
        )
        d = is_.to_dict()
        comp_sum = sum(d["components_bps"].values())
        assert comp_sum == pytest.approx(d["total_bps"], abs=1e-9)
        assert d["n_fills"] == 1
        assert d["side"] == "buy"


# ---------------------------------------------------------------------------
# Cross-execution aggregation (faithful ports)
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_assess_execution_quality_bands_match_v1(self):
        assert assess_execution_quality(0.0) == "Excellent"
        assert assess_execution_quality(4.9) == "Excellent"
        assert assess_execution_quality(5.0) == "Good"
        assert assess_execution_quality(9.9) == "Good"
        assert assess_execution_quality(10.0) == "Average"
        assert assess_execution_quality(19.9) == "Average"
        assert assess_execution_quality(20.0) == "Poor"
        assert assess_execution_quality(49.9) == "Poor"
        assert assess_execution_quality(50.0) == "Very Poor"
        assert assess_execution_quality(1_000.0) == "Very Poor"

    def test_assess_execution_quality_uses_absolute_value(self):
        assert assess_execution_quality(-12.0) == "Average"

    def test_slippage_summary_mean_median_std(self):
        series = [10.0, 20.0, 30.0]
        out = slippage_summary(series)
        assert out["mean_bps"] == pytest.approx(20.0)
        assert out["median_bps"] == pytest.approx(20.0)
        assert out["std_bps"] == pytest.approx(np.std(series))
        assert out["n"] == 3

    def test_identify_outliers_flags_two_sigma_deviations(self):
        # 5 values near 0 plus one value at +10 sigma.
        values = [0.0, 0.1, -0.1, 0.0, 0.05, 50.0]
        out = identify_outliers(values)
        assert len(out) == 1
        assert out[0]["index"] == 5
        assert out[0]["z_score"] > 2.0

    def test_identify_outliers_zero_std_returns_empty(self):
        # All identical -> std 0 -> nothing flagged (matches v1's guard).
        assert identify_outliers([5.0, 5.0, 5.0, 5.0]) == []


# ---------------------------------------------------------------------------
# Market-impact models -- bit-for-bit against v1 formulas
# ---------------------------------------------------------------------------

class TestMarketImpact:
    def test_linear_impact_matches_v1_formula(self):
        # v1: temporary = 0.1*pr*1.5*vol*10000 + spread*5000
        #     permanent = 0.05*pr*vol*10000
        pr, vol, spread = 0.05, 0.02, 0.0001
        temp, perm = estimate_linear_impact(pr, vol, spread)
        expected_temp = 0.1 * pr * 1.5 * vol * 10_000 + spread * 5_000
        expected_perm = 0.05 * pr * vol * 10_000
        assert temp == pytest.approx(expected_temp)
        assert perm == pytest.approx(expected_perm)

    def test_sqrt_impact_matches_v1_formula(self):
        # v1: temporary = 0.314 * sqrt(order/adv) * vol * sqrt(252) * 10000 + spread*5000
        #     permanent = 0.142 * sqrt(order/adv) * vol * 10000
        order, adv, vol, spread = 5000.0, 1_000_000.0, 0.02, 0.0002
        temp, perm = estimate_square_root_impact(order, adv, vol, spread)
        part = order / adv
        expected_temp = 0.314 * np.sqrt(part) * vol * np.sqrt(252) * 10_000 + spread * 5_000
        expected_perm = 0.142 * np.sqrt(part) * vol * 10_000
        assert temp == pytest.approx(expected_temp)
        assert perm == pytest.approx(expected_perm)

    def test_power_law_impact_matches_v1_formula(self):
        # v1: impact = 1.0 * (order/mcap)**0.6 * vol * 10000
        order, mcap, vol = 1000.0, 1e9, 0.03
        out = estimate_power_law_impact(order, mcap, vol)
        expected = 1.0 * np.power(order / mcap, 0.6) * vol * 10_000
        assert out == pytest.approx(expected)

    def test_power_law_alpha_override(self):
        out_default = estimate_power_law_impact(1000.0, 1e9, 0.03)
        out_half = estimate_power_law_impact(1000.0, 1e9, 0.03, alpha=0.5)
        assert out_default != pytest.approx(out_half)


# ---------------------------------------------------------------------------
# Failure paths -- each raises Unavailable
# ---------------------------------------------------------------------------

class TestFailurePaths:
    def test_empty_fill_set_raises(self):
        with pytest.raises(Unavailable, match="no fills"):
            fill_vwap([])
        with pytest.raises(Unavailable, match="no fills"):
            arrival_slippage_bps([], 100.0, "buy")
        with pytest.raises(Unavailable, match="no fills"):
            implementation_shortfall(
                [],
                decision_price=100.0,
                market_price_at_execution=100.0,
                close_price=100.0,
                order_quantity=100.0,
                side="buy",
            )

    def test_fill_with_zero_size_raises(self):
        with pytest.raises(Unavailable, match="zero size"):
            fill_vwap([Fill(price=100.0, size=0.0, timestamp=_T0)])

    def test_fill_with_negative_size_raises(self):
        with pytest.raises(Unavailable, match="negative size"):
            fill_vwap([Fill(price=100.0, size=-5.0, timestamp=_T0)])

    def test_empty_benchmark_window_raises(self):
        with pytest.raises(Unavailable, match="window is empty"):
            interval_vwap([])
        with pytest.raises(Unavailable, match="window is empty"):
            interval_twap([])

    def test_window_with_zero_volume_raises_not_divide_by_zero(self):
        bars = _bars([100.0, 110.0], [0.0, 0.0], ts=[_T0, _T1])
        with pytest.raises(Unavailable, match="zero volume"):
            interval_vwap(bars)
        fills = [Fill(price=100.0, size=10.0, timestamp=_T0)]
        with pytest.raises(Unavailable, match="zero volume"):
            vwap_slippage_bps(fills, bars, "buy")
        with pytest.raises(Unavailable, match="zero volume"):
            participation_rate(fills, bars)

    def test_fills_outside_benchmark_window_raises(self):
        bars = _bars([100.0, 100.0], [10.0, 10.0], ts=[_T1, _T2])
        outside = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)  # before the window starts
        fills = [Fill(price=100.0, size=10.0, timestamp=outside)]
        with pytest.raises(Unavailable, match="outside the benchmark window"):
            vwap_slippage_bps(fills, bars, "buy")
        with pytest.raises(Unavailable, match="outside the benchmark window"):
            participation_rate(fills, bars)

    def test_decision_price_zero_raises(self):
        fills = [Fill(price=100.0, size=10.0, timestamp=_T0)]
        with pytest.raises(Unavailable, match="decision_price is zero"):
            implementation_shortfall(
                fills,
                decision_price=0.0,
                market_price_at_execution=100.0,
                close_price=100.0,
                order_quantity=10.0,
                side="buy",
            )

    def test_benchmark_price_zero_raises(self):
        fills = [Fill(price=100.0, size=10.0, timestamp=_T0)]
        with pytest.raises(Unavailable, match="benchmark price is zero"):
            benchmark_slippage_bps(fills, 0.0, "buy")
        with pytest.raises(Unavailable, match="benchmark price is zero"):
            arrival_slippage_bps(fills, 0.0, "buy")

    def test_order_quantity_zero_raises(self):
        fills = [Fill(price=100.0, size=10.0, timestamp=_T0)]
        with pytest.raises(Unavailable, match="order_quantity must be positive"):
            implementation_shortfall(
                fills,
                decision_price=100.0,
                market_price_at_execution=100.0,
                close_price=100.0,
                order_quantity=0.0,
                side="buy",
            )

    def test_filled_exceeds_order_quantity_raises(self):
        fills = [Fill(price=100.0, size=100.0, timestamp=_T0)]
        with pytest.raises(Unavailable, match="exceeds order_quantity"):
            implementation_shortfall(
                fills,
                decision_price=100.0,
                market_price_at_execution=100.0,
                close_price=100.0,
                order_quantity=50.0,  # filled 100 > 50
                side="buy",
            )

    def test_unknown_side_raises(self):
        fills = [Fill(price=100.0, size=10.0, timestamp=_T0)]
        with pytest.raises(Unavailable, match="side must be 'buy' or 'sell'"):
            arrival_slippage_bps(fills, 100.0, "hold")

    def test_sqrt_impact_zero_adv_raises(self):
        with pytest.raises(Unavailable, match="adv is zero"):
            estimate_square_root_impact(1000.0, 0.0, 0.02, 0.0001)

    def test_power_law_zero_market_cap_raises(self):
        with pytest.raises(Unavailable, match="market_cap is zero"):
            estimate_power_law_impact(1000.0, 0.0, 0.02)

    def test_slippage_summary_empty_raises(self):
        with pytest.raises(Unavailable, match="slippage series is empty"):
            slippage_summary([])

    def test_identify_outliers_empty_raises(self):
        with pytest.raises(Unavailable, match="values is empty"):
            identify_outliers([])


# ---------------------------------------------------------------------------
# Scope guard -- this module decides nothing
# ---------------------------------------------------------------------------

class TestModuleDecidesNothing:
    """The refused v1 surface must not exist in the port, and the module must
    not import the execution tier (the only layer that acts on the world)."""

    REFUSED: ClassVar[list[str]] = [
        "pre_trade_analytics",
        "_generate_execution_schedule",
        "_generate_execution_recommendations",
        "_calculate_execution_risk",
        "_calculate_aggregate_impact",
        "_calculate_portfolio_execution_risk",
        "ExecutionAnalytics",
        "ExecutionAlgorithm",
        "MarketImpactModel",
    ]

    def test_refused_surface_absent(self):
        for name in self.REFUSED:
            assert not hasattr(ea, name), f"refused name {name!r} leaked into the port"

    def test_module_imports_nothing_from_execution_tier(self):
        import inspect

        src = inspect.getsource(ea)
        assert "omni.execution" not in src, (
            "execution_analytics imports the execution tier -- it must not; "
            "broker.py is the only layer that acts on the world"
        )
