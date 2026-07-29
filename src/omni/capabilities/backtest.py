"""Backtest validation -- the defenses that separate edge from data-mining,
plus the leakage-free point-in-time backtester whose invariant they defend.

Most "profitable" backtests are artifacts of look-ahead leakage and multiple
testing. This module ports two v1 sources:

- `app/research/backtest_validation.py`: PurgedKFold, Probabilistic Sharpe
  Ratio, Deflated Sharpe Ratio, and Probability of Backtest Overfitting
  (Lopez de Prado, *Advances in Financial Machine Learning*; Bailey &
  Lopez de Prado 2012/2014).
- `app/services/backtesting/pit_backtester.py`: a deliberately framework-light
  backtester whose single no-look-ahead rule is stated below.

The no-leakage invariant is the point. A signal known at the close of day ``t``
may only earn the return from ``t -> t+1`` (or ``t -> t+h``). It is enforced by
shifting the signal forward one bar before multiplying by forward returns --
there is no code path by which a same-bar signal touches a same-bar return.
``leakage_probe`` makes that guarantee testable; a backtest that can see the
future is worse than none.

All functions are pure and operate on numpy/pandas. Where v1 silently degraded
the requested analysis granularity when input was insufficient (PBO with
``T < n_groups`` rewrote the caller's ``n_groups``), this module raises
``Unavailable`` instead -- reporting the gap honestly rather than fabricating a
result at a different granularity than the caller asked for.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import combinations

import numpy as np
import pandas as pd
from scipy import stats

from omni.ingest.protocol import Unavailable

_EULER_MASCHERONI = 0.5772156649015329


# --------------------------------------------------------------------------- #
# Sharpe-ratio statistics
# --------------------------------------------------------------------------- #

def sharpe_ratio(returns: Sequence[float], periods_per_year: int = 252) -> float:
    """Annualized Sharpe ratio of a per-period return series (excess assumed)."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return float("nan")
    sd = r.std(ddof=1)
    if sd == 0:
        return float("nan")
    return float(r.mean() / sd * np.sqrt(periods_per_year))


def probabilistic_sharpe_ratio(
    observed_sr: float,
    n_obs: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    benchmark_sr: float = 0.0,
) -> float:
    """Probabilistic Sharpe Ratio: P(true SR > benchmark_sr).

    `observed_sr` and `benchmark_sr` are **per-observation** (non-annualized)
    Sharpe ratios. `kurtosis` is non-excess (normal == 3). Returns a probability
    in [0, 1]; higher is more confidence the Sharpe is real.
    """
    if n_obs < 2 or not np.isfinite(observed_sr):
        return float("nan")
    denom = 1.0 - skew * observed_sr + (kurtosis - 1.0) / 4.0 * observed_sr**2
    if denom <= 0:
        return float("nan")
    z = (observed_sr - benchmark_sr) * np.sqrt(n_obs - 1) / np.sqrt(denom)
    return float(stats.norm.cdf(z))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Expected maximum Sharpe across `n_trials` independent strategies under the
    null (true SR = 0), per Bailey & Lopez de Prado. `sr_variance` is the variance
    of the per-observation Sharpe ratios across the trials. The result is the
    benchmark the Deflated Sharpe must beat."""
    if n_trials < 1 or sr_variance <= 0:
        return 0.0
    if n_trials == 1:
        return 0.0
    sd = np.sqrt(sr_variance)
    z1 = stats.norm.ppf(1.0 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1.0 - 1.0 / (n_trials * np.e))
    return float(sd * ((1.0 - _EULER_MASCHERONI) * z1 + _EULER_MASCHERONI * z2))


def deflated_sharpe_ratio(
    observed_sr: float,
    n_obs: int,
    n_trials: int,
    sr_variance: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Deflated Sharpe Ratio: PSR against the expected-max-Sharpe benchmark.

    This is the honest probability that a strategy's Sharpe is real *given that it
    was selected as the best of `n_trials` attempts*. As n_trials grows, the bar
    rises and DSR falls -- which is exactly the correction for backtest selection
    bias that naive Sharpe ignores.
    """
    benchmark = expected_max_sharpe(n_trials, sr_variance)
    return probabilistic_sharpe_ratio(observed_sr, n_obs, skew, kurtosis, benchmark)


@dataclass
class DeflatedSharpeReport:
    annualized_sharpe: float
    per_period_sharpe: float
    n_obs: int
    n_trials: int
    psr: float  # vs 0
    dsr: float  # vs expected-max-of-trials
    skew: float
    kurtosis: float
    is_credible: bool  # DSR > 0.95

    def to_dict(self) -> dict:
        return {
            k: (None if isinstance(v, float) and not np.isfinite(v) else v)
            for k, v in self.__dict__.items()
        }


def evaluate_strategy_sharpe(
    returns: Sequence[float],
    n_trials: int = 1,
    periods_per_year: int = 252,
    sr_variance: float | None = None,
) -> DeflatedSharpeReport:
    """End-to-end Sharpe credibility report for a strategy's return series.

    If `sr_variance` (variance of per-period Sharpe across the trials you ran) is
    unknown, a conservative default of 1/n_obs is used (the sampling variance of
    the Sharpe estimator under the null), which is the common practical choice.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = r.size
    if n < 2 or r.std(ddof=1) == 0:
        return DeflatedSharpeReport(
            float("nan"), float("nan"), n, n_trials,
            float("nan"), float("nan"), float("nan"), float("nan"), False,
        )
    per_period_sr = r.mean() / r.std(ddof=1)
    ann_sr = per_period_sr * np.sqrt(periods_per_year)
    sk = float(stats.skew(r))
    ku = float(stats.kurtosis(r, fisher=False))  # non-excess
    if sr_variance is None:
        sr_variance = 1.0 / n
    psr = probabilistic_sharpe_ratio(per_period_sr, n, sk, ku, 0.0)
    dsr = deflated_sharpe_ratio(per_period_sr, n, n_trials, sr_variance, sk, ku)
    return DeflatedSharpeReport(
        annualized_sharpe=float(ann_sr),
        per_period_sharpe=float(per_period_sr),
        n_obs=int(n),
        n_trials=int(n_trials),
        psr=float(psr) if np.isfinite(psr) else float("nan"),
        dsr=float(dsr) if np.isfinite(dsr) else float("nan"),
        skew=sk,
        kurtosis=ku,
        is_credible=bool(np.isfinite(dsr) and dsr > 0.95),
    )


# --------------------------------------------------------------------------- #
# Purged & embargoed K-fold cross-validation
# --------------------------------------------------------------------------- #

class PurgedKFold:
    """K-fold splitter for time series with overlapping labels.

    Prevents leakage by (1) **purging** from the training set any sample whose
    label interval [start, label_end] overlaps the test interval, and (2)
    **embargoing** an additional fraction of samples immediately after the test
    set (where serial correlation would otherwise leak test info into training).

    Parameters
    ----------
    n_splits : number of folds.
    label_end_times : pd.Series indexed by sample start time, value = the time the
        sample's label is realized. For a horizon-`h` forward return, this is the
        timestamp `h` bars ahead. If None, labels are assumed point-in-time
        (label_end == start) and only the embargo applies.
    embargo_pct : fraction of total samples to embargo after each test fold.
    """

    def __init__(
        self,
        n_splits: int = 5,
        label_end_times: pd.Series | None = None,
        embargo_pct: float = 0.01,
    ):
        if n_splits < 2:
            raise ValueError("n_splits must be >= 2")
        self.n_splits = n_splits
        self.label_end_times = label_end_times
        self.embargo_pct = embargo_pct

    def split(self, X) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        n = len(X)
        indices = np.arange(n)
        if self.label_end_times is not None:
            if len(self.label_end_times) != n:
                raise ValueError("label_end_times length must match X")
            starts = pd.Series(self.label_end_times.index, index=indices)
            ends = pd.Series(self.label_end_times.values, index=indices)
        else:
            starts = pd.Series(indices, index=indices)
            ends = pd.Series(indices, index=indices)

        embargo = int(n * self.embargo_pct)
        test_bounds = [
            (b[0], b[-1] + 1) for b in np.array_split(indices, self.n_splits)
        ]

        for start_ix, end_ix in test_bounds:
            test_idx = indices[start_ix:end_ix]
            test_start = starts.iloc[start_ix]
            test_end = ends.iloc[end_ix - 1]

            # Purge: drop training samples whose label window overlaps the test
            # window. Overlap iff sample.start <= test_end AND sample.label_end >= test_start.
            train_mask = ~((starts <= test_end) & (ends >= test_start))

            # Embargo: also drop a buffer of samples right after the test block.
            if embargo > 0:
                embargo_hi = min(end_ix + embargo, n)
                train_mask.iloc[end_ix:embargo_hi] = False

            # A sample inside the test block is never in train.
            train_mask.iloc[start_ix:end_ix] = False

            train_idx = indices[train_mask.to_numpy()]
            yield train_idx, test_idx


# --------------------------------------------------------------------------- #
# Probability of Backtest Overfitting (CSCV)
# --------------------------------------------------------------------------- #

@dataclass
class PBOReport:
    pbo: float  # probability of backtest overfitting in [0, 1]
    n_combinations: int
    n_strategies: int
    median_logit: float

    def to_dict(self) -> dict:
        return {
            "pbo": None if not np.isfinite(self.pbo) else self.pbo,
            "n_combinations": self.n_combinations,
            "n_strategies": self.n_strategies,
            "median_logit": None if not np.isfinite(self.median_logit) else self.median_logit,
        }


def probability_of_backtest_overfitting(
    performance: np.ndarray,
    n_groups: int = 10,
    higher_is_better: bool = True,
) -> PBOReport:
    """Probability of Backtest Overfitting via Combinatorially-Symmetric CV.

    Parameters
    ----------
    performance : array of shape (T, N) -- T per-slice performance observations for
        each of N candidate strategies (e.g. per-period returns of N strategies).
    n_groups : number of disjoint time groups S (must be even); IS/OOS each take
        S/2 groups across all C(S, S/2) combinations.

    Returns the PBO: the fraction of train/test splits where the strategy that was
    best in-sample lands below the median out-of-sample. PBO near 0.5+ means the
    selection process is overfit (in-sample ranking does not carry out of sample).
    """
    M = np.asarray(performance, dtype=float)
    if M.ndim != 2:
        raise ValueError("performance must be 2-D (T observations x N strategies)")
    T, N = M.shape
    if N < 2:
        raise ValueError("need at least 2 strategies")
    if n_groups % 2 != 0:
        n_groups -= 1
    if n_groups < 2:
        raise ValueError("n_groups must be >= 2")
    if T < n_groups:
        raise Unavailable(
            f"performance has T={T} rows but n_groups={n_groups} was requested; "
            "cannot form that many non-empty groups"
        )
    sign = 1.0 if higher_is_better else -1.0

    groups = [g for g in np.array_split(np.arange(T), n_groups) if len(g) > 0]
    n_groups = len(groups)
    if n_groups < 2:
        return PBOReport(float("nan"), 0, N, float("nan"))

    logits = []
    half = n_groups // 2
    for is_combo in combinations(range(n_groups), half):
        is_set = set(is_combo)
        is_rows = np.concatenate([groups[g] for g in range(n_groups) if g in is_set])
        oos_rows = np.concatenate([groups[g] for g in range(n_groups) if g not in is_set])

        is_perf = sign * M[is_rows].mean(axis=0)
        oos_perf = sign * M[oos_rows].mean(axis=0)

        best_is = int(np.argmax(is_perf))
        # Out-of-sample rank of the in-sample-best strategy (1 = worst, N = best).
        oos_rank = stats.rankdata(oos_perf)[best_is]
        omega = oos_rank / (N + 1.0)
        omega = min(max(omega, 1e-9), 1 - 1e-9)
        logits.append(np.log(omega / (1.0 - omega)))

    if not logits:
        return PBOReport(float("nan"), 0, N, float("nan"))
    logits = np.array(logits)
    pbo = float((logits <= 0).mean())  # below-median OOS == logit <= 0
    return PBOReport(
        pbo=pbo,
        n_combinations=len(logits),
        n_strategies=N,
        median_logit=float(np.median(logits)),
    )


# --------------------------------------------------------------------------- #
# Point-in-time backtester (no look-ahead)
# --------------------------------------------------------------------------- #

@dataclass
class BacktestResult:
    returns: pd.Series          # per-bar strategy returns (causal)
    equity_curve: pd.Series     # cumulative (1+r).cumprod()
    n_bars: int

    def total_return(self) -> float:
        return float(self.equity_curve.iloc[-1] - 1.0) if len(self.equity_curve) else 0.0


def forward_returns(prices: pd.DataFrame | pd.Series, horizon: int = 1) -> pd.DataFrame | pd.Series:
    """``r_t = price_{t+h}/price_t - 1`` -- the return realized AFTER t (causal)."""
    return prices.shift(-horizon) / prices - 1.0


def backtest_signal(
    signal: pd.Series,
    prices: pd.Series,
    *,
    horizon: int = 1,
    cost_per_turn: float = 0.0,
    lag: int = 1,
) -> BacktestResult:
    """Backtest a single-asset position signal against a price series.

    The signal is LAGGED by ``lag`` bars before being applied to forward returns
    -- a signal computed at t can only be acted on at t+lag, never on the same
    bar's move. This is the no-look-ahead rule; ``leakage_probe`` tests it.

    Args:
        signal: desired position at the close of each bar (e.g. -1/0/+1 or a
            continuous weight).
        prices: adjusted close series (DatetimeIndex).
        horizon: holding horizon in bars for the forward return.
        cost_per_turn: proportional cost charged on |position change|.
        lag: bars between signal observation and position application (>=1).
    """
    if lag < 1:
        raise ValueError("lag must be >= 1 to avoid look-ahead (signal at t acts at t+lag)")
    prices = prices.astype("float64").sort_index()
    signal = signal.reindex(prices.index).astype("float64")

    position = signal.shift(lag).fillna(0.0)          # acted on strictly after observation
    fwd = forward_returns(prices, horizon)            # return realized after the bar
    gross = position * fwd
    turns = position.diff().abs().fillna(position.abs())
    costs = turns * cost_per_turn
    net = (gross - costs).dropna()

    equity = (1.0 + net).cumprod()
    return BacktestResult(returns=net, equity_curve=equity, n_bars=len(net))


def leakage_probe(prices: pd.Series, *, horizon: int = 1) -> dict:
    """Demonstrate that the backtester forbids look-ahead.

    Builds a "cheating" signal equal to the *future* return sign (perfect
    foresight). The causal backtester (lag>=1) must NOT reproduce that perfect
    return -- because the signal is shifted forward before being applied --
    whereas naively multiplying the same-bar signal by the same-bar forward
    return would. Returns the two totals and the ``leak_prevented`` verdict.
    """
    prices = prices.astype("float64").sort_index()
    fwd = forward_returns(prices, horizon)
    cheat_signal = np.sign(fwd).fillna(0.0)  # knows the future at t

    # Naive look-ahead PnL: same-bar cheat signal * same-bar forward return.
    naive = (cheat_signal * fwd).dropna()
    naive_total = float((1.0 + naive).prod() - 1.0)

    # Causal backtest of the SAME cheat signal: lag forces it to t+1, destroying
    # the foresight advantage.
    causal = backtest_signal(cheat_signal, prices, horizon=horizon, lag=1)
    causal_total = causal.total_return()

    return {
        "naive_lookahead_total": naive_total,
        "causal_total": causal_total,
        # Leakage is prevented iff the causal path is dramatically worse than the
        # impossible look-ahead path (perfect foresight can't survive the lag).
        "leak_prevented": causal_total < naive_total * 0.5,
    }
