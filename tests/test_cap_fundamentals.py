"""Behaviour tests for the fundamentals/portfolio capabilities.

Every assertion is on a hand-computed value for a known input, not on shape.
Every entry point also has a test that its dependency being unavailable raises
`Unavailable` (or, for a genuine math-impossibility, `ValueError`) rather than
substituting the zero / 0.5 / 1.0 default v1 returned.
"""

from typing import Any, ClassVar

import pytest

from omni.capabilities.fundamentals import (
    benchmark_comparison,
    blend_position_returns,
    correlation_matrix,
    dcf_valuation,
    financial_ratios,
    max_drawdown,
    peer_comparison,
    portfolio_returns,
    ratio_quality_scores,
    risk_metrics,
    stress_tests,
)
from omni.ingest.protocol import Unavailable

FUNDAMENTALS = {
    "income_statement": {
        "eps": 10.0,
        "earnings_growth_rate": 0.20,
        "net_income": 1_000_000,
        "revenue": 5_000_000,
        "cost_of_revenue": 2_000_000,
        "operating_income": 1_500_000,
        "dividends_per_share": 2.0,
    },
    "balance_sheet": {
        "book_value_per_share": 50.0,
        "total_equity": 4_000_000,
        "total_assets": 10_000_000,
        "total_debt": 2_000_000,
        "current_assets": 3_000_000,
        "current_liabilities": 1_500_000,
        "inventory": 500_000,
    },
    "cash_flow": {
        "operating_cash_flow": 1_200_000,
        "capital_expenditures": 200_000,
    },
}

CURRENT_PRICE = 100.0


class TestFinancialRatios:
    async def test_each_ratio_is_the_exact_known_value(self):
        out = await financial_ratios(FUNDAMENTALS, CURRENT_PRICE)

        assert out["pe_ratio"] == pytest.approx(10.0)
        assert out["earnings_per_share"] == 10.0
        assert out["peg_ratio"] == pytest.approx(0.5)
        assert out["pb_ratio"] == pytest.approx(2.0)
        assert out["book_value_per_share"] == 50.0
        assert out["roe"] == pytest.approx(25.0)
        assert out["roa"] == pytest.approx(10.0)
        assert out["debt_to_equity"] == pytest.approx(0.5)
        assert out["current_ratio"] == pytest.approx(2.0)
        assert out["quick_ratio"] == pytest.approx(2_500_000 / 1_500_000)
        assert out["gross_margin"] == pytest.approx(60.0)
        assert out["operating_margin"] == pytest.approx(30.0)
        assert out["net_margin"] == pytest.approx(20.0)
        assert out["free_cash_flow"] == 1_000_000
        assert out["dividend_yield"] == pytest.approx(2.0)
        assert out["current_price"] == CURRENT_PRICE

    async def test_missing_fundamentals_raises_rather_than_returning_an_error_dict(self):
        with pytest.raises(Unavailable, match="no fundamental data"):
            await financial_ratios({}, CURRENT_PRICE)
        with pytest.raises(Unavailable, match="no fundamental data"):
            await financial_ratios(None, CURRENT_PRICE)

    async def test_a_division_by_zero_denominator_is_null_not_a_crash(self):
        fundamentals = {
            "income_statement": {"eps": 0.0},
            "balance_sheet": {
                "total_equity": 0,
                "current_liabilities": 0,
                "revenue": 0,
            },
            "cash_flow": {},
        }
        out = await financial_ratios(fundamentals, CURRENT_PRICE)
        assert out["pe_ratio"] is None
        assert out["roe"] is None
        assert out["current_ratio"] is None
        assert out["gross_margin"] is None


class TestRatioQualityScores:
    def test_score_strings_are_the_known_buckets(self):
        scores = ratio_quality_scores(
            {
                "pe_ratio": 10.0,
                "peg_ratio": 0.5,
                "roe": 25.0,
                "debt_to_equity": 0.5,
                "current_ratio": 2.0,
            }
        )
        assert scores["pe_score"] == "undervalued"
        assert scores["peg_score"] == "undervalued"
        assert scores["roe_score"] == "good"
        assert scores["debt_score"] == "moderate_debt"
        assert scores["liquidity_score"] == "good"

    def test_null_metrics_produce_no_score_rather_than_a_guess(self):
        scores = ratio_quality_scores(
            {"pe_ratio": None, "peg_ratio": None, "roe": None}
        )
        assert scores == {}

    def test_extremes_land_in_the_outermost_buckets(self):
        scores = ratio_quality_scores(
            {
                "pe_ratio": 50.0,
                "peg_ratio": 5.0,
                "roe": -3.0,
                "debt_to_equity": 3.0,
                "current_ratio": 0.5,
            }
        )
        assert scores["pe_score"] == "highly_overvalued"
        assert scores["peg_score"] == "overvalued"
        assert scores["roe_score"] == "negative"
        assert scores["debt_score"] == "very_high_debt"
        assert scores["liquidity_score"] == "poor"


class TestDcfValuation:
    def _fixture(self) -> dict:
        return {
            "income_statement": {
                **FUNDAMENTALS["income_statement"],
                "revenue_growth_rate": 0.20,
            },
            "balance_sheet": {
                **FUNDAMENTALS["balance_sheet"],
                "market_cap": 8_000_000,
                "cash_and_equivalents": 500_000,
                "shares_outstanding": 100_000,
            },
            "cash_flow": FUNDAMENTALS["cash_flow"],
            "beta": 1.2,
        }

    async def test_growth_discount_and_projection_are_the_known_values(self):
        out = await dcf_valuation(self._fixture(), CURRENT_PRICE)

        assert out["assumptions"]["growth_rate"] == pytest.approx(0.20)
        # WACC: 0.8 * (0.04 + 1.2*0.08) + 0.2 * 0.04 * (1 - 0.21)
        expected_discount = 0.8 * 0.136 + 0.2 * 0.04 * 0.79
        assert out["assumptions"]["discount_rate"] == pytest.approx(expected_discount)
        assert out["assumptions"]["terminal_growth_rate"] == 0.03
        assert out["assumptions"]["projection_years"] == 5

        assert len(out["projected_cash_flows"]) == 5
        assert out["projected_cash_flows"][0]["cash_flow"] == pytest.approx(1_200_000.0)
        assert out["projected_cash_flows"][4]["cash_flow"] == pytest.approx(2_488_320.0)
        assert out["projected_cash_flows"][0]["present_value"] == pytest.approx(
            1_200_000.0 / (1 + expected_discount)
        )

    async def test_equity_bridge_and_per_share_value_are_internally_consistent(self):
        out = await dcf_valuation(self._fixture(), CURRENT_PRICE)

        shares = 100_000
        cash = 500_000
        debt = 2_000_000
        expected_equity = (
            out["sum_of_present_values"] + out["terminal_present_value"] + cash - debt
        )
        assert out["equity_value"] == pytest.approx(expected_equity)
        assert out["fair_value_per_share"] == pytest.approx(expected_equity / shares)
        assert out["upside_percentage"] == pytest.approx(
            (out["fair_value_per_share"] - CURRENT_PRICE) / CURRENT_PRICE * 100
        )

        terminal_fcf = out["projected_cash_flows"][-1]["cash_flow"] * 1.03
        assert out["terminal_value"] == pytest.approx(
            terminal_fcf / (out["assumptions"]["discount_rate"] - 0.03)
        )

    async def test_missing_fundamentals_raises(self):
        with pytest.raises(Unavailable, match="no fundamental data"):
            await dcf_valuation({}, CURRENT_PRICE)

    async def test_non_positive_free_cash_flow_raises_value_error(self):
        fixture = self._fixture()
        fixture["cash_flow"] = {"operating_cash_flow": 100, "capital_expenditures": 1000}
        with pytest.raises(ValueError, match="non-positive free cash flow"):
            await dcf_valuation(fixture, CURRENT_PRICE)

    async def test_missing_growth_rate_raises_rather_than_defaulting_to_five_percent(self):
        fixture = self._fixture()
        fixture["income_statement"] = {
            k: v for k, v in fixture["income_statement"].items()
            if k != "revenue_growth_rate"
        }
        with pytest.raises(Unavailable, match="no growth_rate"):
            await dcf_valuation(fixture, CURRENT_PRICE, growth_rate=None)

    async def test_explicit_growth_and_discount_override_derivation(self):
        out = await dcf_valuation(
            self._fixture(),
            CURRENT_PRICE,
            growth_rate=0.10,
            discount_rate=0.12,
            years=3,
        )
        assert out["assumptions"]["growth_rate"] == 0.10
        assert out["assumptions"]["discount_rate"] == 0.12
        assert len(out["projected_cash_flows"]) == 3


class TestPeerComparison:
    COMPARISON: ClassVar[list[dict[str, Any]]] = [
        {
            "symbol": "TGT", "is_target": True, "current_price": 100.0,
            "pe_ratio": 10.0, "peg_ratio": 0.5, "pb_ratio": 2.0, "roe": 25.0,
            "debt_to_equity": 0.5, "current_ratio": 2.0, "gross_margin": 60.0,
            "net_margin": 20.0, "dividend_yield": 2.0,
        },
        {
            "symbol": "P1", "is_target": False, "current_price": 50.0,
            "pe_ratio": 20.0, "peg_ratio": 1.5, "pb_ratio": 4.0, "roe": 10.0,
            "debt_to_equity": 1.0, "current_ratio": 1.0, "gross_margin": 30.0,
            "net_margin": 10.0, "dividend_yield": 1.0,
        },
        {
            "symbol": "P2", "is_target": False, "current_price": 25.0,
            "pe_ratio": 40.0, "peg_ratio": 3.0, "pb_ratio": 8.0, "roe": 5.0,
            "debt_to_equity": 2.0, "current_ratio": 0.5, "gross_margin": 15.0,
            "net_margin": 5.0, "dividend_yield": 0.0,
        },
    ]

    async def test_averages_and_target_rankings_are_known(self):
        out = await peer_comparison("TGT", "Tech", "Technology", self.COMPARISON)

        assert out["peer_count"] == 2
        assert out["peer_averages"]["pe_ratio"] == pytest.approx(70.0 / 3)
        assert out["peer_averages"]["gross_margin"] == pytest.approx(35.0)
        assert out["peer_averages"]["dividend_yield"] == pytest.approx(1.0)

        # TGT is best (rank 1, percentile 100) on every metric.
        for metric in [
            "pe_ratio", "peg_ratio", "pb_ratio", "roe", "debt_to_equity",
            "current_ratio", "gross_margin", "net_margin", "dividend_yield",
        ]:
            assert out["rankings"][metric]["rank"] == 1
            assert out["rankings"][metric]["total"] == 3
            assert out["rankings"][metric]["percentile"] == pytest.approx(100.0)

    async def test_relative_valuation_uses_target_over_peer_average(self):
        out = await peer_comparison("TGT", "Tech", "Technology", self.COMPARISON)
        assert out["relative_valuation"]["pe_ratio_vs_peers"] == pytest.approx(
            (10.0 / (70.0 / 3) - 1) * 100
        )
        assert out["relative_valuation"]["pb_ratio_vs_peers"] == pytest.approx(
            (2.0 / (14.0 / 3) - 1) * 100
        )
        assert out["relative_valuation"]["peg_ratio_vs_peers"] == pytest.approx(
            (0.5 / (5.0 / 3) - 1) * 100
        )

    async def test_fewer_than_two_entries_raises(self):
        with pytest.raises(Unavailable, match="insufficient peer data"):
            await peer_comparison("TGT", "Tech", "Technology", self.COMPARISON[:1])


class TestMaxDrawdown:
    def test_known_trough_returns_minus_twenty(self):
        # cumprod: 1.1 -> 0.88 -> 0.968 ; running max 1.1 ; trough -20%
        assert max_drawdown([0.1, -0.2, 0.1]) == pytest.approx(-20.0)

    def test_monotone_rise_has_zero_drawdown(self):
        assert max_drawdown([0.01, 0.02, 0.03]) == pytest.approx(0.0)

    def test_empty_series_is_zero(self):
        assert max_drawdown([]) == 0.0


class TestBlendPositionReturns:
    def test_weighted_blend_of_two_known_series(self):
        position_returns = {
            "A": {"returns": [0.01, 0.02, 0.03], "weight": 0.5},
            "B": {"returns": [0.02, 0.04, 0.06], "weight": 0.5},
        }
        assert blend_position_returns(position_returns) == [
            pytest.approx(0.015),
            pytest.approx(0.03),
            pytest.approx(0.045),
        ]

    def test_caps_at_252_periods(self):
        position_returns = {
            "A": {"returns": [0.0] * 300, "weight": 1.0},
        }
        assert len(blend_position_returns(position_returns)) == 252

    def test_empty_input_is_empty(self):
        assert blend_position_returns({}) == []


class TestPortfolioReturns:
    TRANSACTIONS: ClassVar[list[dict[str, Any]]] = [
        {"transaction_type": "buy", "total_amount": 10_000},
        {"transaction_type": "buy", "total_amount": 5_000},
        {"transaction_type": "sell", "total_amount": 2_000},
    ]

    async def test_returns_on_known_cash_flows(self):
        # initial_value = 10000 + 5000 - 2000 = 13000; total = 15000
        out = await portfolio_returns(
            self.TRANSACTIONS,
            total_value=15_000,
            daily_returns=[0.01] * 4,
            period_days=365,
            risk_free_rate_pct=2.0,
        )
        assert out["absolute_return"] == pytest.approx(2000.0)
        # function rounds to 2 dp (faithful to v1): 2000/13000*100 = 15.3846... -> 15.38
        assert out["percentage_return"] == 15.38
        # flat returns -> np.std of a bit-identical series is exactly 0.0.
        assert out["volatility"] == 0.0
        # Sharpe is undefined for a zero-variance series (x/0). Returning a
        # number here would be a fabrication; the honest result is None.
        assert out["sharpe_ratio"] is None
        assert out["max_drawdown"] == 0.0

    async def test_annualization_uses_period_days(self):
        out = await portfolio_returns(
            self.TRANSACTIONS,
            total_value=15_000,
            daily_returns=[0.01] * 4,
            period_days=365,
            risk_free_rate_pct=2.0,
        )
        years = 365 / 365.25
        expected = (pow(1 + (2000.0 / 13000.0), 1 / years) - 1) * 100
        assert out["annualized_return"] == pytest.approx(expected, abs=0.01)

    async def test_sharpe_subtracts_the_supplied_risk_free_rate(self):
        # daily_returns with genuine variance: std([.01,-.01,.01,-.01]) == 0.01.
        varying = [0.01, -0.01, 0.01, -0.01]
        ann = (pow(1 + (2000.0 / 13000.0), 1 / (365 / 365.25)) - 1) * 100
        vol = 0.01 * (252 ** 0.5) * 100
        out_zero = await portfolio_returns(
            self.TRANSACTIONS, total_value=15_000, daily_returns=varying,
            period_days=365, risk_free_rate_pct=0.0,
        )
        out_two = await portfolio_returns(
            self.TRANSACTIONS, total_value=15_000, daily_returns=varying,
            period_days=365, risk_free_rate_pct=2.0,
        )
        assert out_zero["sharpe_ratio"] == pytest.approx(ann / vol, abs=0.01)
        assert out_two["sharpe_ratio"] == pytest.approx((ann - 2.0) / vol, abs=0.01)
        # different rf -> different sharpe (proves rf is wired, not ignored).
        assert out_zero["sharpe_ratio"] != out_two["sharpe_ratio"]

    async def test_no_transactions_raises_rather_than_returning_zeros(self):
        with pytest.raises(Unavailable, match="no transactions"):
            await portfolio_returns(
                [], 15_000, [0.01] * 4, 365, risk_free_rate_pct=2.0
            )

    async def test_single_daily_return_raises_rather_than_zero_volatility(self):
        with pytest.raises(Unavailable, match="fewer than 2 daily returns"):
            await portfolio_returns(
                self.TRANSACTIONS, 15_000, [0.01], 365, risk_free_rate_pct=2.0
            )


class TestRiskMetrics:
    SERIES = [-0.01] * 10 + [0.01] * 10  # 20 points, mean 0, std 0.01

    async def test_known_var_std_and_downside_on_balanced_series(self):
        out = await risk_metrics(
            self.SERIES, total_value=10_000, risk_free_rate_pct=4.5
        )
        assert out["value_at_risk_95"] == pytest.approx(-100.0)
        assert out["value_at_risk_99"] == pytest.approx(-100.0)
        assert out["conditional_var_95"] == pytest.approx(-100.0)
        # 0.01 * sqrt(252) * 100 = 15.8745..., rounded to 2 dp -> 15.87
        assert out["standard_deviation"] == 15.87
        # Canonical Sortino downside dev = sqrt(mean(min(r,0)**2)). Ten -0.01
        # returns give mean(0.0001*10/20) = 0.00005 -> sqrt = 0.007071 ->
        # *sqrt(252)*100 = 11.2249 -> 11.22. NOT zero: there ARE downside
        # returns, so there IS downside deviation even though they are equal --
        # the prior code (std of the negative subset about its own mean)
        # returned 0.0 here, the §6.7 defect.
        assert out["downside_deviation"] == 11.22
        # sortino = (annual_return 0 - risk_free 4.5) / 11.2249 = -0.4009 -> -0.4.
        assert out["sortino_ratio"] == -0.4
        assert out["data_quality"] == "historical"
        assert out["data_points"] == 20

    async def test_sortino_and_downside_on_spread_negatives(self):
        # Canonical Sortino downside dev = sqrt(mean(min(r,0)**2)).
        # downside = [-0.02,-0.01,0,0,0]*4 -> mean of squares = 0.002/20 = 0.0001
        # -> sqrt = 0.01 -> *sqrt(252)*100 = 15.8745 -> 15.87.
        series = [-0.02, -0.01, 0.0, 0.01, 0.02] * 4  # 20 points, mean 0
        out = await risk_metrics(series, total_value=10_000, risk_free_rate_pct=4.5)
        assert out["downside_deviation"] == 15.87
        # sortino = (annual_return 0 - risk_free 4.5) / 15.8745 = -0.2835 -> -0.28
        assert out["sortino_ratio"] == -0.28
        # annual_return is exactly 0, so calmar is 0 regardless of drawdown.
        assert out["calmar_ratio"] == 0.0

    async def test_sortino_uses_the_supplied_risk_free_rate(self):
        # annual_return is 0 on this series, so sortino = (0 - rf) / downside_dev.
        series = [-0.02, -0.01, 0.0, 0.01, 0.02] * 4
        dd = 0.01 * (252 ** 0.5) * 100  # canonical Sortino downside dev
        out_zero = await risk_metrics(series, total_value=10_000, risk_free_rate_pct=0.0)
        out_four = await risk_metrics(series, total_value=10_000, risk_free_rate_pct=4.5)
        assert out_zero["sortino_ratio"] == pytest.approx(0.0, abs=0.01)
        assert out_four["sortino_ratio"] == pytest.approx((0.0 - 4.5) / dd, abs=0.01)
        # different rf -> different sortino (proves rf is wired, not ignored).
        assert out_zero["sortino_ratio"] != out_four["sortino_ratio"]

    async def test_calmar_is_none_when_there_is_no_drawdown(self):
        # strictly rising returns -> cumulative NAV never dips -> max drawdown 0.
        # calmar (return / drawdown) is then undefined, not 0.0.
        rising = [0.001 * (i + 1) for i in range(20)]
        out = await risk_metrics(rising, total_value=10_000, risk_free_rate_pct=2.0)
        assert out["calmar_ratio"] is None
        # no negative days -> no downside -> sortino also undefined.
        assert out["sortino_ratio"] is None

    async def test_beta_is_one_when_benchmark_equals_portfolio(self):
        out = await risk_metrics(
            self.SERIES, 10_000, risk_free_rate_pct=4.5, benchmark_returns=self.SERIES
        )
        assert out["portfolio_beta"] == pytest.approx(1.0)

    async def test_beta_is_null_when_no_benchmark_rather_than_one(self):
        out = await risk_metrics(self.SERIES, 10_000, risk_free_rate_pct=4.5)
        assert out["portfolio_beta"] is None

    async def test_beta_is_null_for_a_constant_benchmark_rather_than_garbage(self):
        # a bit-identical benchmark has ~1e-34 variance from float noise; the old
        # `cov_matrix[1,1] > 0` guard passed on that noise and divided by it.
        constant_bench = [0.005] * 20
        out = await risk_metrics(
            self.SERIES, 10_000, risk_free_rate_pct=4.5,
            benchmark_returns=constant_bench,
        )
        assert out["portfolio_beta"] is None

    async def test_insufficient_history_raises_rather_than_running_a_monte_carlo(self):
        with pytest.raises(Unavailable, match="insufficient historical data"):
            await risk_metrics(self.SERIES[:19], 10_000, risk_free_rate_pct=4.5)


class TestStressTests:
    async def test_scenario_impacts_are_known_percentages_of_value(self):
        out = await stress_tests(10_000.0)
        assert out["market_crash_20pct"] == -2000.0
        assert out["interest_rate_rise_2pct"] == -500.0
        assert out["currency_depreciation_10pct"] == -300.0


class TestCorrelationMatrix:
    BASE: ClassVar[list[float]] = [0.01 * i for i in range(1, 13)]  # 12 monotone points

    async def test_perfect_and_inverse_correlations_are_exact(self):
        returns_by_symbol = {
            "A": self.BASE,
            "B": list(self.BASE),          # perfectly correlated
            "C": [-r for r in self.BASE],  # perfectly inverse
        }
        out = await correlation_matrix(returns_by_symbol, ["A", "B", "C"])
        assert out["A"]["A"] == 1.0
        assert out["A"]["B"] == pytest.approx(1.0)
        assert out["A"]["C"] == pytest.approx(-1.0)
        assert out["B"]["C"] == pytest.approx(-1.0)

    async def test_missing_symbol_raises_rather_than_filling_half(self):
        with pytest.raises(Unavailable, match="insufficient return history for X"):
            await correlation_matrix({"A": self.BASE}, ["A", "X"])

    async def test_short_history_raises_rather_than_filling_half(self):
        with pytest.raises(Unavailable, match="insufficient return history"):
            await correlation_matrix({"A": [0.01] * 5, "B": [0.02] * 5}, ["A", "B"])


class TestBenchmarkComparison:
    async def test_return_and_alpha_on_known_prices(self):
        out = await benchmark_comparison(
            portfolio_return=10.0, benchmark_closes=[100.0, 110.0, 105.0]
        )
        assert out["sp500_return"] == pytest.approx(5.0)
        assert out["alpha"] == pytest.approx(5.0)
        assert out["data_source"] == "historical"
        # v1 returned a fabricated beta of 1.0; v2 omits it entirely.
        assert "beta" not in out

    async def test_single_close_raises_rather_than_returning_zeros(self):
        with pytest.raises(Unavailable, match="insufficient benchmark history"):
            await benchmark_comparison(10.0, [100.0])

    async def test_non_positive_start_price_raises(self):
        with pytest.raises(Unavailable, match="non-positive benchmark start price"):
            await benchmark_comparison(10.0, [0.0, 1.0])
