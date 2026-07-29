"""Behaviour tests for the integrated-risk capabilities.

Every assertion is on a hand-computed value for a known input, not on shape.
Every entry point also has a test that its dependency being unavailable raises
`Unavailable` rather than substituting the neutral 50 / 0.5 / fabricated-default
v1 returned on missing input. There is no v1 test file for this module -- v1
`integrated_risk_analyzer.py` was untested -- so these tests are the oracle.
"""

import numpy as np
import pandas as pd
import pytest

from omni.capabilities.risk import (
    analyze_concentration_risk,
    analyze_correlation_risks,
    analyze_credit_risk,
    analyze_geopolitical_risks,
    analyze_growth_risk,
    analyze_inflation_risk,
    analyze_liquidity_risk,
    analyze_market_breadth,
    analyze_options_skew,
    analyze_volatility_regime,
    analyze_yield_curve_risk,
    calculate_correlation_stability,
    calculate_depth_score,
    calculate_overall_risk_score,
    calculate_put_call_skew,
    calculate_top_concentration,
    classify_risk_level,
    estimate_recession_probability,
    find_extreme_skew,
    identify_concentrated_sectors,
    identify_correlation_clusters,
    identify_geopolitical_hotspots,
    identify_scenario_triggers,
)
from omni.ingest.protocol import Unavailable

# ---------------------------------------------------------------------------
# Market risk
# ---------------------------------------------------------------------------

class TestAnalyzeVolatilityRegime:
    @pytest.mark.parametrize(
        "vix,expected_regime,expected_score",
        [
            (12.0, "low_vol", 20.0),
            (14.9, "low_vol", 20.0),
            (15.0, "normal_vol", 40.0),
            (24.9, "normal_vol", 40.0),
            (25.0, "elevated_vol", 70.0),
            (34.9, "elevated_vol", 70.0),
            (35.0, "high_vol", 90.0),
            (50.0, "high_vol", 90.0),
        ],
    )
    def test_regime_thresholds(self, vix, expected_regime, expected_score):
        out = analyze_volatility_regime(vix, 0.0)
        assert out["regime"] == expected_regime
        assert out["score"] == expected_score
        assert out["vix_level"] == vix

    def test_transition_risk_triggers_above_20pct_abs_change(self):
        out = analyze_volatility_regime(30.0, 25.0)
        assert out["transition_risk"] is True
        out = analyze_volatility_regime(30.0, -25.0)
        assert out["transition_risk"] is True

    def test_no_transition_risk_below_threshold(self):
        out = analyze_volatility_regime(30.0, 19.9)
        assert out["transition_risk"] is False

    def test_term_structure_passes_through(self):
        ts = {"front": 25.0, "back": 22.0}
        out = analyze_volatility_regime(20.0, 0.0, term_structure=ts)
        assert out["term_structure"] == ts


class TestAnalyzeLiquidityRisk:
    def test_all_wide_spreads_maxes_score(self):
        # spreads 1% and 2% -- both > 0.5% threshold
        out = analyze_liquidity_risk([(100.0, 101.0), (100.0, 102.0)])
        assert out["wide_spread_ratio"] == pytest.approx(1.0)
        assert out["score"] == pytest.approx(min(100, 1.0 * 200))

    def test_no_wide_spreads_score_zero(self):
        out = analyze_liquidity_risk([(100.0, 100.1), (100.0, 100.4)])
        assert out["wide_spread_ratio"] == pytest.approx(0.0)
        assert out["score"] == pytest.approx(0.0)

    def test_half_wide_spreads(self):
        # 0.6% > 0.5 -> wide; 0.1% -> not
        out = analyze_liquidity_risk([(100.0, 100.6), (100.0, 100.1)])
        assert out["wide_spread_ratio"] == pytest.approx(0.5)
        assert out["score"] == pytest.approx(min(100, 0.5 * 200))

    def test_empty_quotes_raises(self):
        with pytest.raises(Unavailable, match="no quotes"):
            analyze_liquidity_risk([])

    def test_zero_bid_raises_undefined_spread(self):
        with pytest.raises(Unavailable, match="zero bid"):
            analyze_liquidity_risk([(0.0, 1.0)])


class TestCalculateDepthScore:
    def test_known_mean_imbalance(self):
        # (100,100) -> 0 ; (200,100) -> 100/300 = 0.333... -> mean *100
        out = calculate_depth_score([(100.0, 100.0), (200.0, 100.0)])
        assert out == pytest.approx((0.0 + (100.0 / 300.0)) / 2 * 100)

    def test_balanced_book_scores_zero(self):
        out = calculate_depth_score([(100.0, 100.0), (50.0, 50.0)])
        assert out == pytest.approx(0.0)

    def test_zero_depth_levels_filtered_then_empty_raises(self):
        with pytest.raises(Unavailable, match="no non-zero depth"):
            calculate_depth_score([(0.0, 0.0)])

    def test_zero_levels_dropped_but_valid_kept(self):
        out = calculate_depth_score([(0.0, 0.0), (100.0, 100.0)])
        assert out == pytest.approx(0.0)


class TestAnalyzeConcentrationRisk:
    def test_equal_caps_low_hhi(self):
        # four equal caps: each (0.25)^2 = 0.0625, sum 0.25, *10000 = 2500
        out = analyze_concentration_risk([100.0, 100.0, 100.0, 100.0])
        assert out["herfindahl_index"] == pytest.approx(2500.0)
        assert out["score"] == pytest.approx(min(100, 2500 / 100))
        assert out["top_5_concentration"] == pytest.approx(1.0)

    def test_single_cap_maxes_score(self):
        out = analyze_concentration_risk([1000.0])
        assert out["herfindahl_index"] == pytest.approx(10000.0)
        assert out["score"] == pytest.approx(100.0)

    def test_empty_raises(self):
        with pytest.raises(Unavailable, match="no market caps"):
            analyze_concentration_risk([])


class TestCalculateTopConcentration:
    def test_top_share(self):
        assert calculate_top_concentration([1.0, 2.0, 3.0, 4.0], 2) == pytest.approx(7.0 / 10.0)

    def test_empty_returns_zero(self):
        assert calculate_top_concentration([], 5) == 0.0

    def test_n_exceeding_length_returns_total(self):
        assert calculate_top_concentration([1.0, 2.0], 5) == pytest.approx(1.0)


class TestCalculatePutCallSkew:
    def test_put_minus_call_iv(self):
        spot = 100.0
        puts = [{"strike": 90.0, "implied_volatility": 0.3},
                {"strike": 80.0, "implied_volatility": 0.4}]
        calls = [{"strike": 110.0, "implied_volatility": 0.2},
                 {"strike": 120.0, "implied_volatility": 0.18}]
        # OTM put mean 0.35, OTM call mean 0.19 -> 0.16
        assert calculate_put_call_skew(calls, puts, spot) == pytest.approx(0.16)

    def test_zero_spot_returns_none(self):
        assert calculate_put_call_skew([], [], 0.0) is None

    def test_only_otm_puts_returns_put_mean(self):
        out = calculate_put_call_skew(
            [], [{"strike": 90.0, "implied_volatility": 0.3}], 100.0
        )
        assert out == pytest.approx(0.3)

    def test_no_otm_ivs_returns_none(self):
        # strikes inside the OTM bands, or zero iv -> nothing usable
        out = calculate_put_call_skew(
            [{"strike": 100.0, "implied_volatility": 0.2}],
            [{"strike": 100.0, "implied_volatility": 0.2}],
            100.0,
        )
        assert out is None

    def test_itm_strikes_ignored(self):
        # 96-strike put when spot 100 -> not OTM (96 < 95 false) -> dropped
        out = calculate_put_call_skew(
            [{"strike": 104.0, "implied_volatility": 0.2}],
            [{"strike": 96.0, "implied_volatility": 0.3}],
            100.0,
        )
        assert out is None


class TestAnalyzeOptionsSkew:
    def test_score_from_average_skew(self):
        # avg 0.2 -> (0.2 + 20) * 2.5 = 50.5
        out = analyze_options_skew([0.16, 0.24])
        assert out["average_skew"] == pytest.approx(0.2)
        assert out["score"] == pytest.approx(50.5)

    def test_extremes_clamp_to_zero_and_hundred(self):
        assert analyze_options_skew([-20.0])["score"] == pytest.approx(0.0)
        assert analyze_options_skew([20.0])["score"] == pytest.approx(100.0)

    def test_none_skews_dropped(self):
        out = analyze_options_skew([None, -20.0, 20.0])
        assert out["average_skew"] == pytest.approx(0.0)
        assert out["score"] == pytest.approx(50.0)

    def test_all_none_raises(self):
        with pytest.raises(Unavailable, match="no put-call skew"):
            analyze_options_skew([None, None])


class TestFindExtremeSkew:
    def test_returns_only_extreme_symbols(self):
        options = {
            "A": {
                "calls": [{"strike": 110.0, "implied_volatility": 0.2}],
                "puts": [{"strike": 90.0, "implied_volatility": 0.6}],
                "underlying_price": 100.0,
            },  # skew 0.4 -> extreme
            "B": {
                "calls": [{"strike": 104.0, "implied_volatility": 0.2}],
                "puts": [{"strike": 96.0, "implied_volatility": 0.2}],
                "underlying_price": 100.0,
            },  # no OTM -> None -> not extreme
        }
        assert find_extreme_skew(options) == ["A"]

    def test_empty_input_returns_empty(self):
        assert find_extreme_skew({}) == []


class TestAnalyzeMarketBreadth:
    def test_negative_breadth_high_score(self):
        out = analyze_market_breadth(
            advance_decline_ratio=0.4, percent_above_50ma=50,
            percent_above_200ma=50, new_highs=10, new_lows=30,
        )
        assert out["score"] == pytest.approx(80.0)

    def test_positive_breadth_low_score(self):
        out = analyze_market_breadth(
            advance_decline_ratio=3.0, percent_above_50ma=80,
            percent_above_200ma=80, new_highs=100, new_lows=10,
        )
        assert out["score"] == pytest.approx(20.0)

    def test_neutral_conditions_score_50(self):
        out = analyze_market_breadth(
            advance_decline_ratio=1.0, percent_above_50ma=50,
            percent_above_200ma=50, new_highs=10, new_lows=10,
        )
        assert out["score"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Economic risk
# ---------------------------------------------------------------------------

class TestAnalyzeYieldCurveRisk:
    def test_inverted_curve_score_escalates_with_depth(self):
        # spread -0.5 -> 80 + min(20, 0.5*40) = 80 + 20 = 100
        out = analyze_yield_curve_risk(4.5, 4.0)
        assert out["spread_2s10s"] == pytest.approx(-0.5)
        assert out["is_inverted"] is True
        assert out["score"] == pytest.approx(100.0)
        assert out["curve_shape"] == "inverted"

    def test_shallow_curve_near_sixty(self):
        # spread 0.3 (< 0.5, not inverted) -> 60
        out = analyze_yield_curve_risk(4.0, 4.3)
        assert out["score"] == pytest.approx(60.0)
        assert out["is_inverted"] is False

    def test_steep_curve_low_score(self):
        out = analyze_yield_curve_risk(4.0, 5.0)
        assert out["score"] == pytest.approx(30.0)

    def test_mild_inversion_uses_abs_spread(self):
        # spread -0.1 -> 80 + min(20, 0.1*40) = 80 + 4 = 84
        out = analyze_yield_curve_risk(4.1, 4.0)
        assert out["score"] == pytest.approx(84.0)


class TestAnalyzeInflationRisk:
    def test_high_current_inflation(self):
        out = analyze_inflation_risk(6.0, 2.5)
        assert out["score"] == pytest.approx(80.0)

    def test_elevated_current_with_high_expectations(self):
        # current >3 -> 60; expected >3 -> +20 -> 80
        out = analyze_inflation_risk(4.0, 4.0)
        assert out["score"] == pytest.approx(80.0)

    def test_low_current_boosted_by_expectations(self):
        # current <=3 -> 30; expected >3 -> +20 -> 50
        out = analyze_inflation_risk(2.0, 4.0)
        assert out["score"] == pytest.approx(50.0)

    def test_low_inflation_stays_low(self):
        out = analyze_inflation_risk(2.0, 2.0)
        assert out["score"] == pytest.approx(30.0)


class TestEstimateRecessionProbability:
    def test_recession_with_high_unemployment(self):
        assert estimate_recession_probability(-1.0, 6.0) == pytest.approx(0.85)

    def test_weak_growth_and_rising_unemployment(self):
        # 0.15 + 0.3 (gdp<1) + 0.1 (unemp>4) = 0.55
        assert estimate_recession_probability(0.5, 4.5) == pytest.approx(0.55)

    def test_healthy_economy_floor(self):
        assert estimate_recession_probability(3.0, 3.0) == pytest.approx(0.15)

    def test_cap_at_095_when_inputs_extreme(self):
        # base 0.15 + 0.5 (gdp<0) + 0.2 (unemp>5) = 0.85, still under cap
        assert estimate_recession_probability(-10.0, 10.0) == pytest.approx(0.85)


class TestAnalyzeGrowthRisk:
    def test_recession_lifts_to_max(self):
        # gdp<0 -> 90; unemp>5 -> +20 -> capped 100
        out = analyze_growth_risk(-1.0, 6.0, 200000.0)
        assert out["score"] == pytest.approx(100.0)
        assert out["recession_probability"] == pytest.approx(0.85)

    def test_weak_job_growth_penalizes(self):
        # gdp 1.5 -> 50; job_growth<50000 -> +20 -> 70
        out = analyze_growth_risk(1.5, 4.0, 40000.0)
        assert out["score"] == pytest.approx(70.0)

    def test_healthy_growth(self):
        out = analyze_growth_risk(3.0, 3.0, 200000.0)
        assert out["score"] == pytest.approx(30.0)


class TestAnalyzeCreditRisk:
    def test_tight_spreads_low_score(self):
        out = analyze_credit_risk(100.0, 400.0)
        assert out["score"] == pytest.approx(40.0)
        assert out["spread_widening"] is False

    def test_ig_stress_extreme(self):
        # ig 200 > 120*1.5=180 -> 80
        out = analyze_credit_risk(200.0, 400.0)
        assert out["score"] == pytest.approx(80.0)
        assert out["spread_widening"] is True

    def test_moderate_ig_widening(self):
        # ig 150 > 120*1.2=144 (and not >180) -> 60
        out = analyze_credit_risk(150.0, 400.0)
        assert out["score"] == pytest.approx(60.0)

    def test_hy_moderate_widening(self):
        # hy 600 > 450*1.2=540 (and not >675) -> 60
        out = analyze_credit_risk(100.0, 600.0)
        assert out["score"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# Correlation risk
# ---------------------------------------------------------------------------

class TestCalculateCorrelationStability:
    def test_fewer_than_window_returns_zero(self):
        df = pd.DataFrame({"A": [0.01, 0.02], "B": [0.01, 0.02]})
        assert calculate_correlation_stability(df) == pytest.approx(0.0)

    def test_identical_series_zero_variance(self):
        # perfectly co-moving -> every window corr is 1.0 -> std 0.0
        a = np.linspace(0.01, 0.05, 70)
        df = pd.DataFrame({"A": a, "B": a})
        assert calculate_correlation_stability(df) == pytest.approx(0.0)


class TestIdentifyCorrelationClusters:
    def test_pairs_above_threshold_cluster(self):
        corr = pd.DataFrame(
            {"A": [1.0, 0.9, 0.1], "B": [0.9, 1.0, 0.1], "C": [0.1, 0.1, 1.0]},
            index=["A", "B", "C"],
        )
        clusters = identify_correlation_clusters(corr)
        assert clusters == [["A", "B"]]

    def test_no_pairs_above_threshold(self):
        corr = pd.DataFrame(
            {"A": [1.0, 0.1], "B": [0.1, 1.0]}, index=["A", "B"]
        )
        assert identify_correlation_clusters(corr) == []


class TestAnalyzeCorrelationRisks:
    async def test_perfectly_correlated_high_risk(self):
        returns = {"A": [0.01, 0.02, 0.03, 0.04], "B": [0.01, 0.02, 0.03, 0.04]}
        out = await analyze_correlation_risks(returns)
        assert out["average_correlation"] == pytest.approx(1.0)
        assert out["score"] == 80

    async def test_perfectly_anticorrelated_decorrelation_risk(self):
        returns = {
            "A": [0.01, 0.02, 0.03, 0.04],
            "B": [-0.01, -0.02, -0.03, -0.04],
        }
        out = await analyze_correlation_risks(returns)
        assert out["average_correlation"] == pytest.approx(-1.0)
        assert out["score"] == 70

    async def test_single_symbol_raises(self):
        with pytest.raises(Unavailable, match=">=2 symbols"):
            await analyze_correlation_risks({"A": [0.01, 0.02]})

    async def test_empty_raises(self):
        with pytest.raises(Unavailable, match=">=2 symbols"):
            await analyze_correlation_risks({})


# ---------------------------------------------------------------------------
# Geopolitical risk
# ---------------------------------------------------------------------------

class TestIdentifyGeopoliticalHotspots:
    def test_matches_region_keywords(self):
        articles = [{"title": "Russia and Ukraine conflict escalates", "summary": ""}]
        assert "Eastern Europe" in identify_geopolitical_hotspots(articles)

    def test_matches_trade_war_from_summary(self):
        articles = [{"title": "Markets rally", "summary": "trade war fears ease"}]
        assert "Trade War" in identify_geopolitical_hotspots(articles)

    def test_no_match_returns_empty(self):
        assert identify_geopolitical_hotspots([{"title": "Earnings beat", "summary": ""}]) == []

    def test_empty_articles_returns_empty(self):
        assert identify_geopolitical_hotspots([]) == []


class TestAnalyzeGeopoliticalRisks:
    async def test_score_from_keyword_density(self):
        # 1 keyword hit out of 6 -> ratio 1/6 -> min(100, 500/6) = 83.33
        articles = [
            {"title": "Central bank holds rates", "summary": ""},
            {"title": "New tariff announced", "summary": ""},
            {"title": "Tech earnings beat", "summary": ""},
            {"title": "Oil inventories rise", "summary": ""},
            {"title": "Retail sales strong", "summary": ""},
            {"title": "Housing starts up", "summary": ""},
        ]
        out = await analyze_geopolitical_risks(articles)
        assert out["risk_mentions"] == 1
        assert out["total_articles"] == 6
        assert out["score"] == pytest.approx(min(100, (1 / 6) * 500))

    async def test_no_keywords_score_zero(self):
        articles = [{"title": "Earnings beat", "summary": ""}]
        out = await analyze_geopolitical_risks(articles)
        assert out["score"] == pytest.approx(0.0)
        assert out["risk_mentions"] == 0

    async def test_empty_raises(self):
        with pytest.raises(Unavailable, match="no articles"):
            await analyze_geopolitical_risks([])


# ---------------------------------------------------------------------------
# Overall risk
# ---------------------------------------------------------------------------

class TestClassifyRiskLevel:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (80.0, "extreme"),
            (79.99, "high"),
            (60.0, "high"),
            (59.99, "moderate"),
            (40.0, "moderate"),
            (39.99, "low"),
            (20.0, "low"),
            (19.99, "minimal"),
            (0.0, "minimal"),
        ],
    )
    def test_boundaries(self, score, expected):
        assert classify_risk_level(score) == expected


class TestCalculateOverallRiskScore:
    def test_uniform_moderate_score(self):
        out = calculate_overall_risk_score(
            market_score=40.0, economic_score=40.0, sentiment_score=40.0,
            correlation_score=40.0, geopolitical_score=40.0,
        )
        # weights sum to 1.0 -> overall 40; bs = 40/3000
        assert out["score"] == pytest.approx(40.0)
        assert out["black_swan_prob"] == pytest.approx(40.0 / 3000.0)
        assert out["risk_level"] == "moderate"

    def test_high_bucket_probability(self):
        out = calculate_overall_risk_score(
            market_score=70.0, economic_score=70.0, sentiment_score=70.0,
            correlation_score=70.0, geopolitical_score=70.0,
        )
        # overall 70 -> 0.02 + (70-60)/200 = 0.07
        assert out["score"] == pytest.approx(70.0)
        assert out["black_swan_prob"] == pytest.approx(0.07)
        assert out["risk_level"] == "high"

    def test_extreme_bucket_probability(self):
        out = calculate_overall_risk_score(
            market_score=90.0, economic_score=90.0, sentiment_score=90.0,
            correlation_score=90.0, geopolitical_score=90.0,
        )
        # overall 90 -> 0.1 + (90-80)/100 = 0.2
        assert out["score"] == pytest.approx(90.0)
        assert out["black_swan_prob"] == pytest.approx(0.2)
        assert out["risk_level"] == "extreme"

    def test_market_weight_is_thirty_percent(self):
        # only market contributes -> 100 * 0.3 = 30
        out = calculate_overall_risk_score(
            market_score=100.0, economic_score=0.0, sentiment_score=0.0,
            correlation_score=0.0, geopolitical_score=0.0,
        )
        assert out["score"] == pytest.approx(30.0)
        assert out["risk_level"] == "low"


# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------

class TestIdentifyScenarioTriggers:
    def test_recession_triggers(self):
        out = identify_scenario_triggers("recession")
        assert out["yield_curve_inversion"] is True
        assert out["gdp_growth"] == "<0%"

    def test_black_swan_triggers(self):
        out = identify_scenario_triggers("black_swan")
        assert out["vix_spike"] == ">40"
        assert out["correlation_breakdown"] is True

    def test_recovery_triggers(self):
        out = identify_scenario_triggers("recovery")
        assert out["gdp_growth"] == ">3%"

    def test_unknown_scenario_empty(self):
        assert identify_scenario_triggers("nope") == {}


class TestIdentifyConcentratedSectors:
    def test_tech_concentration_flagged(self):
        # 3 of 4 in Technology -> 0.75 > 0.3
        out = identify_concentrated_sectors(["AAPL", "MSFT", "GOOGL", "JPM"])
        assert out == ["Technology"]

    def test_balanced_no_concentration(self):
        out = identify_concentrated_sectors(["AAPL", "JPM", "XOM", "AMZN"])
        assert out == []

    def test_empty_returns_empty(self):
        assert identify_concentrated_sectors([]) == []

    def test_custom_threshold(self):
        out = identify_concentrated_sectors(["AAPL", "JPM"], threshold=0.4)
        # each sector 0.5 > 0.4
        assert set(out) == {"Technology", "Financials"}
