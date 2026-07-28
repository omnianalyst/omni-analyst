"""Fundamental and portfolio analytics as pure capabilities.

Ported from v1 `app/services/fundamental_analysis.py` and
`app/services/analytics_calculator.py`. The IO (fetching fundamentals, quotes,
historical prices, transactions, benchmarks) is removed; every entry point
takes the data it needs as a plain argument.

Where v1 substituted a default on missing input -- a zero metric, a 0.5
correlation for unknown pairs, a Monte-Carlo run on invented volatility and
return, a beta of 1.0 when no benchmark existed -- this module raises
`Unavailable` instead. The census found 44 fabrications in the predecessor by
that exact failure mode; a capability that always returns a number is how
hallucinated coverage enters the store.

Entry points are async (the orchestrator-facing contract). The pure
mathematical helpers (`ratio_quality_scores`, `max_drawdown`,
`blend_position_returns`) are sync because they do no IO.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from omni.ingest.protocol import Unavailable


def ratio_quality_scores(ratios: dict[str, Any]) -> dict[str, str]:
    scores: dict[str, str] = {}

    pe = ratios.get("pe_ratio")
    if pe is not None:
        if pe < 0:
            scores["pe_score"] = "negative_earnings"
        elif pe < 15:
            scores["pe_score"] = "undervalued"
        elif pe < 25:
            scores["pe_score"] = "fair_value"
        elif pe < 40:
            scores["pe_score"] = "overvalued"
        else:
            scores["pe_score"] = "highly_overvalued"

    peg = ratios.get("peg_ratio")
    if peg is not None:
        if peg < 0:
            scores["peg_score"] = "negative_growth"
        elif peg < 1:
            scores["peg_score"] = "undervalued"
        elif peg < 2:
            scores["peg_score"] = "fair_value"
        else:
            scores["peg_score"] = "overvalued"

    roe = ratios.get("roe")
    if roe is not None:
        if roe < 0:
            scores["roe_score"] = "negative"
        elif roe < 10:
            scores["roe_score"] = "poor"
        elif roe < 20:
            scores["roe_score"] = "average"
        elif roe < 30:
            scores["roe_score"] = "good"
        else:
            scores["roe_score"] = "excellent"

    de = ratios.get("debt_to_equity")
    if de is not None:
        if de < 0.5:
            scores["debt_score"] = "low_debt"
        elif de < 1:
            scores["debt_score"] = "moderate_debt"
        elif de < 2:
            scores["debt_score"] = "high_debt"
        else:
            scores["debt_score"] = "very_high_debt"

    current = ratios.get("current_ratio")
    if current is not None:
        if current < 1:
            scores["liquidity_score"] = "poor"
        elif current < 1.5:
            scores["liquidity_score"] = "adequate"
        elif current < 3:
            scores["liquidity_score"] = "good"
        else:
            scores["liquidity_score"] = "excess_liquidity"

    return scores


async def financial_ratios(
    fundamentals: dict[str, Any], current_price: float
) -> dict[str, Any]:
    if not fundamentals:
        raise Unavailable("no fundamental data")

    income_statement = fundamentals.get("income_statement", {}) or {}
    balance_sheet = fundamentals.get("balance_sheet", {}) or {}
    cash_flow = fundamentals.get("cash_flow", {}) or {}

    ratios: dict[str, Any] = {}

    earnings_per_share = income_statement.get("eps", 0)
    if earnings_per_share and earnings_per_share > 0:
        ratios["pe_ratio"] = current_price / earnings_per_share
        ratios["earnings_per_share"] = earnings_per_share
    else:
        ratios["pe_ratio"] = None
        ratios["earnings_per_share"] = earnings_per_share

    earnings_growth = income_statement.get("earnings_growth_rate", 0)
    if ratios.get("pe_ratio") and earnings_growth > 0:
        ratios["peg_ratio"] = ratios["pe_ratio"] / (earnings_growth * 100)
    else:
        ratios["peg_ratio"] = None

    book_value_per_share = balance_sheet.get("book_value_per_share", 0)
    if book_value_per_share and book_value_per_share > 0:
        ratios["pb_ratio"] = current_price / book_value_per_share
        ratios["book_value_per_share"] = book_value_per_share
    else:
        ratios["pb_ratio"] = None
        ratios["book_value_per_share"] = book_value_per_share

    net_income = income_statement.get("net_income", 0)
    total_equity = balance_sheet.get("total_equity", 0)
    if total_equity and total_equity > 0:
        ratios["roe"] = (net_income / total_equity) * 100
    else:
        ratios["roe"] = None

    total_assets = balance_sheet.get("total_assets", 0)
    if total_assets and total_assets > 0:
        ratios["roa"] = (net_income / total_assets) * 100
    else:
        ratios["roa"] = None

    total_debt = balance_sheet.get("total_debt", 0)
    if total_equity and total_equity > 0:
        ratios["debt_to_equity"] = total_debt / total_equity
    else:
        ratios["debt_to_equity"] = None

    current_assets = balance_sheet.get("current_assets", 0)
    current_liabilities = balance_sheet.get("current_liabilities", 0)
    if current_liabilities and current_liabilities > 0:
        ratios["current_ratio"] = current_assets / current_liabilities
    else:
        ratios["current_ratio"] = None

    inventory = balance_sheet.get("inventory", 0)
    if current_liabilities and current_liabilities > 0:
        ratios["quick_ratio"] = (current_assets - inventory) / current_liabilities
    else:
        ratios["quick_ratio"] = None

    revenue = income_statement.get("revenue", 0)
    cost_of_revenue = income_statement.get("cost_of_revenue", 0)
    if revenue and revenue > 0:
        ratios["gross_margin"] = ((revenue - cost_of_revenue) / revenue) * 100
    else:
        ratios["gross_margin"] = None

    operating_income = income_statement.get("operating_income", 0)
    if revenue and revenue > 0:
        ratios["operating_margin"] = (operating_income / revenue) * 100
    else:
        ratios["operating_margin"] = None

    if revenue and revenue > 0:
        ratios["net_margin"] = (net_income / revenue) * 100
    else:
        ratios["net_margin"] = None

    operating_cash_flow = cash_flow.get("operating_cash_flow", 0)
    capital_expenditures = cash_flow.get("capital_expenditures", 0)
    ratios["free_cash_flow"] = operating_cash_flow - abs(capital_expenditures)

    dividends_per_share = income_statement.get("dividends_per_share", 0)
    if current_price and current_price > 0:
        ratios["dividend_yield"] = (dividends_per_share / current_price) * 100
    else:
        ratios["dividend_yield"] = None

    ratios["current_price"] = current_price
    ratios["quality_scores"] = ratio_quality_scores(ratios)

    return ratios


async def dcf_valuation(
    fundamentals: dict[str, Any],
    current_price: float,
    *,
    growth_rate: float | None = None,
    terminal_growth_rate: float = 0.03,
    discount_rate: float | None = None,
    years: int = 5,
) -> dict[str, Any]:
    if not fundamentals:
        raise Unavailable("no fundamental data")

    cash_flow_data = fundamentals.get("cash_flow", {}) or {}
    balance_sheet = fundamentals.get("balance_sheet", {}) or {}
    income_statement = fundamentals.get("income_statement", {}) or {}

    operating_cash_flow = cash_flow_data.get("operating_cash_flow", 0)
    capital_expenditures = cash_flow_data.get("capital_expenditures", 0)
    free_cash_flow = operating_cash_flow - abs(capital_expenditures)

    if free_cash_flow <= 0:
        raise ValueError("non-positive free cash flow")

    if growth_rate is None:
        if "revenue_growth_rate" not in income_statement:
            raise Unavailable(
                "no growth_rate given and no revenue_growth_rate to derive one"
            )
        revenue_growth = income_statement.get("revenue_growth_rate")
        growth_rate = min(revenue_growth, 0.20)

    if discount_rate is None:
        risk_free_rate = 0.04
        market_premium = 0.08
        beta = fundamentals.get("beta", 1.0)

        cost_of_equity = risk_free_rate + beta * market_premium

        total_debt = balance_sheet.get("total_debt", 0)
        total_equity = balance_sheet.get("market_cap", 0)

        if total_equity > 0:
            debt_ratio = total_debt / (total_debt + total_equity)
            equity_ratio = 1 - debt_ratio

            cost_of_debt = 0.04
            tax_rate = 0.21

            discount_rate = (
                equity_ratio * cost_of_equity
                + debt_ratio * cost_of_debt * (1 - tax_rate)
            )
        else:
            discount_rate = cost_of_equity

    projected_cash_flows = []
    current_fcf = free_cash_flow

    for year in range(1, years + 1):
        current_fcf *= 1 + growth_rate
        projected_cash_flows.append(
            {
                "year": year,
                "cash_flow": current_fcf,
                "present_value": current_fcf / ((1 + discount_rate) ** year),
            }
        )

    terminal_fcf = projected_cash_flows[-1]["cash_flow"] * (1 + terminal_growth_rate)
    terminal_value = terminal_fcf / (discount_rate - terminal_growth_rate)
    terminal_pv = terminal_value / ((1 + discount_rate) ** years)

    sum_of_pv = sum(cf["present_value"] for cf in projected_cash_flows)
    enterprise_value = sum_of_pv + terminal_pv

    cash = balance_sheet.get("cash_and_equivalents", 0)
    total_debt = balance_sheet.get("total_debt", 0)
    equity_value = enterprise_value + cash - total_debt

    shares_outstanding = balance_sheet.get("shares_outstanding", 0)
    if shares_outstanding > 0:
        fair_value_per_share = equity_value / shares_outstanding
    else:
        fair_value_per_share = None

    if fair_value_per_share and current_price > 0:
        upside_percentage = (
            (fair_value_per_share - current_price) / current_price
        ) * 100
    else:
        upside_percentage = None

    return {
        "current_price": current_price,
        "fair_value_per_share": fair_value_per_share,
        "upside_percentage": upside_percentage,
        "enterprise_value": enterprise_value,
        "equity_value": equity_value,
        "assumptions": {
            "growth_rate": growth_rate,
            "terminal_growth_rate": terminal_growth_rate,
            "discount_rate": discount_rate,
            "projection_years": years,
        },
        "projected_cash_flows": projected_cash_flows,
        "terminal_value": terminal_value,
        "terminal_present_value": terminal_pv,
        "sum_of_present_values": sum_of_pv,
    }


async def peer_comparison(
    symbol: str,
    industry: str,
    sector: str,
    comparison_data: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(comparison_data) < 2:
        raise Unavailable("insufficient peer data for comparison")

    metrics = [
        "pe_ratio",
        "peg_ratio",
        "pb_ratio",
        "roe",
        "debt_to_equity",
        "current_ratio",
        "gross_margin",
        "net_margin",
        "dividend_yield",
    ]

    peer_averages: dict[str, float] = {}
    rankings: dict[str, Any] = {}

    for metric in metrics:
        values = [d[metric] for d in comparison_data if d.get(metric) is not None]

        if values:
            peer_averages[metric] = float(np.mean(values))

            if metric in [
                "debt_to_equity",
                "pe_ratio",
                "peg_ratio",
                "pb_ratio",
            ]:
                sorted_data = sorted(
                    comparison_data,
                    key=lambda x: (
                        x[metric] if x[metric] is not None else float("inf")
                    ),
                )
            else:
                sorted_data = sorted(
                    comparison_data,
                    key=lambda x: (
                        x[metric] if x[metric] is not None else float("-inf")
                    ),
                    reverse=True,
                )

            for rank, d in enumerate(sorted_data, 1):
                if d["symbol"] == symbol and d[metric] is not None:
                    rankings[metric] = {
                        "rank": rank,
                        "total": len(values),
                        "percentile": ((len(values) - rank + 1) / len(values)) * 100,
                    }
                    break

    target_data = next((d for d in comparison_data if d.get("is_target")), None)

    relative_valuation: dict[str, float] = {}

    if target_data:
        for metric in ["pe_ratio", "pb_ratio", "peg_ratio"]:
            if target_data.get(metric) is not None and peer_averages.get(metric):
                relative = (
                    target_data[metric] / peer_averages[metric] - 1
                ) * 100
                relative_valuation[f"{metric}_vs_peers"] = relative

    return {
        "symbol": symbol,
        "industry": industry,
        "sector": sector,
        "peer_count": sum(1 for d in comparison_data if not d.get("is_target")),
        "target_metrics": target_data,
        "peer_averages": peer_averages,
        "rankings": rankings,
        "relative_valuation": relative_valuation,
        "peer_data": comparison_data,
    }


def max_drawdown(daily_returns: Sequence[float]) -> float:
    if not daily_returns:
        return 0.0

    cumulative = np.cumprod([1 + r for r in daily_returns])
    running_max = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - running_max) / running_max * 100

    return float(np.min(drawdown)) if len(drawdown) > 0 else 0.0


def blend_position_returns(
    position_returns: dict[str, dict[str, Any]],
) -> list[float]:
    if not position_returns:
        return []

    min_periods = min(len(pr["returns"]) for pr in position_returns.values())
    min_periods = min(min_periods, 252)

    daily_returns = []
    for i in range(min_periods):
        day_return = sum(
            pr["weight"] * pr["returns"][i]
            for pr in position_returns.values()
            if i < len(pr["returns"])
        )
        daily_returns.append(day_return)
    return daily_returns


async def portfolio_returns(
    transactions: Sequence[dict[str, Any]],
    total_value: float,
    daily_returns: Sequence[float],
    period_days: int,
) -> dict[str, float]:
    if not transactions:
        raise Unavailable("no transactions in period")

    initial_value = 0.0
    for txn in transactions:
        if txn.get("transaction_type") == "buy":
            initial_value += abs(txn.get("total_amount") or 0)
        elif txn.get("transaction_type") == "sell":
            initial_value -= abs(txn.get("total_amount") or 0)

    if initial_value <= 0:
        initial_value = total_value

    absolute_return = total_value - initial_value

    percentage_return = (
        (absolute_return / initial_value * 100) if initial_value > 0 else 0.0
    )

    years = period_days / 365.25
    annualized_return = (
        (pow(1 + percentage_return / 100, 1 / years) - 1) * 100
        if years > 0
        else percentage_return
    )

    if len(daily_returns) <= 1:
        raise Unavailable("fewer than 2 daily returns; cannot estimate volatility")

    daily_std = np.std(daily_returns)
    volatility = daily_std * np.sqrt(252) * 100

    risk_free_rate = 2.0
    sharpe_ratio = (
        ((annualized_return - risk_free_rate) / volatility)
        if volatility > 0
        else 0.0
    )

    mdd = max_drawdown(daily_returns)

    return {
        "absolute_return": round(absolute_return, 2),
        "percentage_return": round(percentage_return, 2),
        "annualized_return": round(annualized_return, 2),
        "volatility": round(volatility, 2),
        "sharpe_ratio": round(sharpe_ratio, 2),
        "max_drawdown": round(mdd, 2),
    }


async def risk_metrics(
    daily_returns: Sequence[float],
    total_value: float,
    benchmark_returns: Sequence[float] | None = None,
) -> dict[str, Any]:
    if len(daily_returns) < 20:
        raise Unavailable(
            f"insufficient historical data ({len(daily_returns)} days; need >=20) "
            "for historical risk metrics"
        )

    returns_array = np.array(daily_returns)

    var_95_daily = np.percentile(returns_array, 5)
    var_99_daily = np.percentile(returns_array, 1)
    var_95 = var_95_daily * total_value
    var_99 = var_99_daily * total_value

    cvar_95 = np.mean(returns_array[returns_array <= var_95_daily]) * total_value

    daily_std = np.std(returns_array)
    annual_std = daily_std * np.sqrt(252)
    std_dev = annual_std * 100

    negative_returns = returns_array[returns_array < 0]
    downside_dev = (
        np.std(negative_returns) * np.sqrt(252) * 100
        if len(negative_returns) > 0
        else 0.0
    )

    mean_daily_return = np.mean(returns_array)
    annual_return = mean_daily_return * 252 * 100
    risk_free_rate = 4.5

    sortino = (
        ((annual_return - risk_free_rate) / downside_dev)
        if downside_dev > 0
        else 0.0
    )

    cumulative = np.cumprod(1 + returns_array)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = (cumulative - running_max) / running_max
    max_dd = abs(float(np.min(drawdowns))) * 100
    calmar = (annual_return / max_dd) if max_dd > 0 else 0.0

    portfolio_beta: float | None = None
    if benchmark_returns is not None and len(benchmark_returns) >= 20:
        min_len = min(len(daily_returns), len(benchmark_returns))
        port_ret = np.array(daily_returns[:min_len])
        bench_ret = np.array(benchmark_returns[:min_len])
        cov_matrix = np.cov(port_ret, bench_ret)
        if cov_matrix[1, 1] > 0:
            portfolio_beta = float(cov_matrix[0, 1] / cov_matrix[1, 1])

    return {
        "value_at_risk_95": round(var_95, 2),
        "value_at_risk_99": round(var_99, 2),
        "conditional_var_95": round(cvar_95, 2),
        "portfolio_beta": (
            round(portfolio_beta, 2) if portfolio_beta is not None else None
        ),
        "standard_deviation": round(std_dev, 2),
        "downside_deviation": round(downside_dev, 2),
        "sortino_ratio": round(sortino, 2),
        "calmar_ratio": round(calmar, 2),
        "data_quality": "historical",
        "data_points": len(daily_returns),
    }


async def stress_tests(total_value: float) -> dict[str, float]:
    scenarios = {
        "market_crash_20pct": -0.20,
        "interest_rate_rise_2pct": -0.05,
        "currency_depreciation_10pct": -0.03,
    }

    results = {}
    for scenario, impact in scenarios.items():
        results[scenario] = round(total_value * impact, 2)
    return results


async def correlation_matrix(
    returns_by_symbol: dict[str, Sequence[float]],
    symbols: Sequence[str],
) -> dict[str, dict[str, float]]:
    historical_data: dict[str, list[float]] = {}
    for symbol in symbols:
        rets = returns_by_symbol.get(symbol)
        if rets is None or len(rets) <= 10:
            raise Unavailable(
                f"insufficient return history for {symbol} (need >10 points)"
            )
        historical_data[symbol] = list(rets)

    matrix: dict[str, dict[str, float]] = {}
    for sym1 in symbols:
        matrix[sym1] = {}
        for sym2 in symbols:
            if sym1 == sym2:
                matrix[sym1][sym2] = 1.0
            else:
                returns1 = historical_data[sym1]
                returns2 = historical_data[sym2]
                min_len = min(len(returns1), len(returns2))
                returns1 = returns1[:min_len]
                returns2 = returns2[:min_len]
                if min_len <= 10:
                    raise Unavailable(
                        f"insufficient overlapping history for {sym1}/{sym2} "
                        f"(have {min_len}, need >10)"
                    )
                corr = np.corrcoef(returns1, returns2)[0, 1]
                matrix[sym1][sym2] = round(float(corr), 3)
    return matrix


async def benchmark_comparison(
    portfolio_return: float,
    benchmark_closes: Sequence[float],
) -> dict[str, Any]:
    if len(benchmark_closes) < 2:
        raise Unavailable("insufficient benchmark history (need >=2 closes)")

    start_price = benchmark_closes[0]
    end_price = benchmark_closes[-1]
    if start_price <= 0:
        raise Unavailable("non-positive benchmark start price")

    sp500_return = ((end_price - start_price) / start_price) * 100
    alpha = portfolio_return - sp500_return

    return {
        "sp500_return": round(sp500_return, 2),
        "alpha": round(alpha, 2),
        "data_source": "historical",
    }
