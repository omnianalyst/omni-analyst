"""The extracted-capability registry: bound, honest, and planner-discoverable."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from omni.capabilities.fixed_income import (
    Bond,
    CouponFrequency,
    DayCountConvention,
    YieldCurve,
    nelson_siegel,
)
from omni.capabilities.portfolio_risk import Scenario
from omni.capabilities.volatility import Bar
from omni.capability.builtin import build_builtin_registry
from omni.capability.extracted import CLAIM_TYPES, build_extracted_registry
from omni.capability.registry import Registry
from omni.ingest.protocol import Unavailable

# The schema enum, as declared in migrations/001_core_schema.sql and
# migrations/003_domains_and_graph.sql. Mirrored here so the test catches a
# capability (or a migration edit) that drifts outside it.
SCHEMA_CLAIM_TYPES = frozenset(
    {
        "price_snapshot",
        "fundamental_metric",
        "filing_event",
        "macro_series_point",
        "news_event",
        "manipulation_signal",
        "perception_news",
        "perception_macro",
        "perception_social",
        "perception_positioning",
        "perception_divergence",
        "onchain_flow",
        "onchain_tvl",
        "onchain_supply",
    }
)


@pytest.fixture
def registry():
    return build_extracted_registry()


# --- Small fixtures and the behaviour each cap must produce on them. --------
#
# Each entry is (kwargs, predicate). The predicate asserts a hand-computed
# value where one is tractable and otherwise a characteristic field, so this is
# a binding smoke-test (the deep maths lives in tests/test_cap_*.py, which this
# work order does not own).

_FIXTURE_DAILY = [0.01, -0.01] * 10  # 20 balanced returns


async def _stocktwits_fetcher(symbol):
    return {
        "messages": [
            {"entities": {"sentiment": {"basic": "Bullish"}}},
            {"entities": {"sentiment": {"basic": "Bullish"}}},
        ],
        "symbol": {"watchlist_count": 5000},
    }


def _factor_model_inputs():
    """Two assets that are exact linear functions of one factor (no noise).

    Gives recoverable exposures (1.0, 0.5) and R-squared of 1.0, with enough
    observations to clear fit_factor_risk_model's min_obs gate.
    """
    idx = pd.date_range("2024-01-01", periods=60, freq="D")
    mkt = pd.Series(np.linspace(-0.01, 0.01, 60), index=idx)
    factor = pd.DataFrame({"MKT": mkt})
    asset = pd.DataFrame({"A": 1.0 * mkt.to_numpy(), "B": 0.5 * mkt.to_numpy()}, index=idx)
    return {"asset_returns": asset, "factor_returns": factor}


def _stress_scenario():
    # 100 exposure to MKT + 200 at 1.5 beta = 400 dollar exposure; * -0.5 = -200.
    return Scenario("crash", factor_shocks={"MKT": -0.5})


def _regress_inputs():
    # Asset returns identical to the single factor -> beta 1, R-squared 1.
    idx = pd.date_range("2024-01-01", periods=80, freq="D")
    r = pd.Series(np.random.default_rng(3).normal(0.001, 0.01, 80), index=idx)
    return {
        "asset_returns": r,
        "factor_returns": pd.DataFrame({"F": r}, index=r.index),
    }


def _attribute_inputs():
    # Portfolio = 2 * factor exactly -> beta 2, specific ~0, additive split.
    idx = pd.date_range("2024-01-01", periods=80, freq="D")
    f = pd.Series(np.random.default_rng(5).normal(0.001, 0.01, 80), index=idx)
    factor = pd.DataFrame({"F": f}, index=idx)
    port = pd.Series(2.0 * f.to_numpy(), index=idx)
    return {"portfolio_returns": port, "factor_returns": factor}


def _market_model_inputs():
    # Portfolio = 1.3 * benchmark + small noise so beta recovers tightly.
    idx = pd.date_range("2024-01-01", periods=500, freq="D")
    rng = np.random.default_rng(5)
    bench = pd.Series(rng.normal(0.0005, 0.01, 500), index=idx)
    port = pd.Series(1.3 * bench.to_numpy() + rng.normal(0, 0.0005, 500), index=idx)
    return {"portfolio_returns": port, "benchmark_returns": bench}


# Fixed-income fixtures: a 5y annual par bond on 30/360 so day-count is exact.
_FI_ISSUE = date(2020, 1, 15)
_FI_MATURITY = date(2025, 1, 15)


def _par_bond(*, coupon_rate=0.05, ytm=0.05, price=100.0):
    return Bond(
        cusip="000AAA0A",
        isin="US000AAA0000",
        issuer="Test",
        issue_date=_FI_ISSUE,
        maturity_date=_FI_MATURITY,
        coupon_rate=coupon_rate,
        coupon_frequency=CouponFrequency.ANNUAL,
        face_value=100.0,
        price=price,
        yield_to_maturity=ytm,
        settlement_date=_FI_ISSUE,
        day_count=DayCountConvention.THIRTY_360,
    )


def _par_bond_with_rating(rating):
    bond = _par_bond(coupon_rate=0.05, ytm=0.05)
    bond.rating = rating
    return bond


def _flat_curve(rate):
    return YieldCurve(
        curve_date=_FI_ISSUE,
        currency="USD",
        tenors=[1.0, 2.0, 3.0, 4.0, 5.0],
        yields=[rate] * 5,
    )


def _nelson_siegel_inputs():
    true = (0.05, -0.01, 0.02, 1.5)
    tenors = [0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 20.0, 30.0]
    yields = [nelson_siegel(t, *true) for t in tenors]
    return {"tenors": tenors, "yields": yields}


def _zero_curve_bonds():
    # Zero-coupon bonds at known yields; the 5y input is 4.5%.
    bonds = []
    tenors_yields = [
        (1.0, 0.03), (2.0, 0.035), (3.0, 0.04),
        (5.0, 0.045), (10.0, 0.05),
    ]
    for years, ytm in tenors_yields:
        maturity = date(2020, 1, 1)
        for _ in range(int(years * 365)):
            from datetime import timedelta as _td
            maturity = maturity + _td(days=1)
        bonds.append(
            Bond(
                cusip=f"Z{years}",
                isin=f"Z{years}",
                issuer="T",
                issue_date=date(2020, 1, 1),
                maturity_date=maturity,
                coupon_rate=0.0,
                coupon_frequency=CouponFrequency.ZERO,
                face_value=100.0,
                price=100.0 / (1 + ytm) ** years,
                yield_to_maturity=ytm,
                settlement_date=date(2020, 1, 1),
                day_count=DayCountConvention.ACTUAL_ACTUAL,
            )
        )
    return bonds


# --- Options / volatility / regime / indicators fixtures -----------------

def _vol_surface_inputs():
    """Market prices at a flat 20% vol so the surface should recover ~0.20."""
    from omni.capabilities.options import black_scholes as _bs

    strikes = [90.0, 100.0, 110.0]
    expiries = [0.25, 0.5, 1.0]
    prices = np.array(
        [
            [_bs(100.0, k, t, 0.05, 0.20, 0.0, "call")["price"] for t in expiries]
            for k in strikes
        ]
    )
    return {
        "spot": 100.0,
        "r": 0.05,
        "q": 0.0,
        "strikes": strikes,
        "expiries": expiries,
        "market_prices": prices,
        "option_type": "call",
    }


def _ohlc_bars():
    """15 synthetic OHLC bars with non-trivial intraday range."""
    rng = np.random.default_rng(11)
    bars = []
    price = 100.0
    for _ in range(15):
        ret = rng.normal(0, 0.01)
        open_ = price
        close = price * (1 + ret)
        high = max(open_, close) * (1 + abs(rng.normal(0, 0.005)))
        low = min(open_, close) * (1 - abs(rng.normal(0, 0.005)))
        bars.append(Bar(open=open_, high=high, low=low, close=close))
        price = close
    return bars


def _returns(n=50, seed=7):
    return list(np.random.default_rng(seed).normal(0, 0.01, n))


def _cases():
    cpi = [100.0] * 11 + [101.0, 103.0]
    return {
        "macro.sahm_rule": (
            {"unemployment_values": [3.5] * 9 + [3.9, 4.1, 4.3]},
            lambda r: bool(r["triggered"]) is True
            and r["value"] == pytest.approx(0.6),
        ),
        "macro.yield_curve_inversion": (
            {
                "series_2y": {"2024-01": 4.0, "2024-02": 4.1},
                "series_10y": {"2024-01": 4.2, "2024-02": 4.3},
            },
            lambda r: r["current_spread"] == pytest.approx(0.2)
            and r["is_inverted"] is False,
        ),
        "macro.recession_probability": (
            {
                "yield_curve_inverted": True,
                "sahm_triggered": True,
                "lei_signals": ["negative", "negative"],
            },
            lambda r: r["probability"] == 1.0 and r["assessment"] == "high",
        ),
        "macro.inflation_measures": (
            {"cpi_values": cpi},
            lambda r: r["yoy"] == pytest.approx(3.0),
        ),
        "macro.pce_inflation": (
            {"pce_values": [100.0] * 12 + [102.0]},
            lambda r: r["yoy"] == pytest.approx(2.0)
            and r["vs_target"] == pytest.approx(0.0),
        ),
        "macro.inflation_expectations": (
            {"exp_5y": [2.2], "exp_10y": [2.3]},
            lambda r: r["5y5y_forward"] == pytest.approx(2.4)
            and r["anchored"] is True,
        ),
        "macro.labor_market_tightness": (
            {"unemployment_rate": 3.5, "job_growth_3m_avg": 200000.0},
            lambda r: r["score"] == pytest.approx((1 / 3.5) * 2.0, rel=1e-3)
            and r["assessment"] == "tight",
        ),
        "macro.taylor_rule": (
            {"inflation": 2.0, "output_gap": 0.0},
            lambda r: r == pytest.approx(2.5),
        ),
        "macro.taylor_rule_variant": (
            {
                "inflation": 2.0,
                "output_gap": 0.0,
                "target": 2.0,
                "r_star": 0.5,
                "alpha": 1.5,
                "beta": 0.5,
            },
            lambda r: r == pytest.approx(2.5),
        ),
        "macro.assess_policy_implications": (
            {"taylor_rate": 4.0, "current_rate": 2.0, "stance": "accommodative"},
            lambda r: r["rate_adjustment_needed"] == pytest.approx(2.0)
            and r["recommendation"]
            and "Inflation risk" in r["risks"][0],
        ),
        "macro.assess_scenario_impact": (
            {
                "baseline": {"gdp": {"2024": 2.0}},
                "scenario": {"gdp": {"2024": 2.5}},
                "variables": ["gdp"],
            },
            lambda r: r["gdp"]["average_impact"] == pytest.approx(0.5),
        ),
        "fundamentals.financial_ratios": (
            {
                "fundamentals": {
                    "income_statement": {
                        "eps": 10.0,
                        "earnings_growth_rate": 0.10,
                        "net_income": 1000.0,
                        "revenue": 5000.0,
                        "operating_income": 1500.0,
                        "cost_of_revenue": 2000.0,
                        "dividends_per_share": 2.0,
                    },
                    "balance_sheet": {
                        "book_value_per_share": 50.0,
                        "total_equity": 5000.0,
                        "total_assets": 10000.0,
                        "total_debt": 2000.0,
                        "current_assets": 3000.0,
                        "current_liabilities": 1500.0,
                        "inventory": 500.0,
                    },
                    "cash_flow": {
                        "operating_cash_flow": 1200.0,
                        "capital_expenditures": 200.0,
                    },
                },
                "current_price": 100.0,
            },
            lambda r: r["pe_ratio"] == pytest.approx(10.0),
        ),
        "fundamentals.dcf_valuation": (
            {
                "fundamentals": {
                    "income_statement": {},
                    "balance_sheet": {
                        "total_debt": 2000.0,
                        "shares_outstanding": 100.0,
                    },
                    "cash_flow": {
                        "operating_cash_flow": 1200.0,
                        "capital_expenditures": 200.0,
                    },
                },
                "current_price": 100.0,
                "growth_rate": 0.10,
                "discount_rate": 0.10,
            },
            lambda r: isinstance(r["fair_value_per_share"], float)
            and r["fair_value_per_share"] > 0,
        ),
        "fundamentals.peer_comparison": (
            {
                "symbol": "AAPL",
                "industry": "Tech",
                "sector": "Technology",
                "comparison_data": [
                    {
                        "symbol": "AAPL",
                        "is_target": True,
                        "pe_ratio": 25.0,
                        "peg_ratio": 1.5,
                        "pb_ratio": 4.0,
                        "roe": 30.0,
                        "debt_to_equity": 1.5,
                        "current_ratio": 1.0,
                        "gross_margin": 40.0,
                        "net_margin": 20.0,
                        "dividend_yield": 1.0,
                    },
                    {
                        "symbol": "PEER1",
                        "pe_ratio": 20.0,
                        "peg_ratio": 1.2,
                        "pb_ratio": 3.0,
                        "roe": 15.0,
                        "debt_to_equity": 0.5,
                        "current_ratio": 2.0,
                        "gross_margin": 35.0,
                        "net_margin": 10.0,
                        "dividend_yield": 2.0,
                    },
                    {
                        "symbol": "PEER2",
                        "pe_ratio": 30.0,
                        "peg_ratio": 1.8,
                        "pb_ratio": 5.0,
                        "roe": 25.0,
                        "debt_to_equity": 1.0,
                        "current_ratio": 1.5,
                        "gross_margin": 45.0,
                        "net_margin": 15.0,
                        "dividend_yield": 1.5,
                    },
                ],
            },
            lambda r: r["peer_averages"]["pe_ratio"] == pytest.approx(25.0)
            and r["peer_count"] == 2,
        ),
        "fundamentals.portfolio_returns": (
            {
                "transactions": [
                    {"transaction_type": "buy", "total_amount": 10000.0}
                ],
                "total_value": 11000.0,
                "daily_returns": [0.01, -0.005, 0.002],
                "period_days": 365,
            },
            lambda r: r["percentage_return"] == pytest.approx(10.0),
        ),
        "fundamentals.risk_metrics": (
            {"daily_returns": _FIXTURE_DAILY, "total_value": 10000.0},
            lambda r: r["data_quality"] == "historical"
            and r["data_points"] == 20
            and isinstance(r["value_at_risk_95"], float),
        ),
        "fundamentals.stress_tests": (
            {"total_value": 10000.0},
            lambda r: r["market_crash_20pct"] == pytest.approx(-2000.0),
        ),
        "fundamentals.correlation_matrix": (
            {
                "returns_by_symbol": {
                    "A": [0.01, 0.02, -0.01, 0.0, 0.03, -0.02, 0.015,
                          -0.005, 0.025, -0.015, 0.005, -0.025],
                    "B": [0.01, 0.02, -0.01, 0.0, 0.03, -0.02, 0.015,
                          -0.005, 0.025, -0.015, 0.005, -0.025],
                },
                "symbols": ["A", "B"],
            },
            lambda r: r["A"]["B"] == pytest.approx(1.0) and r["A"]["A"] == 1.0,
        ),
        "fundamentals.benchmark_comparison": (
            {"portfolio_return": 10.0, "benchmark_closes": [100.0, 120.0]},
            lambda r: r["sp500_return"] == pytest.approx(20.0)
            and r["alpha"] == pytest.approx(-10.0),
        ),
        "news.aggregate_market_sentiment": (
            {"rows": [("bullish", 70, 0.5), ("bearish", 30, -0.3)]},
            lambda r: r["overall"] == "bullish" and r["total_articles"] == 100,
        ),
        "news.score_portfolio_impact": (
            {
                "sentiment_score": 0.5,
                "sentiment": "bullish",
                "confidence": 0.8,
                "affected_tickers": ["AAPL", "MSFT"],
            },
            lambda r: r["impact_score"] == pytest.approx(60.0)
            and r["impact_type"] == "direct",
        ),
        "news.score_stocktwits_messages": (
            {
                "messages": [
                    {"entities": {"sentiment": {"basic": "Bullish"}}},
                    {"entities": {"sentiment": {"basic": "Bullish"}}},
                    {"entities": {"sentiment": {"basic": "Bearish"}}},
                ],
                "watchers": 1000,
            },
            lambda r: r["sentiment_score"] == pytest.approx(1 / 3)
            and r["bullish_count"] == 2,
        ),
        "news.stocktwits_sentiment": (
            {
                "symbol": "AAPL",
                "fetch_fn": _stocktwits_fetcher,
            },
            lambda r: r["sentiment_score"] == 1.0 and r["watchers"] == 5000,
        ),
        "portfolio.optimize_weights": (
            {
                "cov": pd.DataFrame(
                    [[0.04, 0.01], [0.01, 0.09]],
                    index=["A", "B"],
                    columns=["A", "B"],
                ),
            },
            lambda r: isinstance(r, pd.Series)
            and abs(float(r.sum()) - 1.0) < 1e-9
            and (r.to_numpy() >= -1e-12).all(),
        ),
        "portfolio.vol_target_weights": (
            {
                "asset_vols": pd.Series([0.10, 0.20], index=["A", "B"]),
                "target_vol": 0.10,
                "max_leverage": 5.0,
            },
            lambda r: isinstance(r, pd.Series)
            and abs(
                float(np.sqrt(np.sum((r.to_numpy() * np.array([0.10, 0.20])) ** 2)))
                - 0.10
            )
            < 1e-9,
        ),
        "portfolio.risk_contributions": (
            {
                "cov": pd.DataFrame(
                    [[0.04, 0.01], [0.01, 0.09]],
                    index=["A", "B"],
                    columns=["A", "B"],
                ),
                "weights": [0.5, 0.5],
            },
            lambda r: abs(float(r.sum()) - 1.0) < 1e-9
            and float(r["B"]) > float(r["A"]),
        ),
        "portfolio.fit_factor_risk_model": (
            _factor_model_inputs(),
            lambda r: list(r.assets) == ["A", "B"]
            and abs(r.exposures[0, 0] - 1.0) < 1e-6
            and abs(r.exposures[1, 0] - 0.5) < 1e-6
            and r.r_squared.min() > 0.999,
        ),
        "portfolio.atr_position_size": (
            {
                "equity": 100000.0,
                "atr_value": 2.0,
                "price": 50.0,
                "risk_fraction": 0.0015,
            },
            lambda r: r == pytest.approx(75.0),
        ),
        "portfolio.fractional_kelly": (
            {"edge": 0.10, "odds": 2.0, "fraction": 0.5, "cap": 1.0},
            lambda r: r == pytest.approx(0.025),
        ),
        "portfolio.meta_label_size": (
            {"probability": 0.75, "threshold": 0.5, "max_size": 1.0},
            lambda r: r == pytest.approx(0.5),
        ),
        "portfolio.drawdown_breaker": (
            {
                "equity_curve": [100, 90, 100, 95, 80],
                "threshold": 0.15,
                "size": 1.0,
            },
            lambda r: r == 0.0,
        ),
        "portfolio_risk.calculate_var": (
            {
                "returns": [-0.10, 0.00] + [0.01] * 18,
                "confidence_level": 0.95,
                "seed": 42,
            },
            lambda r: r["historical"]["daily_var_pct"] == pytest.approx(-0.5)
            and r["confidence_level"] == 0.95
            and "monte_carlo" in r
            and "parametric" in r,
        ),
        "portfolio_risk.calculate_cvar": (
            {
                "returns": [-0.10, 0.00] + [0.01] * 18,
                "confidence_level": 0.95,
            },
            lambda r: r["daily_cvar_pct"] == pytest.approx(-10.0)
            and r["confidence_level"] == 0.95,
        ),
        "portfolio_risk.calculate_beta": (
            {
                "asset_returns": [0.01, 0.02, -0.01, 0.005, -0.02, 0.015,
                                  -0.005, 0.025, -0.015, 0.03],
                "benchmark_returns": [0.01, 0.02, -0.01, 0.005, -0.02, 0.015,
                                      -0.005, 0.025, -0.015, 0.03],
            },
            lambda r: r == pytest.approx(1.0),
        ),
        "portfolio_risk.calculate_correlation_matrix": (
            {
                "returns_by_symbol": {
                    "A": [0.01, 0.02, -0.01, 0.005, -0.02, 0.015,
                          -0.005, 0.025, -0.015, 0.03],
                    "B": [0.01, 0.02, -0.01, 0.005, -0.02, 0.015,
                          -0.005, 0.025, -0.015, 0.03],
                }
            },
            lambda r: r["matrix"]["A"]["B"] == pytest.approx(1.0)
            and r["average_correlation"] == pytest.approx(1.0),
        ),
        "portfolio_risk.stress_book": (
            {
                "assets": ["A", "B"],
                "factors": ["MKT"],
                "exposures": [[1.0], [1.5]],
                "positions": {"A": 100.0, "B": 200.0},
                "scenario": _stress_scenario(),
            },
            lambda r: r.scenario == "crash"
            and r.factor_pnl == pytest.approx(-200.0)
            and r.specific_pnl == pytest.approx(0.0)
            and r.total_pnl == pytest.approx(-200.0),
        ),
        "attribution.regress_factor_exposures": (
            _regress_inputs(),
            lambda r: r.betas[0] == pytest.approx(1.0, abs=1e-9)
            and r.r_squared == pytest.approx(1.0, abs=1e-9)
            and list(r.factor_names) == ["F"],
        ),
        "attribution.attribute_returns": (
            _attribute_inputs(),
            lambda r: abs(r.specific_return) < 1e-9
            and abs(
                sum(r.factor_contributions.values()) + r.specific_return
                - r.total_return
            )
            < 1e-9,
        ),
        "attribution.market_model_attribution": (
            _market_model_inputs(),
            lambda r: r.beta == pytest.approx(1.3, abs=1e-2)
            and r.factor_return + r.specific_return
            == pytest.approx(r.total_return, abs=1e-9),
        ),
        "attribution.holding_contributions": (
            {
                "holdings_returns": pd.DataFrame(
                    {"AAA": [0.01, 0.02], "BBB": [0.02, 0.03]}
                ),
                "weights": {"AAA": 0.5, "BBB": 0.5},
            },
            lambda r: r["AAA"] == pytest.approx(0.015)
            and r["BBB"] == pytest.approx(0.025),
        ),
        "fixed_income.calculate_price": (
            {"bond": _par_bond(coupon_rate=0.05, ytm=0.05)},
            lambda r: r == pytest.approx(100.0, abs=1e-9),
        ),
        "fixed_income.calculate_yield": (
            {
                "bond": _par_bond(coupon_rate=0.05, ytm=None, price=100.0),
                "price": 100.0,
            },
            lambda r: r == pytest.approx(0.05, abs=1e-9),
        ),
        "fixed_income.calculate_duration": (
            {"bond": _par_bond()},
            lambda r: r["modified_duration"] > 0
            and r["macaulay_duration"] > 0
            and r["modified_duration"] < r["macaulay_duration"],
        ),
        "fixed_income.calculate_convexity": (
            {"bond": _par_bond()},
            lambda r: r > 0,
        ),
        "fixed_income.calculate_z_spread": (
            {
                "bond": _par_bond(coupon_rate=0.05, ytm=None, price=100.0),
                "risk_free_curve": _flat_curve(0.03),
            },
            lambda r: r > 0,
        ),
        "fixed_income.calculate_spread_duration": (
            {
                "bond": _par_bond(coupon_rate=0.05, ytm=None, price=100.0),
                "risk_free_curve": _flat_curve(0.03),
            },
            lambda r: r > 0,
        ),
        "fixed_income.calculate_credit_metrics": (
            {
                "bond": _par_bond(coupon_rate=0.05, ytm=None, price=100.0),
                "risk_free_curve": _flat_curve(0.03),
                "recovery_rate": 0.4,
            },
            lambda r: r["z_spread"] > 0
            and r["recovery_rate"] == 0.4
            and r["z_spread_bps"] == pytest.approx(r["z_spread"] * 10000)
            and "implied_default_probability" in r,
        ),
        "fixed_income.calculate_total_return": (
            {
                "bond": _par_bond(coupon_rate=0.05, ytm=0.05),
                "holding_period_days": 366,
                "ending_yield": 0.05,
                "reinvestment_rate": 0.05,
            },
            lambda r: r["coupon_income"] == pytest.approx(5.0, abs=1e-9)
            and r["total_return"] == pytest.approx(5.0, abs=1e-9),
        ),
        "fixed_income.analyze_credit_migration": (
            {
                "bond": _par_bond_with_rating("AAA"),
                "transition_matrix": pd.DataFrame(
                    {"AA": [0.1], "AAA": [0.9]}, index=["AAA"]
                ),
                "rating_spreads": {"AAA": 0.0, "AA": 0.005},
            },
            lambda r: r["migration_scenarios"]["AA"]["spread_change_bps"]
            == pytest.approx(50.0)
            and r["downgrade_probability"] == pytest.approx(0.1),
        ),
        "fixed_income.fit_nelson_siegel": (
            _nelson_siegel_inputs(),
            lambda r: len(r) == 4
            and all(np.isfinite(v) for v in r),
        ),
        "fixed_income.build_yield_curve": (
            {"bonds": _zero_curve_bonds(), "curve_date": date(2020, 1, 1)},
            lambda r: r.interpolate(5.0) == pytest.approx(0.045, abs=0.005),
        ),
        # --- options ---
        "options.black_scholes": (
            {
                "S": 100.0,
                "K": 100.0,
                "T": 1.0,
                "r": 0.05,
                "sigma": 0.2,
                "q": 0.0,
                "option_type": "call",
            },
            lambda r: r["price"] == pytest.approx(10.45, abs=0.05)
            and 0.0 < r["delta"] < 1.0,
        ),
        "options.implied_volatility": (
            {
                "market_price": 10.45,
                "S": 100.0,
                "K": 100.0,
                "T": 1.0,
                "r": 0.05,
                "q": 0.0,
                "option_type": "call",
            },
            lambda r: r is not None and abs(r - 0.2) < 0.01,
        ),
        "options.monte_carlo": (
            {
                "S": 100.0,
                "K": 100.0,
                "T": 1.0,
                "r": 0.05,
                "sigma": 0.2,
                "q": 0.0,
                "option_type": "call",
                "simulations": 20000,
                "seed": 42,
            },
            lambda r: r["price"] == pytest.approx(10.45, abs=1.0)
            and r["std_error"] > 0,
        ),
        "options.build_volatility_surface": (
            _vol_surface_inputs(),
            lambda r: isinstance(r, np.ndarray)
            and r.shape == (3, 3)
            and abs(float(r[1, 1]) - 0.20) < 0.01,
        ),
        "options.put_call_ratio": (
            {
                "contracts": [
                    {"option_type": "call", "volume": 100, "strike": 100.0},
                    {"option_type": "put", "volume": 50, "strike": 100.0},
                ]
            },
            lambda r: r["ratio"] == pytest.approx(0.5)
            and r["call_volume"] == 100,
        ),
        "options.max_pain": (
            {
                "contracts": [
                    {"option_type": "call", "strike": 100.0, "open_interest": 100},
                    {"option_type": "put", "strike": 100.0, "open_interest": 100},
                    {"option_type": "call", "strike": 110.0, "open_interest": 50},
                    {"option_type": "put", "strike": 110.0, "open_interest": 50},
                ]
            },
            lambda r: r["strike"] == 100.0,
        ),
        "options.detect_unusual_activity": (
            {
                "contracts": [
                    {"option_type": "call", "strike": 100.0, "volume": 300, "open_interest": 100},
                    {"option_type": "put", "strike": 100.0, "volume": 10, "open_interest": 200},
                ]
            },
            lambda r: len(r) == 1
            and r[0]["strike"] == 100.0
            and r[0]["vol_oi_ratio"] == pytest.approx(3.0),
        ),
        "options.put_call_parity_errors": (
            {
                "contracts": [
                    {"option_type": "call", "strike": 100.0, "expiry": "2024-12-20", "bid": 14.0, "ask": 16.0},
                    {"option_type": "put", "strike": 100.0, "expiry": "2024-12-20", "bid": 5.0, "ask": 7.0},
                ],
                "underlying_price": 100.0,
                "risk_free_rate": 0.05,
                "time_to_expiry": 1.0,
            },
            lambda r: len(r) == 1 and r[0]["parity_error"] > 0.10,
        ),
        # --- volatility ---
        "volatility.close_to_close": (
            {
                "prices": [100, 101, 99, 102, 100, 103, 98, 101, 100, 102, 99, 101],
                "window": 10,
                "annualisation": 252,
            },
            lambda r: r > 0,
        ),
        "volatility.ewma": (
            {
                "prices": [100, 101, 99, 102, 100, 103, 98, 101, 100, 102, 99, 101],
                "window": 10,
                "annualisation": 252,
            },
            lambda r: r > 0,
        ),
        "volatility.parkinson": (
            {"bars": _ohlc_bars(), "window": 10, "annualisation": 252},
            lambda r: r > 0,
        ),
        "volatility.garman_klass": (
            {"bars": _ohlc_bars(), "window": 10, "annualisation": 252},
            lambda r: r > 0,
        ),
        "volatility.rogers_satchell": (
            {"bars": _ohlc_bars(), "window": 10, "annualisation": 252},
            lambda r: r > 0,
        ),
        "volatility.volatility_of_volatility": (
            {
                "volatilities": [0.15, 0.18, 0.20, 0.17, 0.22, 0.19, 0.21,
                                 0.16, 0.20, 0.18, 0.19, 0.21],
                "window": 10,
                "annualisation": 252,
            },
            lambda r: r > 0,
        ),
        # --- regime ---
        "regime.realised_volatility": (
            {"returns": _returns(50)},
            lambda r: isinstance(r, np.ndarray)
            and len(r) == 50
            and np.all(r >= 0),
        ),
        "regime.volatility_regime_path": (
            {"returns": _returns(50)},
            lambda r: isinstance(r, list)
            and len(r) == 50
            and all(l in ("quiet", "transition", "volatile") for l in r),
        ),
        "regime.classify_volatility": (
            {"returns": _returns(50)},
            lambda r: r["regime"] in ("quiet", "transition", "volatile")
            and r["current_volatility"] >= 0,
        ),
        "regime.classify_trend": (
            {"returns": _returns(80)},
            lambda r: r["regime"] in ("uptrend", "downtrend", "neutral")
            and "ma_short" in r
            and "ma_long" in r,
        ),
        "regime.detect_regime_changes": (
            {
                "regimes": [
                    "quiet", "quiet", "volatile", "volatile",
                    "quiet", "transition",
                ]
            },
            lambda r: len(r) == 3
            and r[0]["index"] == 2
            and r[0]["from_regime"] == "quiet"
            and r[0]["to_regime"] == "volatile",
        ),
        # --- indicators ---
        "indicators.sma": (
            {
                "prices": [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20],
                "period": 5,
            },
            lambda r: r[0] is None
            and r[4] == pytest.approx(12.0)
            and r[-1] == pytest.approx(18.0),
        ),
        "indicators.ema": (
            {
                "prices": [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20],
                "period": 5,
            },
            lambda r: r[0] is None
            and r[4] == pytest.approx(12.0)
            and r[-1] is not None,
        ),
        "indicators.rsi": (
            {
                "prices": [44, 44.34, 44.09, 43.61, 44.33, 44.83,
                           45.10, 45.42, 45.84, 46.36, 46.84, 47.01],
                "period": 7,
            },
            lambda r: all(v is None for v in r[:7])
            and all(v is not None and 0 <= v <= 100 for v in r[7:]),
        ),
        "indicators.macd": (
            {
                "prices": list(range(1, 40)),
                "fast_period": 5,
                "slow_period": 10,
                "signal_period": 3,
            },
            lambda r: "macd" in r and "signal" in r and "histogram" in r
            and len(r["macd"]) == 39,
        ),
        "indicators.bollinger_bands": (
            {
                "prices": [10, 12, 11, 13, 14, 12, 15, 13, 16, 14, 17, 15],
                "period": 5,
                "num_std": 2.0,
            },
            lambda r: r["upper"][0] is None
            and r["middle"][4] == pytest.approx(12.0)
            and r["upper"][4] >= r["middle"][4] >= r["lower"][4],
        ),
        "indicators.stochastic": (
            {
                "high": [10, 11, 12, 11, 13, 14, 13, 15, 16, 15, 17, 18],
                "low": [9, 10, 11, 10, 12, 13, 12, 14, 15, 14, 16, 17],
                "close": [10, 10.5, 11.5, 10.5, 12.5, 13.5,
                          12.5, 14.5, 15.5, 14.5, 16.5, 17.5],
                "k_period": 5,
                "d_period": 3,
            },
            lambda r: "k" in r and "d" in r
            and r["k"][0] is None
            and any(v is not None for v in r["k"]),
        ),
        "indicators.atr": (
            {
                "high": [10, 11, 12, 11, 13, 14, 13, 15, 16, 15, 17, 18],
                "low": [9, 10, 11, 10, 12, 13, 12, 14, 15, 14, 16, 17],
                "close": [10, 10.5, 11.5, 10.5, 12.5, 13.5,
                          12.5, 14.5, 15.5, 14.5, 16.5, 17.5],
                "period": 5,
            },
            lambda r: len(r) == 12 and r[0] is None and r[5] is not None,
        ),
        "indicators.vwap": (
            {
                "prices": [10, 11, 12, 11, 13],
                "volumes": [100, 200, 150, 300, 250],
            },
            lambda r: len(r) == 5
            and r[0] == pytest.approx(10.0)
            and all(v is not None for v in r),
        ),
        "indicators.obv": (
            {
                "prices": [10, 11, 10, 12, 11],
                "volumes": [100, 200, 150, 300, 250],
            },
            lambda r: r == [0.0, 200.0, 50.0, 350.0, 100.0],
        ),
    }


class TestRegistryShape:
    def test_every_bound_capability_is_invocable(self, registry):
        """The extracted registry is the opposite of the census: nothing here
        is aspirational."""
        assert len(registry) == registry.summary()["invocable"]
        assert registry.backlog() == []

    def test_claim_type_constant_matches_the_schema_enum(self):
        assert CLAIM_TYPES == SCHEMA_CLAIM_TYPES

    def test_every_declared_claim_type_is_in_the_closed_enum(self, registry):
        for name in registry._by_name:
            cap = registry.get(name)
            declared = set(cap.produces) | set(cap.consumes)
            assert declared <= SCHEMA_CLAIM_TYPES, (
                f"{name} declares a claim type outside the schema enum: {declared}"
            )

    def test_there_are_producers_for_the_shareable_query_test(self, registry):
        # A guard against an edit that removes every producer and makes the
        # shareable-exclusion test below vacuous.
        producers = {c.name for c in registry._by_name.values() if c.produces}
        assert producers == {
            "news.aggregate_market_sentiment",
            "news.score_stocktwits_messages",
            "news.stocktwits_sentiment",
        }


class TestInvocation:
    @pytest.mark.parametrize("name", list(_cases().keys()))
    async def test_every_capability_runs_and_produces_its_value(self, registry, name):
        kwargs, predicate = _cases()[name]
        result = await registry.get(name).call(**kwargs)
        assert predicate(result), f"{name} returned an unexpected result: {result!r}"

    async def test_a_missing_dependency_raises_rather_than_substituting(self, registry):
        """The fill pipeline needs the reason, not a silent default."""
        with pytest.raises(Unavailable):
            await registry.get("macro.sahm_rule").call(
                unemployment_values=[3.5, 3.6, 3.7]
            )

    async def test_empty_messages_raise_rather_than_neutral(self, registry):
        with pytest.raises(Unavailable):
            await registry.get("news.score_stocktwits_messages").call(
                messages=[], watchers=0
            )

    async def test_the_sync_and_async_wrappers_both_await_correctly(self, registry):
        # taylor_rule is sync; sahm_rule is async. Both must return through the
        # async `call` the binder builds.
        sync_out = await registry.get("macro.taylor_rule").call(
            inflation=2.0, output_gap=0.0
        )
        async_out = await registry.get("macro.sahm_rule").call(
            unemployment_values=[3.5] * 9 + [3.9, 4.1, 4.3]
        )
        assert sync_out == pytest.approx(2.5)
        assert "triggered" in async_out


class TestLicenceClassification:
    @pytest.mark.parametrize(
        "name",
        [
            "macro.sahm_rule",
            "macro.yield_curve_inversion",
            "macro.recession_probability",
            "macro.inflation_measures",
            "macro.pce_inflation",
            "macro.inflation_expectations",
            "macro.labor_market_tightness",
            "macro.taylor_rule",
            "macro.taylor_rule_variant",
            "macro.assess_policy_implications",
            "macro.assess_scenario_impact",
        ],
    )
    def test_macro_from_fred_is_shareable(self, registry, name):
        assert registry.get(name).touches_byo is False

    @pytest.mark.parametrize(
        "name",
        [
            "fundamentals.financial_ratios",
            "fundamentals.dcf_valuation",
            "fundamentals.peer_comparison",
            "fundamentals.stress_tests",
        ],
    )
    def test_edgar_sourced_fundamentals_are_shareable(self, registry, name):
        assert registry.get(name).touches_byo is False

    @pytest.mark.parametrize(
        "name",
        [
            "fundamentals.portfolio_returns",
            "fundamentals.risk_metrics",
            "fundamentals.correlation_matrix",
            "fundamentals.benchmark_comparison",
        ],
    )
    def test_portfolio_analytics_over_prices_inherit_the_price_licence(
        self, registry, name
    ):
        assert registry.get(name).touches_byo is True

    @pytest.mark.parametrize(
        "name",
        [
            "news.aggregate_market_sentiment",
            "news.score_portfolio_impact",
            "news.score_stocktwits_messages",
            "news.stocktwits_sentiment",
        ],
    )
    def test_news_and_sentiment_from_commercial_apis_are_private(
        self, registry, name
    ):
        assert registry.get(name).touches_byo is True

    @pytest.mark.parametrize(
        "name",
        [
            "options.black_scholes",
            "options.implied_volatility",
            "options.monte_carlo",
            "options.build_volatility_surface",
            "options.put_call_ratio",
            "options.max_pain",
            "options.detect_unusual_activity",
            "options.put_call_parity_errors",
            "volatility.close_to_close",
            "volatility.ewma",
            "volatility.parkinson",
            "volatility.garman_klass",
            "volatility.rogers_satchell",
            "volatility.volatility_of_volatility",
            "regime.realised_volatility",
            "regime.volatility_regime_path",
            "regime.classify_volatility",
            "regime.classify_trend",
            "regime.detect_regime_changes",
            "indicators.sma",
            "indicators.ema",
            "indicators.rsi",
            "indicators.macd",
            "indicators.bollinger_bands",
            "indicators.stochastic",
            "indicators.atr",
            "indicators.vwap",
            "indicators.obv",
        ],
    )
    def test_market_analytics_over_prices_inherit_the_price_licence(
        self, registry, name
    ):
        assert registry.get(name).touches_byo is True

    def test_a_shareable_query_excludes_every_licensed_producer(self, registry):
        """How a planner avoids tainting an answer it intends to share."""
        assert registry.producing("perception_social", allow_byo=False) == []
        assert registry.producing("perception_news", allow_byo=False) == []
        assert len(registry.producing("perception_social")) == 2
        assert len(registry.producing("perception_news")) == 1

    def test_the_extracted_registry_has_no_shareable_producer_of_its_own(
        self, registry
    ):
        """The extracted caps transform already-ingested data; the shareable
        producers live in builtin (fred.series, edgar.companyfacts, ...). Here,
        asking for any shareable producer must come up empty."""
        for claim_type in SCHEMA_CLAIM_TYPES:
            assert registry.producing(claim_type, allow_byo=False) == []

    def test_entity_kinds_route_equities_away_from_macro(self, registry):
        assert registry.get("fundamentals.financial_ratios").entity_kinds == (
            "company",
        )
        assert registry.get("macro.inflation_measures").entity_kinds == ()


class TestCompositionWithBuiltin:
    def test_the_two_registries_share_no_capability_name(self, registry):
        builtin = build_builtin_registry()
        overlap = set(registry._by_name) & set(builtin._by_name)
        assert overlap == set(), f"name collision: {overlap}"

    def test_both_registries_combine_without_collision(self, registry):
        """The orchestrator mounts one registry; the two builders must compose."""
        builtin = build_builtin_registry()
        combined = Registry()
        for cap in registry._by_name.values():
            combined.add(cap)
        for cap in builtin._by_name.values():
            combined.add(cap)
        assert len(combined) == len(registry) + len(builtin)

    def test_builtin_prices_and_extracted_analytics_are_both_discoverable(
        self, registry
    ):
        builtin = build_builtin_registry()
        combined = Registry()
        for cap in registry._by_name.values():
            combined.add(cap)
        for cap in builtin._by_name.values():
            combined.add(cap)
        # The price producer (builtin) and the price consumer (extracted) coexist
        # without one shadowing the other.
        assert {c.name for c in combined.producing("price_snapshot")} == {
            "polygon.aggregates",
            "coingecko.market_chart",
        }
        assert combined.get("fundamentals.risk_metrics").consumes == (
            "price_snapshot",
        )
