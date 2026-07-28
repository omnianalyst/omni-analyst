"""Behaviour tests for the macro capabilities.

Every assertion is on a hand-computed value for a known input, not on shape.
Every entry point also has a test that its dependency being unavailable raises
`Unavailable` rather than substituting the zero / empty-dict / "stable" default
v1 returned.
"""

import math

import numpy as np
import pytest

from omni.capabilities.macro import (
    assess_policy_implications,
    assess_recession_risk,
    assess_scenario_impact,
    calculate_inflation_trend,
    calculate_percentile,
    calculate_trend,
    categorize_series,
    generate_simple_forecast,
    historical_context,
    inflation_expectations,
    inflation_measures,
    labor_market_tightness,
    pce_inflation,
    period_change_percent,
    policy_recommendation,
    policy_stance,
    recession_probability,
    sahm_rule,
    taylor_rule,
    taylor_rule_variant,
    yield_curve_inversion,
)
from omni.ingest.protocol import Unavailable


class TestCalculateTrend:
    def test_strongly_rising_series_is_rising_sharply(self):
        values = [100.0 * (1.05 ** i) for i in range(8)]
        assert calculate_trend(values) == "rising_sharply"

    def test_strongly_falling_series_is_falling_sharply(self):
        values = [100.0 * (0.95 ** i) for i in range(8)]
        assert calculate_trend(values) == "falling_sharply"

    def test_flat_series_is_stable(self):
        values = [50.0, 50.1, 49.9, 50.0, 50.05, 49.95]
        assert calculate_trend(values) == "stable"

    def test_insufficient_data(self):
        assert calculate_trend([1.0]) == "insufficient_data"
        assert calculate_trend([]) == "insufficient_data"

    def test_zero_mean_returns_stable(self):
        assert calculate_trend([0.0, 0.0, 0.0]) == "stable"


class TestGenerateSimpleForecast:
    def test_forecast_is_exponential_smoothing_flat_level(self):
        out = generate_simple_forecast([3.0, 3.0, 3.0, 3.0, 3.0], periods=3)
        assert set(out) == {"period_1", "period_2", "period_3"}
        assert out["period_1"] == pytest.approx(3.0)
        assert out["period_2"] == pytest.approx(3.0)
        assert out["period_3"] == pytest.approx(3.0)

    def test_forecast_converges_asymptotically_toward_latest(self):
        # s starts at values[0]=0, alpha=0.3 -> s_n = 10*(1 - 0.7^n); after 20
        # updates it is within ~0.01 of the level the series settled at.
        out = generate_simple_forecast([0.0] + [10.0] * 20, periods=1)
        assert out["period_1"] == pytest.approx(10.0, abs=0.01)

    def test_too_few_values_raises(self):
        with pytest.raises(Unavailable, match="fewer than 3 values"):
            generate_simple_forecast([1.0, 2.0])
        with pytest.raises(Unavailable, match="fewer than 3 values"):
            generate_simple_forecast([])


class TestCalculatePercentile:
    def test_value_at_median_is_50(self):
        out = calculate_percentile(5.0, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0])
        assert out == pytest.approx((5 / 9) * 100)

    def test_max_value_is_100th_percentile(self):
        all_vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert calculate_percentile(5.0, all_vals) == pytest.approx(100.0)

    def test_min_value(self):
        all_vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert calculate_percentile(1.0, all_vals) == pytest.approx((1 / 5) * 100)

    def test_empty_raises(self):
        with pytest.raises(Unavailable, match="no values"):
            calculate_percentile(1.0, [])


class TestCalculateInflationTrend:
    def test_known_momentum_and_annualization(self):
        # Increasing absolute increments -> percentage changes grow, so the
        # most recent change exceeds the one three months back: accelerating.
        cpi = [100, 101, 103, 106, 110, 115, 121, 128, 136, 145, 155, 166]
        out = calculate_inflation_trend(cpi)

        recent = (166.0 - 155.0) / 155.0 * 100
        three_back = (145.0 - 136.0) / 136.0 * 100
        assert out["recent_mom_changes"][0] == pytest.approx(recent)
        assert out["recent_mom_changes"][2] == pytest.approx(three_back)
        assert out["momentum"] == "accelerating"
        assert out["3m_annualized"] == pytest.approx(
            np.mean(out["recent_mom_changes"]) * 12
        )

    def test_decelerating_when_recent_below_older(self):
        # Index rises fast then slows: late months grow less than early ones
        cpi = [100.0, 110.0, 119.0, 127.0, 130.0, 131.0, 131.5]
        out = calculate_inflation_trend(cpi)
        assert out["momentum"] == "decelerating"

    def test_too_few_raises(self):
        with pytest.raises(Unavailable, match="fewer than 6 CPI"):
            calculate_inflation_trend([100.0, 101.0, 102.0])


class TestAssessRecessionRisk:
    @pytest.mark.parametrize(
        "probability,expected",
        [
            (0.8, "high"),
            (0.7, "high"),
            (0.69, "elevated"),
            (0.4, "elevated"),
            (0.39, "moderate"),
            (0.2, "moderate"),
            (0.19, "low"),
            (0.0, "low"),
        ],
    )
    def test_threshold_boundaries(self, probability, expected):
        assert assess_recession_risk(probability) == expected


class TestCategorizeSeries:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Federal Funds Effective Rate", "monetary_policy"),
            ("Unemployment Rate", "employment"),
            ("Consumer Price Index for All Urban Consumers", "inflation"),
            ("Real Gross Domestic Product", "gdp"),
            ("10-Year Treasury Constant Maturity Rate", "interest_rates"),
            ("Bank Credit, All Commercial Banks", "banking"),
            ("Trade Balance: Goods and Services", "international"),
            ("Housing Starts: Total", "housing"),
            ("Retail Sales: Total", "consumer"),
            ("Industrial Production Index", "business"),
            ("Obscure Unmatched Series", "monetary_policy"),
        ],
    )
    def test_keyword_routing(self, title, expected):
        assert categorize_series(title) == expected


class TestTaylorRule:
    def test_known_value_at_target_inflation_and_zero_gap(self):
        # inflation == target, output_gap == 0 -> r* + inflation
        out = taylor_rule(2.0, 0.0)
        assert out == pytest.approx(0.5 + 2.0)

    def test_known_value_with_inflation_and_output_gap(self):
        # 0.5 + 3.0 + 1.5*(3.0-2.0) + 0.5*(-1.0) = 0.5+3.0+1.5-0.5 = 4.5
        out = taylor_rule(3.0, -1.0)
        assert out == pytest.approx(4.5)

    def test_custom_weights_respected(self):
        out = taylor_rule(
            3.0, 1.0, inflation_target=2.0, neutral_real_rate=0.5,
            inflation_weight=2.0, output_gap_weight=1.0,
        )
        # 0.5 + 3.0 + 2.0*(1.0) + 1.0*(1.0) = 6.5
        assert out == pytest.approx(6.5)


class TestTaylorRuleVariant:
    def test_matches_explicit_formula(self):
        out = taylor_rule_variant(3.0, -1.0, target=2.0, r_star=0.5, alpha=1.5, beta=0.5)
        # 0.5 + 3.0 + 1.5*(3.0-2.0) + 0.5*(-1.0) = 4.5
        assert out == pytest.approx(4.5)


class TestPolicyStance:
    @pytest.mark.parametrize(
        "deviation,expected",
        [
            (1.5, "restrictive"),
            (1.01, "restrictive"),
            (1.0, "neutral"),
            (0.0, "neutral"),
            (-1.0, "neutral"),
            (-1.01, "accommodative"),
            (-2.0, "accommodative"),
        ],
    )
    def test_thresholds(self, deviation, expected):
        assert policy_stance(deviation) == expected


class TestPolicyRecommendation:
    @pytest.mark.parametrize(
        "taylor_rate,current_rate,expected_substring",
        [
            (5.0, 5.0, "Maintain"),
            (5.3, 5.0, "Gradual tightening"),
            (7.0, 5.0, "accelerated tightening"),
            (4.7, 5.0, "Gradual easing"),
            (3.0, 5.0, "accelerated easing"),
        ],
    )
    def test_recommendation_text(self, taylor_rate, current_rate, expected_substring):
        assert expected_substring in policy_recommendation(taylor_rate, current_rate)


class TestAssessPolicyImplications:
    def test_accommodative_with_inflation_risk(self):
        # taylor well above current -> stance accommodative, inflation risk flagged
        out = assess_policy_implications(6.0, 4.0, "accommodative")
        assert out["rate_adjustment_needed"] == pytest.approx(2.0)
        assert out["stance"] == "accommodative"
        assert "Inflation risk from overly loose policy" in out["risks"]
        assert "tightening" in out["recommendation"].lower()

    def test_restrictive_with_growth_risk(self):
        out = assess_policy_implications(2.0, 4.0, "restrictive")
        assert out["rate_adjustment_needed"] == pytest.approx(-2.0)
        assert "Growth risk from overly tight policy" in out["risks"]

    def test_neutral_has_no_risks(self):
        out = assess_policy_implications(5.0, 5.0, "neutral")
        assert out["risks"] == []
        assert out["recommendation"] == "Maintain current policy stance"


class TestSahmRule:
    async def test_triggered_when_3m_avg_rose_above_12m_low(self):
        # last 3 = [4.0, 4.5, 5.0] -> avg 4.5; last-12 low = 3.9 (the leading
        # 3.8 is outside the trailing 12-month window) -> Sahm value 0.6 >= 0.5
        unemployment = [3.8, 3.9, 4.0, 4.0, 4.1, 4.0, 4.0, 3.9, 4.0, 4.0, 4.0, 4.5, 5.0]
        out = await sahm_rule(unemployment)
        assert out["value"] == pytest.approx(0.6)
        assert out["triggered"]
        assert out["current_unemployment"] == 5.0
        assert out["12m_low"] == pytest.approx(3.9)

    async def test_not_triggered_below_threshold(self):
        unemployment = [3.5] * 9 + [3.6, 3.6, 3.7, 3.7]
        out = await sahm_rule(unemployment)
        last3 = [3.6, 3.7, 3.7]
        assert out["value"] == pytest.approx(np.mean(last3) - 3.5)
        assert not out["triggered"]

    async def test_too_few_observations_raises(self):
        with pytest.raises(Unavailable, match=">=12"):
            await sahm_rule([4.0] * 11)


class TestYieldCurveInversion:
    async def test_inverted_curve_detected(self):
        s2y = {"d1": 4.5, "d2": 4.4, "d3": 4.3}
        s10y = {"d1": 4.0, "d2": 4.1, "d3": 4.2}
        out = await yield_curve_inversion(s2y, s10y)
        assert out["current_spread"] == pytest.approx(4.2 - 4.3)
        assert out["is_inverted"] is True
        assert out["days_inverted_90d"] == 3

    async def test_normal_curve(self):
        s2y = {"d1": 4.0}
        s10y = {"d1": 4.5}
        out = await yield_curve_inversion(s2y, s10y)
        assert out["is_inverted"] is False
        assert out["current_spread"] == pytest.approx(0.5)

    async def test_no_overlap_raises(self):
        with pytest.raises(Unavailable, match="no overlapping dates"):
            await yield_curve_inversion({"a": 1.0}, {"b": 1.0})


class TestRecessionProbability:
    async def test_all_signals_negative_maxes_below_one(self):
        out = await recession_probability(
            yield_curve_inverted=True,
            sahm_triggered=True,
            lei_signals=["negative", "negative"],
        )
        # 0.3 + 0.4 + (2/2)*0.3 = 1.0
        assert out["probability"] == pytest.approx(1.0)
        assert out["assessment"] == "high"

    async def test_no_signals_is_low(self):
        out = await recession_probability(
            yield_curve_inverted=False,
            sahm_triggered=False,
            lei_signals=["positive", "positive"],
        )
        assert out["probability"] == pytest.approx(0.0)
        assert out["assessment"] == "low"

    async def test_partial_signals(self):
        out = await recession_probability(
            yield_curve_inverted=True,
            sahm_triggered=False,
            lei_signals=["negative", "positive"],
        )
        # 0.3 + 0 + (1/2)*0.3 = 0.45 -> elevated
        assert out["probability"] == pytest.approx(0.45)
        assert out["assessment"] == "elevated"

    async def test_empty_lei_is_allowed_and_does_not_diverge(self):
        out = await recession_probability(
            yield_curve_inverted=False, sahm_triggered=False, lei_signals=[]
        )
        assert out["probability"] == pytest.approx(0.0)


class TestInflationMeasures:
    async def test_known_yoy_mom_and_3m(self):
        # 13 months, index 300 -> 307.5 over the year
        cpi = [300.0, 301.0, 302.0, 303.0, 304.0, 305.0, 306.0,
               306.5, 307.0, 307.0, 307.2, 307.3, 307.5]
        out = await inflation_measures(cpi)
        current, year_ago = 307.5, 300.0
        assert out["yoy"] == pytest.approx((current - year_ago) / year_ago * 100)
        prev, three_back = 307.3, 307.0
        assert out["mom_annualized"] == pytest.approx(
            ((current - prev) / prev) * 100 * 12
        )
        assert out["3m_annualized"] == pytest.approx(
            ((current - three_back) / three_back) * 100 * 4
        )
        assert out["current_index"] == 307.5
        assert "trend" in out

    async def test_too_few_raises(self):
        with pytest.raises(Unavailable, match=">=13"):
            await inflation_measures([100.0] * 12)


class TestPceInflation:
    async def test_known_yoy_and_distance_from_target(self):
        pce = [100.0] * 12 + [104.0]
        out = await pce_inflation(pce)
        assert out["yoy"] == pytest.approx(((104.0 - 100.0) / 100.0) * 100)
        assert out["vs_target"] == pytest.approx(4.0 - 2.0)
        assert out["distance_from_target"] == pytest.approx(2.0)

    async def test_custom_target(self):
        pce = [100.0] * 12 + [103.0]
        out = await pce_inflation(pce, target=3.0)
        assert out["vs_target"] == pytest.approx(0.0)

    async def test_too_few_raises(self):
        with pytest.raises(Unavailable, match=">=13"):
            await pce_inflation([100.0] * 5)


class TestInflationExpectations:
    async def test_known_5y5y_forward_and_anchored(self):
        # 10y latest 2.4 is 0.4 from the 2.0 target -> within 0.5 -> anchored
        out = await inflation_expectations([2.2, 2.3], [2.3, 2.4])
        assert out["5y"] == 2.3
        assert out["10y"] == 2.4
        assert out["5y5y_forward"] == pytest.approx(2 * 2.4 - 2.3)
        assert out["anchored"] is True

    async def test_unanchored_when_far_from_two(self):
        out = await inflation_expectations([3.0], [3.0])
        assert out["anchored"] is False

    async def test_empty_raises(self):
        with pytest.raises(Unavailable, match="missing"):
            await inflation_expectations([], [2.5])
        with pytest.raises(Unavailable, match="missing"):
            await inflation_expectations([2.5], [])


class TestLaborMarketTightness:
    async def test_tight_market(self):
        # (1/4.0) * (250000/100000) = 0.25 * 2.5 = 0.625 -> tight/high
        out = await labor_market_tightness(4.0, 250000.0)
        assert out["score"] == pytest.approx(0.625)
        assert out["assessment"] == "tight"
        assert out["wage_pressure"] == "high"

    async def test_loose_market(self):
        # (1/5.0) * (5000/100000) = 0.2 * 0.05 = 0.01 -> loose/low
        out = await labor_market_tightness(5.0, 5000.0)
        assert out["score"] == pytest.approx(0.01)
        assert out["assessment"] == "loose"
        assert out["wage_pressure"] == "low"

    async def test_zero_unemployment_raises(self):
        with pytest.raises(Unavailable, match="non-positive"):
            await labor_market_tightness(0.0, 100000.0)


class TestPeriodChangePercent:
    def test_known_change(self):
        assert period_change_percent(110.0, 100.0) == pytest.approx(10.0)
        assert period_change_percent(90.0, 100.0) == pytest.approx(-10.0)

    def test_zero_previous_raises(self):
        with pytest.raises(Unavailable, match="zero"):
            period_change_percent(5.0, 0.0)


class TestHistoricalContext:
    def test_known_stats(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        out = historical_context(5.0, vals)
        assert out["mean"] == pytest.approx(3.0)
        assert out["min"] == 1.0
        assert out["max"] == 5.0
        assert out["std"] == pytest.approx(np.std(vals))
        assert out["percentile"] == pytest.approx(100.0)

    def test_empty_raises(self):
        with pytest.raises(Unavailable, match="no values"):
            historical_context(1.0, [])


class TestAssessScenarioImpact:
    def test_average_and_peak_impact_per_variable(self):
        baseline = {"gdp": {"q1": 2.0, "q2": 2.0, "q3": 2.0}}
        scenario = {"gdp": {"q1": 1.0, "q2": 0.0, "q3": -1.0}}
        out = assess_scenario_impact(baseline, scenario, ["gdp"])
        # average: mean([1,0,-1]) - mean([2,2,2]) = 0 - 2 = -2
        assert out["gdp"]["average_impact"] == pytest.approx(-2.0)
        # peak: max(1-2, 0-2, -1-2) = max(-1,-2,-3) = -1
        assert out["gdp"]["peak_impact"] == pytest.approx(-1.0)

    def test_missing_variable_is_skipped_not_raised(self):
        out = assess_scenario_impact({"a": {"m": 1.0}}, {"a": {"m": 2.0}}, ["a", "b"])
        assert set(out) == {"a"}

    def test_empty_forecast_series_raises(self):
        with pytest.raises(Unavailable, match="empty forecast series"):
            assess_scenario_impact({"x": {}}, {"x": {}}, ["x"])
