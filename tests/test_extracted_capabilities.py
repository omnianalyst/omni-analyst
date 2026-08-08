"""The extracted-capability registry: bound, honest, and planner-discoverable."""

import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from omni.capabilities.execution_analytics import BenchmarkBar, Fill
from omni.capabilities.fixed_income import (
    Bond,
    CouponFrequency,
    DayCountConvention,
    YieldCurve,
    nelson_siegel,
)
from omni.capabilities.portfolio_risk import Scenario
from omni.capabilities.signal_fusion import NormalizationMethod
from omni.capabilities.volatility import Bar
from omni.capability.builtin import build_builtin_registry
from omni.capability.extracted import CLAIM_TYPES, build_extracted_registry
from omni.capability.registry import Registry
from omni.ingest.protocol import Unavailable

# The schema enum, as declared across the migration files. Mirrored here so
# the test catches a capability (or a migration edit) that drifts outside it.
# The migration-driven test (test_claim_types_frozenset_mirrors_the_migration_enum)
# is the real drift guard; this constant exists for the declared-claim-type
# checks below.
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
        "yield_curve_signal",
        "sahm_rule_signal",
        "inflation_signal",
        "output_gap_signal",
        "lei_signal",
        "regime_assessment",
        "sector_score",
        # 035: crypto derivatives.
        "funding_rate",
        "open_interest",
        "liquidation_event",
        "basis",
        # 037: protocol fundamentals.
        "protocol_revenue",
        "protocol_fees",
        "stablecoin_supply",
        "chain_tvl",
    }
)


_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _claim_type_enum_from_migrations() -> frozenset[str]:
    """Read every claim_type enum value out of the migration SQL files.

    Parses ``CREATE TYPE claim_type AS ENUM (...)`` (001) and every
    ``ALTER TYPE claim_type ADD VALUE IF NOT EXISTS '...'`` (003, 010, ...)
    so the frozenset in extracted.py cannot silently drift from the schema
    the next time a claim type is added. Comment text is stripped first so a
    commented-out DDL statement cannot fool the parser.
    """
    values: set[str] = set()
    create_re = re.compile(
        r"CREATE\s+TYPE\s+claim_type\s+AS\s+ENUM\s*\((.*?)\)",
        re.DOTALL | re.IGNORECASE,
    )
    alter_re = re.compile(
        r"ALTER\s+TYPE\s+claim_type\s+ADD\s+VALUE\s+IF\s+NOT\s+EXISTS"
        r"\s+'([^']+)'",
        re.IGNORECASE,
    )
    value_re = re.compile(r"'([^']+)'")
    for sql_path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        raw = sql_path.read_text()
        cleaned = "\n".join(
            line.split("--", 1)[0] for line in raw.splitlines()
        )
        for m in create_re.finditer(cleaned):
            values.update(value_re.findall(m.group(1)))
        values.update(alter_re.findall(cleaned))
    return frozenset(values)


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


# --- Microstructure / execution / signal-fusion fixtures ----------------

# A flat quote book (bid 99.99 / ask 100.01, mid 100.00) and 10 buys at the
# ask, so effective_spread = 2 * |100.01 - 100.00| = 0.02 and a buy at 100.01
# whose future mid reverts to 100.00 gives realised_spread = -0.02.
def _effective_spread_inputs():
    from datetime import UTC, datetime, timedelta

    base = datetime(2024, 1, 1, 9, 30, tzinfo=UTC)
    quotes = [
        {"timestamp": base + timedelta(minutes=i), "bid": 99.99, "ask": 100.01}
        for i in range(25)
    ]
    trades = [
        {
            "timestamp": base + timedelta(minutes=i, seconds=30),
            "price": 100.01,
            "side": "buy",
            "volume": 100.0,
        }
        for i in range(1, 11)
    ]
    return {"trades": trades, "quotes": quotes, "horizon": timedelta(minutes=5)}


# 11 upticks where price_change == 1e-4 * signed_volume exactly, so the OLS
# slope is 1e-4 and lambda = slope * 1e4 == 1.0.
def _kyle_lambda_inputs():
    volumes = [(i + 1) * 10.0 for i in range(11)]
    prices = [100.0]
    for i in range(1, 11):
        prices.append(prices[-1] + 1e-4 * volumes[i])
    trades = [{"price": p, "volume": v} for p, v in zip(prices, volumes)]
    return {"trades": trades}


# 50 strictly-increasing-price trades at constant volume: every volume bucket
# is one-sided (all buys), so VPIN == 1.0.
def _vpin_inputs():
    trades = [
        {"price": 100.0 + i * 0.001, "volume": 10.0} for i in range(50)
    ]
    return {"trades": trades}


# 40 of 100 ordered, filled at 100 vs decision 100, market 101, close 104:
# delay = 0.4*1/100 = 40 bps, trading = 0.4*(100-101)/100 = -40 bps,
# opportunity = 0.6*4/100 = 240 bps, total = 240 bps (additive).
def _implementation_shortfall_inputs():
    from datetime import UTC, datetime

    return {
        "fills": [Fill(price=100.0, size=40.0, timestamp=datetime(2024, 1, 1, 10, 0, tzinfo=UTC))],
        "decision_price": 100.0,
        "market_price_at_execution": 101.0,
        "close_price": 104.0,
        "order_quantity": 100.0,
        "side": "buy",
    }


def _fill(price=101.0, size=100.0):
    from datetime import UTC, datetime

    return Fill(price=price, size=size, timestamp=datetime(2024, 1, 1, 10, 0, tzinfo=UTC))


def _flat_window_bars(price=100.0):
    from datetime import UTC, datetime, timedelta

    base = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    return [
        BenchmarkBar(timestamp=base, price=price, volume=50.0),
        BenchmarkBar(timestamp=base + timedelta(minutes=1), price=price, volume=50.0),
    ]


def _cases():
    cpi = [100.0] * 11 + [101.0, 103.0]

    # A 15-date x 6-asset panel where signal rank matches forward-return rank on
    # every date -> per-date IC of 1.0 with enough periods to be significant and
    # to reach the STRONG EDGE verdict. Hand-computed in test_cap_crossasset.
    _edge_rows = []
    for _d in range(15):
        for _a in range(6):
            _edge_rows.append({
                "date": _d,
                "asset": f"a{_a}",
                "signal": _a + 1,
                "forward_return": (_a + 1) * 0.1,
            })
    _edge_panel = pd.DataFrame(_edge_rows)

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
                "risk_free_rate_pct": 4.5,
            },
            lambda r: r["percentage_return"] == pytest.approx(10.0),
        ),
        "fundamentals.risk_metrics": (
            {"daily_returns": _FIXTURE_DAILY, "total_value": 10000.0, "risk_free_rate_pct": 4.5},
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
        # --- microstructure ---
        "microstructure.effective_spread": (
            _effective_spread_inputs(),
            lambda r: r["effective_spread"] == pytest.approx(0.02)
            and r["realized_spread"] == pytest.approx(0.02)
            and r["price_improvement"] == pytest.approx(-0.01),
        ),
        "microstructure.kyle_lambda": (
            _kyle_lambda_inputs(),
            # price_change == 1e-4 * signed_volume, so the OLS slope is exactly
            # 1e-4 (ddof-independent); scaled by 1e4 -> 1.0. The prior
            # np.cov(ddof=1)/np.var(ddof=0) form added an n/(n-1) factor and
            # returned 10/9; this asserts the corrected regression coefficient.
            lambda r: r == pytest.approx(1.0),
        ),
        "microstructure.order_flow_toxicity": (
            _vpin_inputs(),
            lambda r: r == pytest.approx(1.0),
        ),
        # --- execution analytics ---
        "execution.implementation_shortfall": (
            _implementation_shortfall_inputs(),
            lambda r: r.total_bps == pytest.approx(240.0)
            and r.delay_cost_bps == pytest.approx(40.0)
            and r.trading_cost_bps == pytest.approx(-40.0)
            and r.opportunity_cost_bps == pytest.approx(240.0)
            and r.fill_rate == pytest.approx(0.4),
        ),
        "execution.benchmark_slippage": (
            {"fills": [_fill(101.0)], "benchmark_price": 100.0, "side": "buy"},
            lambda r: r == pytest.approx(100.0),
        ),
        "execution.vwap_slippage": (
            {"fills": [_fill(101.0)], "bars": _flat_window_bars(100.0), "side": "buy"},
            lambda r: r == pytest.approx(100.0),
        ),
        "execution.slippage_summary": (
            {"slippage_bps": [10.0, 20.0, 30.0]},
            lambda r: r["mean_bps"] == pytest.approx(20.0)
            and r["median_bps"] == pytest.approx(20.0)
            and r["n"] == 3,
        ),
        "execution.identify_outliers": (
            {"values": [0.0] * 9 + [100.0]},
            lambda r: len(r) == 1
            and r[0]["index"] == 9
            and r[0]["z_score"] == pytest.approx(3.0),
        ),
        # --- signal fusion ---
        "signal_fusion.normalize": (
            {
                "values": [1.0, 2.0, 3.0],
                "method": NormalizationMethod.MIN_MAX,
                "native_range": (1.0, 3.0),
            },
            lambda r: r.tolist() == pytest.approx([-1.0, 0.0, 1.0]),
        ),
        "signal_fusion.convergence": (
            {"signal_values": {"a": 0.5, "b": 0.5}},
            lambda r: r.direction == pytest.approx(0.5)
            and r.alignment == pytest.approx(1.0)
            and r.bullish == 2
            and r.bearish == 0,
        ),
        "signal_fusion.conviction": (
            {"alignment_value": 0.8, "direction_value": 0.5, "participation": 0.5},
            lambda r: r == pytest.approx(0.68),
        ),
        "signal_fusion.lead_lag": (
            {
                "a": [float(i) for i in range(40)],
                "b": [0.0, 0.0] + [float(i) for i in range(38)],
                "max_lag": 5,
            },
            lambda r: r.lag == 2
            and r.correlation == pytest.approx(1.0)
            and r.significance == pytest.approx(1.0),
        ),
        # --- market risk (capabilities/risk.py) ---
        "market_risk.liquidity_risk": (
            {"quotes": [(100.0, 100.5), (100.0, 101.0)]},
            # (100,100.5) spread 0.5 not wide; (100,101) spread 1.0 wide -> 1/2
            lambda r: r["score"] == 100 and r["wide_spread_ratio"] == 0.5,
        ),
        "market_risk.concentration_risk": (
            {"market_caps": [100.0, 100.0, 100.0, 100.0, 100.0]},
            # 5 equal -> HHI 5*(0.2**2)*10000 = 2000 -> score 20; top5 = 1.0
            lambda r: r["herfindahl_index"] == pytest.approx(2000.0)
            and r["score"] == pytest.approx(20.0)
            and r["top_5_concentration"] == 1.0,
        ),
        "market_risk.options_skew": (
            {"skews": [10.0, None, 10.0]},
            # avg 10 -> score (10 + 20) * 2.5 = 75; None dropped
            lambda r: r["score"] == 75.0 and r["average_skew"] == 10.0,
        ),
        "market_risk.breadth": (
            {
                "advance_decline_ratio": 0.4,
                "percent_above_50ma": 80,
                "percent_above_200ma": 80,
                "new_highs": 10,
                "new_lows": 5,
            },
            # A/D 0.4 < 0.5 -> stressed branch -> score 80
            lambda r: r["score"] == 80.0 and r["advance_decline_ratio"] == 0.4,
        ),
        "market_risk.growth_risk": (
            {"gdp_growth": -0.5, "unemployment": 6.0, "job_growth": 40000},
            # gdp<0 -> 90; unemp>5 -> +20 = 100; recession 0.15+0.5+0.2 = 0.85
            lambda r: r["score"] == 100
            and r["growth_score_recession_heuristic"] == pytest.approx(0.85),
        ),
        "market_risk.credit_risk": (
            {"ig_spread": 200.0, "hy_spread": 700.0},
            # ig 200 > 120*1.5 -> stressed band 80; spread_widening True
            lambda r: r["score"] == 80 and r["spread_widening"] is True,
        ),
        "market_risk.correlation_risks": (
            {
                "returns_data": {
                    "A": [0.01, 0.02, -0.01, 0.0],
                    "B": [0.01, 0.02, -0.01, 0.0],
                }
            },
            # two identical series -> avg corr 1.0 -> score 80; cluster [[A, B]]
            lambda r: r["score"] == 80
            and r["average_correlation"] == pytest.approx(1.0)
            and r["correlation_clusters"] == [["A", "B"]],
        ),
        "market_risk.geopolitical_risks": (
            {
                "articles": [
                    {"title": "Trade war tariffs escalate", "summary": ""},
                    {"title": "Markets rally", "summary": ""},
                ]
            },
            # 1 of 2 titles carries a risk keyword -> ratio 0.5 -> score 100
            lambda r: r["score"] == 100.0
            and r["risk_mentions"] == 1
            and r["hotspots"] == ["Trade War"],
        ),
        "market_risk.overall_risk_score": (
            {
                "market_score": 50.0,
                "economic_score": 50.0,
                "sentiment_score": 50.0,
                "correlation_score": 50.0,
                "geopolitical_score": 50.0,
            },
            # weights sum to 1.0 -> overall 50; 40<=50<60 -> moderate
            lambda r: r["score"] == pytest.approx(50.0)
            and r["risk_level"] == "moderate"
            and r["black_swan_prob"] == pytest.approx(50.0 / 3000.0),
        ),
        # --- backtest validation (capabilities/backtest.py) ---
        "backtest.evaluate_strategy_sharpe": (
            {
                "returns": list(
                    np.random.default_rng(11).normal(0.0012, 0.01, 252)
                ),
                "n_trials": 10,
                "periods_per_year": 252,
            },
            # The discriminator: PSR vs 0 is 0.989 (>0.95) but once the
            # expected-max-Sharpe over n_trials=10 is subtracted the DSR drops
            # to 0.764 (<0.95). is_credible follows the DEFLATED Sharpe, not
            # the naive PSR -- a handler that gated credibility on PSR, or that
            # ignored n_trials (DSR==PSR), fails here.
            lambda r: r.psr > 0.95
            and r.dsr < 0.95
            and r.is_credible is False
            and r.dsr < r.psr
            and r.annualized_sharpe > 1.0
            and r.n_obs == 252
            and r.n_trials == 10,
        ),
        "backtest.probability_of_backtest_overfitting": (
            {
                # Strategy A (col 0) strictly dominates B (col 1) in every
                # group, so the in-sample-best is A out-of-sample too: every
                # split's logit is > 0 -> PBO == 0.0 (no overfitting).
                "performance": [[0.002, 0.001]] * 20,
                "n_groups": 10,
            },
            lambda r: r.pbo == 0.0
            and r.n_strategies == 2
            and r.n_combinations > 0,
        ),
        "backtest.backtest_signal": (
            {
                "signal": pd.Series(
                    [1.0, 1.0, 1.0, 1.0, 1.0],
                    index=pd.date_range("2024-01-01", periods=5, freq="D"),
                ),
                "prices": pd.Series(
                    [100.0, 101.0, 102.0, 103.0, 104.0],
                    index=pd.date_range("2024-01-01", periods=5, freq="D"),
                ),
            },
            # Long-only on a rising series. lag=1 shifts the signal off the
            # same bar, so the first bar's position is fillna(0) -> its net
            # return is 0.0 (not dropped: only the last bar's NaN forward
            # return is). 4 net bars survive; total return is positive.
            lambda r: r.n_bars == 4
            and float(r.returns.iloc[0]) == 0.0
            and r.total_return() > 0,
        ),
        "backtest.leakage_probe": (
            {
                "prices": pd.Series(
                    [100.0, 102.0, 100.0, 102.0, 100.0, 102.0, 100.0, 102.0],
                    index=pd.date_range("2024-01-01", periods=8, freq="D"),
                ),
            },
            # Zigzag prices: the perfect-foresight (future-sign) signal is
            # hugely profitable naively (same-bar) but the causal lag destroys
            # the edge -> leak_prevented is True.
            lambda r: r["leak_prevented"] is True
            and r["naive_lookahead_total"] > 0,
        ),
        # --- cross-asset relationships and signal edge (capabilities/crossasset.py) ---
        "crossasset.infer_cycle_phase": (
            {"leaders": {"Financials", "Consumer Discretionary", "Industrials"}},
            lambda r: r == "early_cycle",
        ),
        "crossasset.detect_divergences": (
            {
                "corr_dict": {
                    "SPY": {"VIX": 0.1, "HYG": 0.6},
                    "VIX": {"SPY": 0.1},
                    "HYG": {"SPY": 0.6},
                    "GLD": {"TLT": 0.3},
                    "TLT": {"GLD": 0.3},
                }
            },
            # SPY/VIX expected -0.7, actual +0.1 -> diff +0.8 (>0.5, "high").
            # GLD/TLT and SPY/HYG exactly at norm -> not flagged.
            lambda r: len(r) == 1
            and r[0]["pair"] == "SPY/VIX"
            and r[0]["divergence"] == pytest.approx(0.8)
            and r[0]["significance"] == "high",
        ),
        "crossasset.cross_asset_correlations": (
            {
                "returns_data": {
                    "A": [float(i) for i in range(1, 21)],
                    "B": [float(i) for i in range(1, 21)],
                    "C": [-float(i) for i in range(1, 21)],
                    "D": [2.0 * i for i in range(1, 21)],
                }
            },
            # B=A (corr 1.0); C=-A (corr -1.0); D=2A (corr 1.0).
            lambda r: r["matrix"]["A"]["B"] == pytest.approx(1.0)
            and r["matrix"]["A"]["C"] == pytest.approx(-1.0)
            and r["data_points"] == 20
            and r["divergences"] == [],
        ),
        "crossasset.roro_indicator": (
            # Only VIX present with enough history: 5d return -0.25 -> vix_score
            # clamps to 1.0, weight 0.30 -> composite 0.30 -> RISK_ON.
            {"returns": {"VIX": [0.0] * 5 + [-0.05] * 5}},
            lambda r: r["components"] == {"vix_direction": 1.0}
            and r["score"] == pytest.approx(0.30, abs=0.001)
            and r["classification"] == "RISK_ON",
        ),
        "crossasset.sector_rotation": (
            {
                "sector_prices": {
                    "Technology": [100.0 + 0.5 * i for i in range(20)],
                    "Energy": [50.0] * 20,
                    "Utilities": [100.0 - 0.5 * i for i in range(20)],
                }
            },
            # Tech 100->109.5 -> ret_20d 9.5; Energy flat -> momentum 0.0.
            lambda r: r["leaders"][0]["sector"] == "Technology"
            and r["sectors"]["Technology"]["return_20d"] == 9.5
            and r["sectors"]["Energy"]["momentum_score"] == 0.0,
        ),
        "crossasset.information_coefficient": (
            {
                "panel": pd.DataFrame({
                    "date": [1] * 6 + [2] * 6,
                    "asset": ["a", "b", "c", "d", "e", "f"] * 2,
                    "signal": [1, 2, 3, 4, 5, 6] * 2,
                    "forward_return": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6] * 2,
                }),
                "signal_col": "signal",
                "forward_return_col": "forward_return",
            },
            # Perfect rank match each date -> IC 1.0; 2 periods < 12 -> not sig.
            lambda r: r.mean_ic == pytest.approx(1.0)
            and r.n_periods == 2
            and r.is_significant is False,
        ),
        "crossasset.time_series_ic": (
            {
                "signal": pd.Series([float(i) for i in range(1, 11)]),
                "forward_return": pd.Series([0.1 * i for i in range(1, 11)]),
                "method": "pearson",
            },
            lambda r: r.mean_ic == pytest.approx(1.0)
            and r.n_periods == 10
            and r.positive_ic_rate == 1.0,
        ),
        "crossasset.quantile_analysis": (
            {
                "panel": pd.DataFrame({
                    "date": [1] * 10 + [2] * 10,
                    "asset": [f"a{i}" for i in range(10)] * 2,
                    "signal": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] * 2,
                    "forward_return": [
                        0.01, 0.02, 0.03, 0.04, 0.05,
                        0.06, 0.07, 0.08, 0.09, 0.10,
                    ] * 2,
                }),
                "signal_col": "signal",
                "forward_return_col": "forward_return",
                "n_quantiles": 5,
            },
            # Monotone signal -> top-minus-bottom 0.09-0.01=0.08, monotone 1.0.
            lambda r: r.top_minus_bottom == pytest.approx(0.08)
            and r.monotonicity == pytest.approx(1.0)
            and r.n_periods == 2,
        ),
        "crossasset.evaluate_signal": (
            {
                "panel": _edge_panel,
                "signal_col": "signal",
                "forward_return_col": "forward_return",
            },
            lambda r: r.ic.mean_ic == pytest.approx(1.0)
            and r.ic.is_significant is True
            and r.verdict.startswith("STRONG EDGE"),
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

    def test_claim_types_frozenset_mirrors_the_migration_enum(self):
        """Drift guard: CLAIM_TYPES must equal the claim_type values declared
        across the migration files. The next migration that adds a claim type
        (e.g. a concurrent branch adding 'sahm_rule_signal') will fail this
        test until CLAIM_TYPES is updated -- that is the test working as
        designed, not a defect. The one-line fix: add the new value to
        CLAIM_TYPES in src/omni/capability/extracted.py.
        """
        enum_values = _claim_type_enum_from_migrations()
        assert CLAIM_TYPES == enum_values, (
            "CLAIM_TYPES drifted from the migration-defined claim_type enum.\n"
            f"  in CLAIM_TYPES but not migrations: {CLAIM_TYPES - enum_values}\n"
            f"  in migrations but not CLAIM_TYPES: {enum_values - CLAIM_TYPES}\n"
            "If a concurrent branch added a claim type, add it to CLAIM_TYPES"
            " in src/omni/capability/extracted.py to resolve."
        )

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

    def test_the_market_risk_block_registers_without_a_name_collision(
        self, registry
    ):
        # build_extracted_registry() raises ValueError on a duplicate name, so
        # merely reaching here proves no collision; this asserts the intended
        # set landed intact (guards a merge that drops or duplicates an entry,
        # and a prefix that drifts away from `market_risk.`).
        assert {
            n for n in registry._by_name if n.startswith("market_risk.")
        } == {
            "market_risk.liquidity_risk",
            "market_risk.concentration_risk",
            "market_risk.options_skew",
            "market_risk.breadth",
            "market_risk.growth_risk",
            "market_risk.credit_risk",
            "market_risk.correlation_risks",
            "market_risk.geopolitical_risks",
            "market_risk.overall_risk_score",
        }

    def test_the_backtest_block_registers_without_a_name_collision(
        self, registry
    ):
        # build_extracted_registry() raises ValueError on a duplicate name, so
        # merely reaching here proves no collision; this asserts the intended
        # set landed intact (guards a merge that drops or duplicates an entry,
        # and a prefix that drifts away from `backtest.`).
        assert {n for n in registry._by_name if n.startswith("backtest.")} == {
            "backtest.evaluate_strategy_sharpe",
            "backtest.probability_of_backtest_overfitting",
            "backtest.backtest_signal",
            "backtest.leakage_probe",
        }

    def test_the_crossasset_block_registers_without_a_name_collision(
        self, registry
    ):
        # build_extracted_registry() raises ValueError on a duplicate name, so
        # reaching here proves no collision; this asserts the intended set
        # landed intact (guards a merge that drops or duplicates an entry, and a
        # prefix that drifts away from `crossasset.`).
        assert {
            n for n in registry._by_name if n.startswith("crossasset.")
        } == {
            "crossasset.infer_cycle_phase",
            "crossasset.detect_divergences",
            "crossasset.cross_asset_correlations",
            "crossasset.roro_indicator",
            "crossasset.sector_rotation",
            "crossasset.information_coefficient",
            "crossasset.time_series_ic",
            "crossasset.quantile_analysis",
            "crossasset.evaluate_signal",
        }


class TestInvocation:
    @pytest.mark.parametrize("name", list(_cases().keys()))
    async def test_every_capability_runs_and_produces_its_value(self, registry, name):
        kwargs, predicate = _cases()[name]
        result = await registry.get(name).call(**kwargs)
        assert predicate(result), f"{name} returned an unexpected result: {result!r}"

    async def test_credit_risk_double_registration_resolves_to_same_function(
        self, registry
    ):
        """market_risk.credit_risk is registered in BOTH extracted.py (bound
        to risk.analyze_credit_risk) and orchestrator/analysis.py (whose
        _compute_credit_risk calls the same function via the name-keyed
        declared-argument path). Pin: both paths must produce identical output
        for the same inputs. If someone rebinds one to a different function,
        this test fails -- the two registrations have drifted.

        This imports a private name (_compute_credit_risk) from analysis.py,
        which a concurrent order owns. If that import fails on merge, the
        declared-argument path has been restructured and this pin needs review
        -- the ImportError is the signal, not a defect in the test.
        """
        from omni.capabilities.risk import analyze_credit_risk
        from omni.capability.arguments import Materialized
        from omni.orchestrator.analysis import _compute_credit_risk

        ig = Materialized(value=200.0, claim_ids=(), rows=())
        hy = Materialized(value=700.0, claim_ids=(), rows=())

        via_analysis = await _compute_credit_risk(ig_spread=ig, hy_spread=hy)
        via_extracted = await registry.get("market_risk.credit_risk").call(
            ig_spread=200.0, hy_spread=700.0
        )
        direct = analyze_credit_risk(ig_spread=200.0, hy_spread=700.0)

        assert via_analysis == via_extracted == direct

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

    async def test_growth_risk_recession_field_is_not_interchangeable_with_macro_composite(
        self, registry
    ):
        # QM M2 / QF2: growth_risk embeds a GDP/unemployment recession heuristic
        # that DIVERGES from the registered macro.recession_probability composite
        # on the same state (0.85 vs 1.0). The two must never share an output
        # field name, or a consumer reads them as two estimates of one calibrated
        # quantity and averages them into meaninglessness. macro.recession_probability
        # surfaces its result under the key `probability`; growth_risk must not
        # surface a key that collides with that or with the bare name
        # `recession_probability` (which mirrors the macro capability's own name).
        growth = await registry.get("market_risk.growth_risk").call(
            gdp_growth=-0.5, unemployment=6.0, job_growth=40000
        )
        macro = await registry.get("macro.recession_probability").call(
            yield_curve_inverted=True,
            sahm_triggered=True,
            lei_signals=["negative"],
        )
        assert "probability" not in growth, (
            "growth_risk must not surface a `probability` key; it collides with "
            "macro.recession_probability's output key and implies a calibrated "
            "estimate where there is only a band-add heuristic"
        )
        assert "recession_probability" not in growth, (
            "growth_risk must not surface a `recession_probability` key; the "
            "bare name reads as interchangeable with macro.recession_probability"
        )
        assert "growth_score_recession_heuristic" in growth, (
            "growth_risk must carry its recession heuristic under an explicitly "
            "named, unambiguous field (growth_score_recession_heuristic)"
        )
        # The two numbers really do differ on this state -- the divergence the
        # distinct naming exists to surface, not hide.
        assert growth["growth_score_recession_heuristic"] != pytest.approx(
            macro["probability"]
        )


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

    def test_no_fundamentals_cap_consuming_a_price_is_marked_shareable(
        self, registry
    ):
        # Corrected from test_edgar_sourced_fundamentals_are_shareable, which
        # encoded the leak: the "EDGAR-sourced => shareable" premise ignored the
        # byo_only price these blend. Any fundamentals capability that declares
        # a price_snapshot input must carry the price licence -- flipping one
        # back to shareable must fail here. stress_tests (bar-derived NAV, no
        # declared consume) is covered by its membership in the parametrized
        # ..._inherit_the_price_licence list above.
        for name, cap in registry._by_name.items():
            if name.startswith("fundamentals.") and "price_snapshot" in cap.consumes:
                assert cap.touches_byo is True, (
                    f"{name} consumes a byo_only price but is marked shareable"
                )

    @pytest.mark.parametrize(
        "name",
        [
            "news.aggregate_market_sentiment",
            "news.score_portfolio_impact",
            "news.score_stocktwits_messages",
            "news.stocktwits_sentiment",
        ],
    )
    def test_news_and_sentiment_caps_default_to_private(
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

    @pytest.mark.parametrize(
        "name",
        [
            "microstructure.effective_spread",
            "microstructure.kyle_lambda",
            "microstructure.order_flow_toxicity",
            "execution.implementation_shortfall",
            "execution.benchmark_slippage",
            "execution.vwap_slippage",
            "execution.slippage_summary",
            "execution.identify_outliers",
            "signal_fusion.normalize",
            "signal_fusion.convergence",
            "signal_fusion.conviction",
            "signal_fusion.lead_lag",
        ],
    )
    def test_batch_n3_caps_inherit_their_input_licence(self, registry, name):
        # Microstructure and the fill/bar execution caps compute over price-
        # derived market data; the slippage-summary / outlier / signal-fusion
        # caps take scalars whose licence the descriptor cannot see but which
        # are downstream of price or byo_only perception claims. All twelve
        # default to private until the claim writer proves otherwise.
        assert registry.get(name).touches_byo is True

    @pytest.mark.parametrize(
        "name",
        [
            "market_risk.liquidity_risk",
            "market_risk.concentration_risk",
            "market_risk.options_skew",
            "market_risk.breadth",
            "market_risk.credit_risk",
            "market_risk.correlation_risks",
            "market_risk.geopolitical_risks",
            "market_risk.overall_risk_score",
        ],
    )
    def test_market_risk_caps_over_market_data_inherit_the_input_licence(
        self, registry, name
    ):
        # Quotes / market caps / option IVs / breadth / returns are all
        # price_snapshot-derived (the only producers are the byo_only polygon
        # and coingecko feeds); news articles' producer could be byo (news_api
        # is in the catalog though unwired); credit spreads are commonly bond-
        # price-derived; and the overall composite blends these byo sub-scores.
        # Every one inherits its input's licence, exactly like
        # detect.manipulation -- over-excluded from shared plans rather than
        # risking a leak.
        assert registry.get(name).touches_byo is True

    @pytest.mark.parametrize(
        "name",
        [
            "crossasset.infer_cycle_phase",
            "crossasset.detect_divergences",
            "crossasset.cross_asset_correlations",
            "crossasset.roro_indicator",
            "crossasset.sector_rotation",
            "crossasset.information_coefficient",
            "crossasset.time_series_ic",
            "crossasset.quantile_analysis",
            "crossasset.evaluate_signal",
        ],
    )
    def test_crossasset_caps_inherit_the_price_licence(self, registry, name):
        # Every crossasset analysis runs over price-derived series (returns,
        # sector prices, forward-return panels) or over an already-computed
        # structure built from them (a correlation dict, a set of leader labels
        # ranked from sector prices). price_snapshot is produced only by the
        # byo_only polygon/coingecko feeds, so every output inherits that
        # licence -- including infer_cycle_phase and detect_divergences, which
        # take no claims directly but whose inputs are downstream of prices
        # (matching regime.detect_regime_changes, touches_byo over a label
        # sequence). None may fold into shared coverage.
        assert registry.get(name).touches_byo is True

    def test_no_crossasset_cap_consuming_a_price_is_marked_shareable(
        self, registry
    ):
        # Structural guard for the leak class QN found in fundamentals: any
        # crossasset capability that declares a price_snapshot input must carry
        # the price licence. infer_cycle_phase / detect_divergences consume no
        # claims (their inputs are inter-step values) but are still touches_byo
        # via the parametrized test above; this guard specifically locks the
        # declared-consume side.
        for name, cap in registry._by_name.items():
            if name.startswith("crossasset.") and "price_snapshot" in cap.consumes:
                assert cap.touches_byo is True, (
                    f"{name} consumes a byo_only price but is marked shareable"
                )

    def test_market_risk_growth_score_is_shareable_because_it_runs_on_fred_macro(
        self, registry
    ):
        # analyze_growth_risk takes gdp_growth / unemployment / job_growth --
        # macro_series_point series sourced from FRED (allowed), with no price
        # input -- so it is the one market_risk cap safe to fold into shared
        # coverage. Proven by the closed producer set: the only
        # macro_series_point producer is fred.series (FALLBACK_ALLOWED). Its
        # embedded growth_score_recession_heuristic diverges from
        # macro.recession_probability -- that is a divergence in the NUMBER, not
        # a licence issue; see N6 / QM M2.
        assert registry.get("market_risk.growth_risk").touches_byo is False

    @pytest.mark.parametrize(
        "name",
        [
            "backtest.evaluate_strategy_sharpe",
            "backtest.probability_of_backtest_overfitting",
            "backtest.backtest_signal",
            "backtest.leakage_probe",
        ],
    )
    def test_backtest_caps_over_returns_inherit_the_price_licence(
        self, registry, name
    ):
        # Each runs over a strategy return series or a price series; returns
        # are price_snapshot-derived (the only producers are the byo_only
        # polygon/coingecko feeds), so every one inherits that licence exactly
        # like detect.manipulation -- over-excluded from shared plans rather
        # than risking a leak.
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
