"""ETF versus constituent-selection portfolio experiment.

The experiment is deliberately a portfolio comparison, not a claim that a
backtest is investable.  It receives a caller-supplied adjusted-close panel and
never fetches data itself.  Scores are calculated at a decision close, target
weights take effect on the following session, and turnover is charged before
that following session's return.

Historical membership snapshots are an input the current deployment does not
yet own.  A caller using one static constituent set must label the result
``current_membership_preview``; this module carries that limitation into every
result so a survivorship-biased preview cannot be mistaken for a point-in-time
backtest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

MembershipMode = Literal["point_in_time", "current_membership_preview"]
TRADING_DAYS = 252
_ZERO_VOLATILITY_ATOL = 1e-12


@dataclass(frozen=True)
class PortfolioMetrics:
    total_return_pct: float
    cagr_pct: float
    volatility_pct: float
    sharpe: float | None
    max_drawdown_pct: float
    turnover: float
    modelled_cost_pct: float
    tracking_error_pct: float
    information_ratio: float | None
    sessions: int


@dataclass(frozen=True)
class StrategyPath:
    name: str
    values: pd.Series
    returns: pd.Series
    targets: pd.DataFrame
    metrics: PortfolioMetrics


@dataclass(frozen=True)
class Experiment:
    etf_symbol: str
    membership_mode: MembershipMode
    decision_rule: str
    warnings: tuple[str, ...]
    strategies: dict[str, StrategyPath]


def _percentile(series: pd.Series, *, higher_is_better: bool = True) -> pd.Series:
    ranked = series.rank(method="average", pct=True)
    return ranked if higher_is_better else 1.0 - ranked + (1.0 / len(ranked))


def price_quality_scores(history: pd.DataFrame) -> pd.Series:
    """Score assets from information knowable at the final history row.

    The rule rewards six-month and three-month momentum, stable daily returns,
    shallow six-month drawdown, and the fraction of positive one-month windows.
    It is intentionally simpler than Discover's broad-asset score because the
    company store currently has only two years of price history.
    """
    if len(history) < 127:
        return pd.Series(dtype=float)
    complete = history.columns[history.tail(127).notna().all()]
    if len(complete) < 2:
        return pd.Series(dtype=float)
    prices = history.loc[:, complete].tail(127)
    daily = prices.pct_change(fill_method=None).dropna(how="all")
    momentum_126 = prices.iloc[-1] / prices.iloc[0] - 1.0
    momentum_63 = prices.iloc[-1] / prices.iloc[-64] - 1.0
    volatility = daily.tail(63).std(ddof=1) * math.sqrt(TRADING_DAYS)
    drawdown = prices.div(prices.cummax()).sub(1.0).min()
    month_returns = prices.pct_change(21, fill_method=None)
    consistency = (month_returns > 0).sum() / month_returns.notna().sum()
    measures = pd.DataFrame({
        "momentum_126": momentum_126,
        "momentum_63": momentum_63,
        "volatility": volatility,
        "drawdown": drawdown,
        "consistency": consistency,
    }).replace([np.inf, -np.inf], np.nan).dropna()
    if len(measures) < 2:
        return pd.Series(dtype=float)
    return (
        _percentile(measures["momentum_126"]) * 0.35
        + _percentile(measures["momentum_63"]) * 0.20
        + _percentile(measures["volatility"], higher_is_better=False) * 0.20
        + _percentile(measures["drawdown"]) * 0.15
        + _percentile(measures["consistency"]) * 0.10
    ).sort_values(ascending=False)


def turnover_between(current: pd.Series, target: pd.Series) -> float:
    """One-way turnover moving from one weight vector to another.

    Public because the forward shadow book charges the same quantity against
    the same cost assumption, and two implementations of turnover would let the
    live record and the backtest disagree about what a rebalance cost while
    both looked correct.
    """
    names = current.index.union(target.index)
    current = current.reindex(names, fill_value=0.0)
    target = target.reindex(names, fill_value=0.0)
    # Include the cash leg. Moving from cash to a fully-invested portfolio is
    # 100% turnover, not 50%; subsequent fully-invested changes reduce to the
    # usual half sum of absolute weight changes.
    current_cash = max(0.0, 1.0 - float(current.sum()))
    target_cash = max(0.0, 1.0 - float(target.sum()))
    return 0.5 * (float((target - current).abs().sum()) + abs(target_cash - current_cash))


_turnover = turnover_between


def _simulate(
    returns: pd.DataFrame,
    targets: dict[pd.Timestamp, pd.Series],
    *,
    cost_bps: float,
) -> tuple[pd.Series, pd.Series, pd.DataFrame, float, float]:
    if not targets:
        raise ValueError("strategy produced no rebalance targets")
    first_decision = min(targets)
    dates = returns.index[returns.index > first_decision]
    if dates.empty:
        raise ValueError("no return sessions follow the first decision")
    weights = pd.Series(dtype=float)
    value = 1.0
    values: list[float] = []
    realised: list[float] = []
    value_dates: list[pd.Timestamp] = []
    total_turnover = 0.0
    total_cost = 0.0

    for date in dates:
        prior_dates = [decision for decision in targets if decision < date]
        decision = max(prior_dates) if prior_dates else None
        if decision is not None and (not value_dates or decision >= value_dates[-1]):
            target = targets[decision]
            turnover = _turnover(weights, target)
            charge = value * turnover * cost_bps / 10_000.0
            value -= charge
            total_turnover += turnover
            total_cost += charge
            weights = target.copy()

        row = returns.loc[date].reindex(weights.index)
        if row.isna().any():
            missing = ", ".join(row.index[row.isna()][:5])
            raise ValueError(f"missing held return on {date.date()}: {missing}")
        portfolio_return = float(weights @ row) if not weights.empty else 0.0
        value *= 1.0 + portfolio_return
        asset_growth = weights * (1.0 + row)
        weights = asset_growth / float(asset_growth.sum())
        values.append(value)
        realised.append(portfolio_return)
        value_dates.append(date)

    target_frame = pd.DataFrame(targets).T.sort_index().fillna(0.0)
    return (
        pd.Series(values, index=value_dates, name="value"),
        pd.Series(realised, index=value_dates, name="return"),
        target_frame,
        total_turnover,
        total_cost,
    )


def _annualized_volatility_and_ratio(series: pd.Series) -> tuple[float, float | None]:
    observations = series.to_numpy(dtype=float)
    if len(observations) < 2 or not np.isfinite(observations).all():
        return float("nan"), None
    daily_volatility = float(observations.std(ddof=1))
    annualized_volatility = daily_volatility * math.sqrt(TRADING_DAYS)
    if np.isclose(daily_volatility, 0.0, rtol=0.0, atol=_ZERO_VOLATILITY_ATOL):
        return annualized_volatility, None
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        ratio = float(np.divide(observations.mean(), daily_volatility) * math.sqrt(TRADING_DAYS))
    return annualized_volatility, ratio if np.isfinite(ratio) else None


def _metrics(
    values: pd.Series,
    returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    turnover: float,
    modelled_cost: float,
) -> PortfolioMetrics:
    aligned = pd.concat([returns, benchmark_returns], axis=1, join="inner").dropna()
    aligned.columns = ["strategy", "benchmark"]
    sessions = len(returns)
    years = sessions / TRADING_DAYS
    total = float(values.iloc[-1] - 1.0)
    cagr = float(values.iloc[-1] ** (1.0 / years) - 1.0)
    volatility, sharpe = _annualized_volatility_and_ratio(returns)
    drawdown = values.div(values.cummax()).sub(1.0)
    active = aligned["strategy"] - aligned["benchmark"]
    tracking, information = _annualized_volatility_and_ratio(active)
    return PortfolioMetrics(
        total_return_pct=total * 100.0,
        cagr_pct=cagr * 100.0,
        volatility_pct=volatility * 100.0,
        sharpe=sharpe,
        max_drawdown_pct=float(drawdown.min()) * 100.0,
        turnover=turnover,
        modelled_cost_pct=modelled_cost * 100.0,
        tracking_error_pct=tracking * 100.0,
        information_ratio=information,
        sessions=sessions,
    )


def run_experiment(
    prices: pd.DataFrame,
    *,
    etf_symbol: str,
    constituents: list[str],
    membership_mode: MembershipMode,
    top_n: int = 10,
    hybrid_active_weight: float = 0.20,
    rebalance_sessions: int = 21,
    warmup_sessions: int = 126,
    constituent_cost_bps: float = 20.0,
    etf_spread_bps: float = 2.0,
    etf_expense_bps: float = 10.0,
) -> Experiment:
    """Compare ETF, equal-weight, ranked selection, and ETF/ranked hybrid."""
    if membership_mode not in ("point_in_time", "current_membership_preview"):
        raise ValueError("membership_mode must state whether membership is point-in-time")
    if not 0.0 <= hybrid_active_weight <= 1.0:
        raise ValueError("hybrid_active_weight must be in [0, 1]")
    if top_n < 2:
        raise ValueError("top_n must be at least 2")
    symbols = [etf_symbol, *dict.fromkeys(constituents)]
    panel = prices.reindex(columns=symbols).sort_index()
    panel = panel.loc[~panel.index.duplicated(keep="last")]
    # The source panel is a union of every symbol's timestamps. An ETF's own
    # observed sessions define its comparison calendar; requiring it to print
    # on a timestamp that exists only because some other security printed would
    # falsely reject an otherwise complete ETF series. A constituent's isolated
    # gap is marked at its last observable close for at most three ETF sessions
    # (a trading halt or provider hole has zero observable price return). Longer
    # gaps remain NaN and remove the name from scoring rather than inventing a
    # liquidation value.
    panel = panel.loc[panel[etf_symbol].notna()]
    constituent_gap_count = int(panel[constituents].isna().sum().sum())
    panel.loc[:, constituents] = panel[constituents].ffill(limit=3)
    if len(panel) <= warmup_sessions + rebalance_sessions:
        raise ValueError("price history is too short for warmup plus one rebalance period")
    if panel[etf_symbol].isna().any():
        raise ValueError(f"ETF {etf_symbol} has missing prices")
    daily = panel.pct_change(fill_method=None)
    decisions = list(
        panel.index[warmup_sessions:-1:rebalance_sessions]
    )
    equal_targets: dict[pd.Timestamp, pd.Series] = {}
    ranked_targets: dict[pd.Timestamp, pd.Series] = {}
    hybrid_targets: dict[pd.Timestamp, pd.Series] = {}
    etf_targets: dict[pd.Timestamp, pd.Series] = {}

    for decision in decisions:
        history = panel.loc[:decision, constituents]
        scores = price_quality_scores(history)
        if len(scores) < 2:
            continue
        eligible = list(scores.index)
        equal = pd.Series(1.0 / len(eligible), index=eligible)
        selected = list(scores.index[: min(top_n, len(scores))])
        ranked = pd.Series(1.0 / len(selected), index=selected)
        hybrid = ranked * hybrid_active_weight
        hybrid.loc[etf_symbol] = 1.0 - hybrid_active_weight
        equal_targets[decision] = equal
        ranked_targets[decision] = ranked
        hybrid_targets[decision] = hybrid
        etf_targets[decision] = pd.Series({etf_symbol: 1.0})

    raw: dict[str, tuple[pd.Series, pd.Series, pd.DataFrame, float, float]] = {}
    raw["etf"] = _simulate(daily, etf_targets, cost_bps=etf_spread_bps)
    raw["equal_weight"] = _simulate(
        daily, equal_targets, cost_bps=constituent_cost_bps,
    )
    raw["ranked_top_n"] = _simulate(
        daily, ranked_targets, cost_bps=constituent_cost_bps,
    )
    raw["hybrid"] = _simulate(
        daily, hybrid_targets, cost_bps=constituent_cost_bps,
    )

    # Expense ratio is a daily drag on the ETF-containing paths. Apply it to
    # both value and return series after execution costs have been simulated.
    daily_expense = etf_expense_bps / 10_000.0 / TRADING_DAYS
    for name, exposure in (("etf", 1.0), ("hybrid", 1.0 - hybrid_active_weight)):
        values, returns, targets, turnover, cost = raw[name]
        adjusted_returns = returns - daily_expense * exposure
        initial = float(values.iloc[0] / (1.0 + returns.iloc[0]))
        adjusted_values = initial * (1.0 + adjusted_returns).cumprod()
        raw[name] = adjusted_values, adjusted_returns, targets, turnover, cost

    benchmark_returns = raw["etf"][1]
    strategies: dict[str, StrategyPath] = {}
    for name, (values, returns, targets, turnover, cost) in raw.items():
        strategies[name] = StrategyPath(
            name=name,
            values=values,
            returns=returns,
            targets=targets,
            metrics=_metrics(
                values, returns, benchmark_returns,
                turnover=turnover, modelled_cost=cost,
            ),
        )

    warnings = [
        "Historical result uses adjusted closes; taxes and account-specific constraints are excluded.",
        "The ranking rule is a price-only research rule, not the full Omni evidence score.",
    ]
    if constituent_gap_count:
        warnings.append(
            "Isolated constituent price gaps are marked at the last close for at most three ETF sessions."
        )
    if membership_mode == "current_membership_preview":
        warnings.insert(
            0,
            "Current constituents are applied to earlier dates; survivorship bias makes this exploratory only.",
        )
    return Experiment(
        etf_symbol=etf_symbol,
        membership_mode=membership_mode,
        decision_rule="close at t; rebalance and charge costs before return at t+1",
        warnings=tuple(warnings),
        strategies=strategies,
    )


__all__ = [
    "Experiment",
    "PortfolioMetrics",
    "StrategyPath",
    "price_quality_scores",
    "run_experiment",
    "turnover_between",
]
