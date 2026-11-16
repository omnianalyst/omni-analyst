"""Allocation across ETFs, replayed on history.

The question `docs/ETF_PORTFOLIO_EXPERIMENT.md` answered was whether picking
*constituents* inside a sector beats the sector ETF. It does not: 3 of 9
sectors, median excess CAGR -2.70%. This asks a different question -- how to
weight the sector ETFs themselves -- and reuses that experiment's simulator so
the two are measured the same way.

It is the backward half of the same work the shadow book does forward. The
shadow book is the half that will eventually be evidence; this one exists to
say whether any of these rules is worth the shadow book's time, and it inherits
every limitation a two-year panel imposes.

**One bias the constituent experiment had, this one does not.** That experiment
applied today's index membership backward, so companies dropped from the index
-- disproportionately the ones that did badly -- were invisible. The eleven SPDR
sector ETFs have all existed across this window, so their membership is not
being reconstructed at all. Two-year length, one regime, and the absence of a
holdout remain.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from omni.research.allocation import (
    MIN_HISTORY_SESSIONS,
    Allocation,
    AllocationRefused,
)
from omni.research.etf_replication import TRADING_DAYS, turnover_between

# How often the rule is allowed to restate its weights.
#
# `static` decides once and never again -- the honest floor, because a rule that
# cannot beat its own first decision is paying turnover for nothing. `quarterly`
# is 63 sessions. `threshold` restates only when drift takes the book far enough
# from target to matter, which is the cadence that usually wins on cost and the
# one most likely to be omitted because it is fiddlier to implement.
CADENCES = ("static", "quarterly", "threshold")
QUARTERLY_SESSIONS = 63

# One-way drift, summed over names, past which a threshold rule rebalances.
DRIFT_THRESHOLD = 0.10

# The Sharpe difference against the baseline is tested with a paired circular
# block bootstrap: both return series are resampled on the SAME blocks, so the
# correlation between a book and its benchmark -- most of what makes the
# difference measurable at all -- survives, and the blocks keep the serial
# dependence a day-by-day resample would destroy.
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_BLOCK_SESSIONS = 21
BOOTSTRAP_SEED = 0
MIN_BOOTSTRAP_SESSIONS = 60
_ZERO_VOLATILITY_ATOL = 1e-9


@dataclass(frozen=True)
class AllocationResult:
    book: str
    cadence: str
    total_return_pct: float
    cagr_pct: float
    volatility_pct: float
    sharpe: float
    max_drawdown_pct: float
    turnover: float
    modelled_cost_pct: float
    rebalances: int
    sessions: int
    excess_cagr_pct: float
    excess_sharpe: float
    excess_max_drawdown_pct: float
    # Two-sided bootstrap p-value of `excess_sharpe` against zero. None on the
    # baseline itself, and None when the difference cannot be tested (too few
    # sessions, or a series with no variation) -- never a substituted 1.0.
    excess_sharpe_p_value: float | None = None
    cash_weight_mean: float = 0.0


@dataclass(frozen=True)
class AllocationExperiment:
    benchmark: str
    universe: list[str]
    first_session: str
    last_session: str
    cost_bps: float
    warnings: tuple[str, ...]
    baseline: AllocationResult
    results: list[AllocationResult]
    warmup_sessions: int = MIN_HISTORY_SESSIONS
    cash_rate_annual_pct: float | None = None


def _drift(current: pd.Series, target: pd.Series) -> float:
    names = current.index.union(target.index)
    return float(
        (current.reindex(names, fill_value=0.0) - target.reindex(names, fill_value=0.0))
        .abs()
        .sum()
    )


def _run(
    panel: pd.DataFrame,
    rule: Callable[..., Allocation],
    *,
    universe: list[str],
    benchmark: str,
    cadence: str,
    cost_bps: float,
    warmup_sessions: int = MIN_HISTORY_SESSIONS,
    cash_rate_daily: float = 0.0,
) -> tuple[pd.Series, float, float, int, float]:
    """Replay one rule at one cadence.

    The decision is taken on the close at `t` from history up to and including
    `t`, and the weights take effect at `t+1` with the cost charged first --
    the same rule `etf_replication` states, so the two experiments' numbers can
    be put next to each other.
    """
    returns = panel.pct_change(fill_method=None)
    sessions = panel.index[warmup_sessions:]
    if len(sessions) < 2:
        raise AllocationRefused(
            f"{len(panel)} sessions leaves {len(sessions)} after the "
            f"{warmup_sessions}-session warmup; a rule needs at least two "
            f"to produce a return"
        )

    weights = pd.Series(dtype=float)
    target = pd.Series(dtype=float)
    value = 1.0
    values: list[float] = []
    dates: list[pd.Timestamp] = []
    total_turnover = 0.0
    total_cost = 0.0
    rebalances = 0
    since_rebalance = 0
    decided = False
    cash_weights: list[float] = []

    for position, date in enumerate(sessions[:-1]):
        history = panel.loc[:date]
        due = (
            not decided
            or (cadence == "quarterly" and since_rebalance >= QUARTERLY_SESSIONS)
            or (
                cadence == "threshold"
                and decided
                and _drift(weights, target) >= DRIFT_THRESHOLD
            )
        )
        if due:
            allocation = rule(history, universe, benchmark=benchmark)
            target = pd.Series(allocation.weights, dtype=float)
            moved = turnover_between(weights, target)
            charge = value * moved * cost_bps / 10_000.0
            value -= charge
            total_turnover += moved
            total_cost += charge
            weights = target.copy()
            rebalances += 1
            since_rebalance = 0
            decided = True

        following = sessions[position + 1]
        row = returns.loc[following].reindex(weights.index)
        if row.isna().any():
            missing = ", ".join(row.index[row.isna()][:5])
            raise AllocationRefused(
                f"missing return for {missing} on {following.date()}; a held "
                f"name without a mark cannot be scored as flat"
            )
        # Weights that sum below 1.0 hold the remainder in cash. Renormalising
        # the invested names to 1.0 after one session -- what this did before --
        # silently converted a long/flat rule into a fully invested one.
        cash = max(0.0, 1.0 - float(weights.sum()))
        cash_weights.append(cash)
        grown = weights * (1.0 + row)
        total = float(grown.sum()) + cash * (1.0 + cash_rate_daily)
        value *= total
        weights = grown / total
        values.append(value)
        dates.append(following)
        since_rebalance += 1

    return (
        pd.Series(values, index=dates),
        total_turnover,
        total_cost,
        rebalances,
        float(np.mean(cash_weights)) if cash_weights else 0.0,
    )


def _sharpe(excess: np.ndarray) -> float | None:
    """Annualised Sharpe of a series of excess returns, or None if undefined."""
    if len(excess) < 2 or not np.isfinite(excess).all():
        return None
    sigma = float(excess.std(ddof=1))
    if sigma * math.sqrt(TRADING_DAYS) <= _ZERO_VOLATILITY_ATOL:
        return None
    return float(excess.mean() / sigma * math.sqrt(TRADING_DAYS))


def sharpe_difference_p_value(
    strategy: pd.Series,
    baseline: pd.Series,
    *,
    draws: int = BOOTSTRAP_DRAWS,
    block: int = BOOTSTRAP_BLOCK_SESSIONS,
    seed: int = BOOTSTRAP_SEED,
) -> float | None:
    """Two-sided p-value that two paired return series share one Sharpe.

    Paired circular block bootstrap, centred on the observed difference: the
    resampled differences describe the sampling noise around the observed one,
    and the p-value is how often noise alone is at least as large as it.
    """
    paired = pd.concat([strategy, baseline], axis=1, join="inner").dropna()
    n = len(paired)
    if n < MIN_BOOTSTRAP_SESSIONS:
        return None
    a = paired.iloc[:, 0].to_numpy(dtype=float)
    b = paired.iloc[:, 1].to_numpy(dtype=float)
    sharpe_a, sharpe_b = _sharpe(a), _sharpe(b)
    if sharpe_a is None or sharpe_b is None:
        return None
    observed = sharpe_a - sharpe_b
    rng = np.random.default_rng(seed)
    blocks = math.ceil(n / block)
    offsets = np.arange(block)
    exceed = 0
    valid = 0
    for _ in range(draws):
        starts = rng.integers(0, n, size=blocks)
        index = ((starts[:, None] + offsets[None, :]) % n).ravel()[:n]
        draw_a, draw_b = _sharpe(a[index]), _sharpe(b[index])
        if draw_a is None or draw_b is None:
            continue
        valid += 1
        if abs((draw_a - draw_b) - observed) >= abs(observed):
            exceed += 1
    if valid < draws // 2:
        return None
    return (exceed + 1) / (valid + 1)


def _measure(
    values: pd.Series,
    *,
    book: str,
    cadence: str,
    turnover: float,
    cost: float,
    rebalances: int,
    baseline: AllocationResult | None,
    baseline_values: pd.Series | None = None,
    cash_rate_daily: float = 0.0,
    cash_weight_mean: float = 0.0,
) -> AllocationResult:
    returns = values.pct_change().dropna()
    years = len(values) / TRADING_DAYS
    cagr = float(values.iloc[-1] ** (1.0 / years) - 1.0)
    sigma = float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS))
    # Guarded with a tolerance rather than `== 0`: a near-constant series has a
    # standard deviation around 1e-17, not zero, and dividing by it returns a
    # confident Sharpe computed from noise.
    excess_returns = returns - cash_rate_daily
    sharpe = (
        float(excess_returns.mean() / returns.std(ddof=1) * math.sqrt(TRADING_DAYS))
        if sigma > _ZERO_VOLATILITY_ATOL
        else 0.0
    )
    p_value = None
    if baseline is not None and baseline_values is not None:
        p_value = sharpe_difference_p_value(
            excess_returns, baseline_values.pct_change().dropna() - cash_rate_daily
        )
    drawdown = float(values.div(values.cummax()).sub(1.0).min())
    return AllocationResult(
        book=book,
        cadence=cadence,
        total_return_pct=float(values.iloc[-1] - 1.0) * 100.0,
        cagr_pct=cagr * 100.0,
        volatility_pct=sigma * 100.0,
        sharpe=sharpe,
        max_drawdown_pct=drawdown * 100.0,
        turnover=turnover,
        modelled_cost_pct=cost * 100.0,
        rebalances=rebalances,
        sessions=len(values),
        excess_cagr_pct=0.0 if baseline is None else cagr * 100.0 - baseline.cagr_pct,
        excess_sharpe=0.0 if baseline is None else sharpe - baseline.sharpe,
        excess_max_drawdown_pct=(
            0.0 if baseline is None else drawdown * 100.0 - baseline.max_drawdown_pct
        ),
        excess_sharpe_p_value=p_value,
        cash_weight_mean=cash_weight_mean,
    )


def run_allocation_experiment(
    panel: pd.DataFrame,
    rules: dict[str, Callable[..., Allocation]],
    *,
    universe: list[str],
    benchmark: str,
    cost_bps: float = 2.0,
    cadences: tuple[str, ...] = CADENCES,
    cash_rate_annual: float | None = None,
) -> AllocationExperiment:
    """Every rule at every cadence, against buy-and-hold of the benchmark.

    The baseline is the benchmark bought once and held, charged the same entry
    cost. It is the comparison that matters: a rule that cannot beat holding SPY
    after costs has produced nothing, however it ranks against the other rules.
    """
    needed = [*universe, benchmark]
    missing = [s for s in needed if s not in panel.columns]
    if missing:
        raise AllocationRefused(
            f"the panel has no column for {', '.join(sorted(missing))}"
        )
    frame = panel.loc[:, needed].sort_index()
    holed = sorted(frame.columns[frame.isna().any()])
    if holed:
        raise AllocationRefused(
            f"incomplete history for {', '.join(holed)}; the experiment refuses "
            f"rather than filling, because a filled mark is a price nobody saw"
        )

    def _hold_benchmark(history, _universe, *, benchmark: str) -> Allocation:
        return Allocation(
            book="benchmark",
            rule_version="buy_and_hold/v1",
            universe=[benchmark],
            weights={benchmark: 1.0},
            inputs={},
            benchmark=benchmark,
        )

    if cash_rate_annual is not None and not math.isfinite(cash_rate_annual):
        raise AllocationRefused(
            f"cash_rate_annual must be finite, got {cash_rate_annual}"
        )
    cash_rate_daily = (
        0.0
        if cash_rate_annual is None
        else (1.0 + cash_rate_annual) ** (1.0 / TRADING_DAYS) - 1.0
    )
    # One warmup for every book and the baseline, set by the rule that needs
    # the most history. A rule needing 253 sessions used to be called on 128
    # and refuse, so it never ran; starting each book at its own first
    # possible date instead would compare them over different windows.
    warmup = max(
        [MIN_HISTORY_SESSIONS]
        + [
            int(getattr(rule, "min_history_sessions", MIN_HISTORY_SESSIONS)) - 1
            for rule in rules.values()
        ]
    )

    values, turnover, cost, rebalances, _cash = _run(
        frame, _hold_benchmark,
        universe=[benchmark], benchmark=benchmark,
        cadence="static", cost_bps=cost_bps,
        warmup_sessions=warmup, cash_rate_daily=cash_rate_daily,
    )
    baseline_values = values
    baseline = _measure(
        values, book=f"{benchmark} buy and hold", cadence="static",
        turnover=turnover, cost=cost, rebalances=rebalances, baseline=None,
        cash_rate_daily=cash_rate_daily,
    )

    results: list[AllocationResult] = []
    warnings: list[str] = [
        (
            "Two years of daily history covers one regime and contains no "
            "holdout; this is exploratory and is not a capital-allocation gate."
        ),
        (
            "Adjusted closes only. Taxes, bid-ask beyond the stated cost, and "
            "account constraints are excluded."
        ),
        (
            "Sector ETF membership is not reconstructed, so this experiment "
            "does not carry the constituent experiment's survivorship bias -- "
            "but it carries every other limitation it had."
        ),
    ]
    for book, rule in rules.items():
        for cadence in cadences:
            try:
                values, turnover, cost, rebalances, cash_mean = _run(
                    frame, rule,
                    universe=universe, benchmark=benchmark,
                    cadence=cadence, cost_bps=cost_bps,
                    warmup_sessions=warmup, cash_rate_daily=cash_rate_daily,
                )
            except AllocationRefused as exc:
                warnings.append(f"{book}/{cadence} refused: {exc}")
                continue
            results.append(
                _measure(
                    values, book=book, cadence=cadence,
                    turnover=turnover, cost=cost,
                    rebalances=rebalances, baseline=baseline,
                    baseline_values=baseline_values,
                    cash_rate_daily=cash_rate_daily,
                    cash_weight_mean=cash_mean,
                )
            )

    if results:
        warnings.append(
            f"{len(results)} book/cadence cells are each compared with one "
            f"baseline. excess_sharpe_p_value is per cell and uncorrected; a "
            f"cell is distinguishable from the baseline only below "
            f"{0.05 / len(results):.4f} (Bonferroni at 5%)."
        )
    if cash_rate_annual is None and any(
        r.cash_weight_mean > _ZERO_VOLATILITY_ATOL for r in results
    ):
        warnings.append(
            "No cash rate was supplied, so uninvested cash earned 0% and Sharpe "
            "is measured against 0%. A long/flat rule is understated by "
            "whatever cash actually paid while it was flat."
        )

    return AllocationExperiment(
        benchmark=benchmark,
        universe=list(universe),
        first_session=str(frame.index[0].date()),
        last_session=str(frame.index[-1].date()),
        cost_bps=cost_bps,
        warnings=tuple(warnings),
        baseline=baseline,
        results=results,
        warmup_sessions=warmup,
        cash_rate_annual_pct=(
            None if cash_rate_annual is None else cash_rate_annual * 100.0
        ),
    )


def summary(experiment: AllocationExperiment) -> dict[str, Any]:
    return {
        "benchmark": experiment.benchmark,
        "universe": experiment.universe,
        "window": [experiment.first_session, experiment.last_session],
        "cost_bps": experiment.cost_bps,
        "warmup_sessions": experiment.warmup_sessions,
        "cash_rate_annual_pct": experiment.cash_rate_annual_pct,
        "warnings": list(experiment.warnings),
        "baseline": experiment.baseline.__dict__,
        "results": [result.__dict__ for result in experiment.results],
    }


__all__ = [
    "CADENCES",
    "DRIFT_THRESHOLD",
    "QUARTERLY_SESSIONS",
    "AllocationExperiment",
    "AllocationResult",
    "run_allocation_experiment",
    "sharpe_difference_p_value",
    "summary",
]
