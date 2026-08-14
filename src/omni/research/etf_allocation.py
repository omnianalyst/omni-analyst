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
) -> tuple[pd.Series, float, float, int]:
    """Replay one rule at one cadence.

    The decision is taken on the close at `t` from history up to and including
    `t`, and the weights take effect at `t+1` with the cost charged first --
    the same rule `etf_replication` states, so the two experiments' numbers can
    be put next to each other.
    """
    returns = panel.pct_change(fill_method=None)
    sessions = panel.index[MIN_HISTORY_SESSIONS:]
    if len(sessions) < 2:
        raise AllocationRefused(
            f"{len(panel)} sessions leaves {len(sessions)} after the "
            f"{MIN_HISTORY_SESSIONS}-session warmup; a rule needs at least two "
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

    for position, date in enumerate(sessions[:-1]):
        history = panel.loc[:date]
        due = (
            weights.empty
            or (cadence == "quarterly" and since_rebalance >= QUARTERLY_SESSIONS)
            or (
                cadence == "threshold"
                and not target.empty
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

        following = sessions[position + 1]
        row = returns.loc[following].reindex(weights.index)
        if row.isna().any():
            missing = ", ".join(row.index[row.isna()][:5])
            raise AllocationRefused(
                f"missing return for {missing} on {following.date()}; a held "
                f"name without a mark cannot be scored as flat"
            )
        value *= 1.0 + float(weights @ row)
        grown = weights * (1.0 + row)
        weights = grown / float(grown.sum())
        values.append(value)
        dates.append(following)
        since_rebalance += 1

    return pd.Series(values, index=dates), total_turnover, total_cost, rebalances


def _measure(
    values: pd.Series,
    *,
    book: str,
    cadence: str,
    turnover: float,
    cost: float,
    rebalances: int,
    baseline: AllocationResult | None,
) -> AllocationResult:
    returns = values.pct_change().dropna()
    years = len(values) / TRADING_DAYS
    cagr = float(values.iloc[-1] ** (1.0 / years) - 1.0)
    sigma = float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS))
    # Guarded with a tolerance rather than `== 0`: a near-constant series has a
    # standard deviation around 1e-17, not zero, and dividing by it returns a
    # confident Sharpe computed from noise.
    sharpe = (
        float(returns.mean() / returns.std(ddof=1) * math.sqrt(TRADING_DAYS))
        if sigma > 1e-9
        else 0.0
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
    )


def run_allocation_experiment(
    panel: pd.DataFrame,
    rules: dict[str, Callable[..., Allocation]],
    *,
    universe: list[str],
    benchmark: str,
    cost_bps: float = 2.0,
    cadences: tuple[str, ...] = CADENCES,
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

    values, turnover, cost, rebalances = _run(
        frame, _hold_benchmark,
        universe=[benchmark], benchmark=benchmark,
        cadence="static", cost_bps=cost_bps,
    )
    baseline = _measure(
        values, book=f"{benchmark} buy and hold", cadence="static",
        turnover=turnover, cost=cost, rebalances=rebalances, baseline=None,
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
                values, turnover, cost, rebalances = _run(
                    frame, rule,
                    universe=universe, benchmark=benchmark,
                    cadence=cadence, cost_bps=cost_bps,
                )
            except AllocationRefused as exc:
                warnings.append(f"{book}/{cadence} refused: {exc}")
                continue
            results.append(
                _measure(
                    values, book=book, cadence=cadence,
                    turnover=turnover, cost=cost,
                    rebalances=rebalances, baseline=baseline,
                )
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
    )


def summary(experiment: AllocationExperiment) -> dict[str, Any]:
    return {
        "benchmark": experiment.benchmark,
        "universe": experiment.universe,
        "window": [experiment.first_session, experiment.last_session],
        "cost_bps": experiment.cost_bps,
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
    "summary",
]
