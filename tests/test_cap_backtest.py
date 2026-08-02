"""Behaviour tests for the backtest validation capabilities.

Two v1 test files were the oracle and were copied verbatim except for the
import path (per PORTING.md):

- `app/research/tests/test_backtest_validation.py` -> all of its tests, below.
- `app/research/tests/test_stage6_lifecycle.py` -> only the S6.1 PIT-backtester
  tests (the rest of that file exercised modules not in this port).

Additional tests at the end cover the failure paths the work order requires:
PBO with `T < n_groups` now raises `Unavailable` (v1 silently degraded the
requested granularity), and the no-leakage invariant is asserted on a hand-
computed price path rather than only on a seeded random series.
"""

import numpy as np
import pandas as pd
import pytest

from omni.capabilities.backtest import (
    PurgedKFold,
    average_trade_duration,
    backtest_signal,
    deflated_sharpe_ratio,
    evaluate_strategy_sharpe,
    expected_max_sharpe,
    forward_returns,
    leakage_probe,
    probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting,
    sharpe_ratio,
    win_rate,
)
from omni.ingest.protocol import Unavailable

# --------------------------------------------------------------------------- #
# Sharpe-ratio statistics (ported from test_backtest_validation.py)
# --------------------------------------------------------------------------- #

def test_sharpe_ratio_basic():
    rng = np.random.default_rng(0)
    r = rng.normal(0.001, 0.01, 1000)
    sr = sharpe_ratio(r, periods_per_year=252)
    # mean/std ~ 0.1 per period * sqrt(252) ~ 1.5; just check it's sane & positive
    assert 0.5 < sr < 3.5


def test_sharpe_ratio_flat_market_float_noise_is_nan():
    # A flat market's returns are float noise (~1e-17), not exactly 0.0; an
    # `== 0` guard does not fire and Sharpe explodes to a confident number that
    # means nothing (~2.7 on this series). The economically-zero variance must
    # return nan. Discriminates the old `sd == 0` guard, which returned 2.695.
    import math

    near = np.array([0.0, 1e-17, -1e-17, 2e-17, -1e-17] * 10)
    assert math.isnan(sharpe_ratio(near))
    # And the same guard carries into evaluate_strategy_sharpe (is_credible False).
    rep = evaluate_strategy_sharpe(near)
    assert math.isnan(rep.annualized_sharpe)
    assert rep.is_credible is False


def test_psr_increases_with_sharpe():
    low = probabilistic_sharpe_ratio(0.05, 252)
    high = probabilistic_sharpe_ratio(0.20, 252)
    assert high > low
    assert 0 <= low <= 1 and 0 <= high <= 1


def test_psr_increases_with_sample_size():
    short = probabilistic_sharpe_ratio(0.1, 50)
    long = probabilistic_sharpe_ratio(0.1, 5000)
    assert long > short


def test_expected_max_sharpe_increases_with_trials():
    one = expected_max_sharpe(1, 0.01)
    many = expected_max_sharpe(100, 0.01)
    assert one == 0.0
    assert many > 0.0
    assert expected_max_sharpe(1000, 0.01) > many


def test_dsr_penalizes_multiple_trials():
    # Same observed strategy, but found after 1 vs 200 trials.
    psr_like = deflated_sharpe_ratio(0.15, 1000, n_trials=1, sr_variance=0.01)
    deflated = deflated_sharpe_ratio(0.15, 1000, n_trials=200, sr_variance=0.01)
    assert deflated < psr_like


def test_evaluate_strategy_sharpe_credible_vs_noise():
    rng = np.random.default_rng(1)
    # Genuinely good, single trial, long sample
    good = rng.normal(0.0008, 0.01, 2000)
    rep_good = evaluate_strategy_sharpe(good, n_trials=1)
    assert rep_good.annualized_sharpe > 0.8
    assert rep_good.psr > 0.9
    # The same series but claimed as best-of-500 trials should be far less credible
    rep_overfit = evaluate_strategy_sharpe(good, n_trials=500, sr_variance=0.02)
    assert rep_overfit.dsr < rep_good.psr


def test_evaluate_strategy_sharpe_noise_not_credible():
    rng = np.random.default_rng(2)
    noise = rng.normal(0, 0.01, 500)  # zero-mean -> no real Sharpe
    rep = evaluate_strategy_sharpe(noise, n_trials=100, sr_variance=0.02)
    assert not rep.is_credible


# ---- PurgedKFold: no leakage ------------------------------------------------ #

def test_purged_kfold_no_label_overlap_between_train_and_test():
    n = 200
    horizon = 5
    idx = pd.RangeIndex(n)
    # each sample's label ends `horizon` bars later
    label_end = pd.Series(np.minimum(np.arange(n) + horizon, n - 1), index=idx)
    cv = PurgedKFold(n_splits=5, label_end_times=label_end, embargo_pct=0.02)
    X = np.zeros((n, 1))

    starts = np.arange(n)
    ends = label_end.to_numpy()
    seen_test = 0
    for train_idx, test_idx in cv.split(X):
        seen_test += len(test_idx)
        t_start, t_end = test_idx.min(), test_idx.max()
        # No training sample's [start, label_end] may overlap the test window.
        for ti in train_idx:
            overlap = (starts[ti] <= t_end) and (ends[ti] >= t_start)
            assert not overlap, f"train sample {ti} leaks into test [{t_start},{t_end}]"
        # train and test are disjoint
        assert len(set(train_idx) & set(test_idx)) == 0
    assert seen_test == n  # every sample tested exactly once


def test_purged_kfold_requires_matching_length():
    cv = PurgedKFold(n_splits=3, label_end_times=pd.Series([1, 2, 3]))
    with pytest.raises(ValueError):
        list(cv.split(np.zeros((10, 1))))


# ---- PBO -------------------------------------------------------------------- #

def test_pbo_high_for_pure_noise_strategies():
    # For a SINGLE finite noise matrix one strategy is the luckiest over the full
    # sample and persists across splits, so a single PBO can be low. Averaged over
    # many independent noise draws the in-sample winner's OOS rank becomes uniform
    # and E[PBO] -> ~0.5. That average is the theoretically correct assertion.
    pbos = []
    for seed in range(25):
        rng = np.random.default_rng(seed)
        perf = rng.normal(0, 1, (400, 16))
        rep = probability_of_backtest_overfitting(perf, n_groups=10)
        assert rep.n_combinations > 0
        pbos.append(rep.pbo)
    mean_pbo = float(np.mean(pbos))
    assert mean_pbo > 0.3  # noise selection does not generalize OOS


def test_pbo_low_when_one_strategy_genuinely_dominates():
    rng = np.random.default_rng(4)
    T, N = 400, 20
    perf = rng.normal(0, 1, (T, N))
    perf[:, 0] += 1.0  # strategy 0 has a real, persistent edge every period
    rep = probability_of_backtest_overfitting(perf, n_groups=10)
    assert rep.pbo < 0.1  # the IS-best (strat 0) is also OOS-best -> not overfit


def test_pbo_requires_two_strategies():
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting(np.zeros((100, 1)))


# --------------------------------------------------------------------------- #
# PIT backtester (ported from test_stage6_lifecycle.py, S6.1 portion only)
# --------------------------------------------------------------------------- #

def _prices(n=300, seed=0, drift=0.0003, vol=0.01):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(drift, vol, n))), index=idx)


def test_backtest_requires_positive_lag():
    p = _prices()
    with pytest.raises(ValueError):
        backtest_signal(pd.Series(1.0, index=p.index), p, lag=0)


def test_leakage_probe_prevents_lookahead():
    p = _prices(n=400, seed=1)
    probe = leakage_probe(p, horizon=1)
    # Perfect-foresight signal makes a fortune naively...
    assert probe["naive_lookahead_total"] > 1.0
    # ...but the causal (lagged) backtest cannot reproduce it.
    assert probe["causal_total"] < probe["naive_lookahead_total"] * 0.5
    assert probe["leak_prevented"] is True


def test_costs_reduce_returns():
    p = _prices(n=300, seed=2)
    # An alternating signal churns every bar -> costs bite.
    sig = pd.Series(np.where(np.arange(len(p)) % 2 == 0, 1.0, -1.0), index=p.index)
    free = backtest_signal(sig, p, cost_per_turn=0.0).total_return()
    costly = backtest_signal(sig, p, cost_per_turn=0.001).total_return()
    assert costly < free


# --------------------------------------------------------------------------- #
# Additional failure-path / invariant tests required by the G3 work order.
# --------------------------------------------------------------------------- #

def test_sharpe_ratio_nan_below_two_obs():
    assert np.isnan(sharpe_ratio([0.01]))
    assert np.isnan(sharpe_ratio([]))


def test_sharpe_ratio_nan_when_zero_variance():
    # Constant returns -> zero std -> Sharpe undefined, not zero.
    assert np.isnan(sharpe_ratio([0.001, 0.001, 0.001, 0.001]))


def test_psr_nan_below_two_obs():
    assert np.isnan(probabilistic_sharpe_ratio(0.1, n_obs=1))


def test_purged_kfold_requires_at_least_two_splits():
    with pytest.raises(ValueError, match="n_splits must be >= 2"):
        PurgedKFold(n_splits=1)


def test_pbo_raises_unavailable_when_too_few_rows_for_requested_groups():
    # Caller asked for 10 groups on 5 rows. v1 silently rewrote n_groups; v2
    # reports the gap honestly instead of running a degraded analysis.
    perf = np.arange(10).reshape(5, 2).astype(float)
    with pytest.raises(Unavailable, match="n_groups=10"):
        probability_of_backtest_overfitting(perf, n_groups=10)


def test_forward_returns_is_strictly_causal_on_known_prices():
    # r_t = price_{t+h} / price_t - 1; nothing at t touches price_{t+h-1} or later.
    p = pd.Series([100.0, 110.0, 121.0, 133.1])
    out = forward_returns(p, horizon=1)
    assert out.iloc[0] == pytest.approx(110.0 / 100.0 - 1.0)
    assert out.iloc[1] == pytest.approx(121.0 / 110.0 - 1.0)
    assert np.isnan(out.iloc[-1])  # no future price -> undefined, not fabricated


def test_leakage_probe_blocks_same_bar_signal_on_hand_computed_path():
    # An oscillating price: fwd sign flips every bar, so a perfect-foresight
    # signal is +1/-1/+1/-1/.... Naive same-bar multiplication lands on the
    # right side every bar -> compounds up. The lagged causal backtester enters
    # each bar with yesterday's (wrong) sign -> compounds down. That gap is
    # exactly what `leak_prevented` detects.
    p = pd.Series([100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0,
                   100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0,
                   100.0, 101.0, 100.0, 101.0])
    probe = leakage_probe(p, horizon=1)
    assert probe["naive_lookahead_total"] > 0.0
    assert probe["causal_total"] < 0.0
    assert probe["leak_prevented"] is True


def test_backtest_signal_cannot_act_on_same_bar_return():
    # The whole no-leakage invariant in one case: a signal observed at the close
    # of bar 0 "knew" bar 0 -> bar 1 would rise 10%. Legal lag=1 shifts the
    # position to bar 1 onward, by which point the move is over (fwd[1] = 0).
    # The same-bar foresight is therefore neutralized -- total return is zero,
    # not +10%.
    idx = pd.date_range("2020-01-01", periods=3, freq="B")
    prices = pd.Series([100.0, 110.0, 110.0], index=idx)
    sig = pd.Series([1.0, 0.0, 0.0], index=idx)
    res = backtest_signal(sig, prices, lag=1, cost_per_turn=0.0)
    assert res.n_bars > 0
    assert res.total_return() == pytest.approx(0.0)


def test_backtest_signal_lag_governs_which_bars_are_reachable():
    # Same signal, two legal lags. Signal observed at bar 1. The price jumps
    # between bar 2 and bar 3 (fwd[2] = +10%). lag=1 lands the position on
    # bars 2.. and captures fwd[2]; lag=2 lands it on bars 3.. and misses it.
    idx = pd.date_range("2020-01-01", periods=6, freq="B")
    prices = pd.Series([100.0, 100.0, 100.0, 110.0, 110.0, 110.0], index=idx)
    sig = pd.Series([0.0, 1.0, 0.0, 0.0, 0.0, 0.0], index=idx)
    res_lag1 = backtest_signal(sig, prices, lag=1, cost_per_turn=0.0)
    res_lag2 = backtest_signal(sig, prices, lag=2, cost_per_turn=0.0)
    assert res_lag1.total_return() == pytest.approx(0.10, abs=1e-9)
    assert res_lag2.total_return() == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# Strategy / trade-performance statistics (from v1 trading router)
# --------------------------------------------------------------------------- #

def test_win_rate_is_winning_over_total():
    assert win_rate(7, 10) == 0.7


def test_win_rate_zero_winners_is_zero():
    assert win_rate(0, 5) == 0.0


def test_win_rate_all_winners_is_one():
    assert win_rate(3, 3) == 1.0


def test_win_rate_undefined_with_no_closed_trades():
    # v1 left win_rate = 0.0 when total_trades == 0, indistinguishable from a
    # strategy that loses every trade.
    with pytest.raises(Unavailable):
        win_rate(0, 0)


def test_win_rate_rejects_negative_winners():
    with pytest.raises(Unavailable):
        win_rate(-1, 10)


def test_average_trade_duration_converts_seconds_to_hours():
    # [3600, 7200] -> mean 5400s -> 1.5h.
    assert average_trade_duration([3600, 7200]) == 1.5


def test_average_trade_duration_single_trade():
    assert average_trade_duration([1800]) == 0.5


def test_average_trade_duration_empty_raises():
    # v1 returned 0.0 on an empty list, reporting a zero-hour average over
    # trades that do not exist.
    with pytest.raises(Unavailable):
        average_trade_duration([])
