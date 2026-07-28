"""Tests for triple-barrier labeling + CUSUM events (S4.1, Appendix A.8)."""

import numpy as np
import pandas as pd

from omni.research.labeling.triple_barrier import (
    cusum_events,
    ewma_volatility,
    triple_barrier_labels,
)


def _dt(n):
    return pd.bdate_range("2020-01-01", periods=n)


def test_upper_barrier_hit_first_labels_plus_one():
    # Monotone rising price: from any event the upper barrier is touched first.
    idx = _dt(60)
    prices = pd.Series(100.0 * (1.01 ** np.arange(60)), index=idx)
    out = triple_barrier_labels(
        prices, [idx[5]], max_holding=20, upper_mult=1.0, lower_mult=1.0
    )
    row = out.labels.iloc[0]
    assert row["label"] == 1
    assert row["barrier"] == "upper"
    assert row["ret"] > 0


def test_lower_barrier_hit_first_labels_minus_one():
    idx = _dt(60)
    prices = pd.Series(100.0 * (0.99 ** np.arange(60)), index=idx)
    out = triple_barrier_labels(
        prices, [idx[5]], max_holding=20, upper_mult=1.0, lower_mult=1.0
    )
    row = out.labels.iloc[0]
    assert row["label"] == -1
    assert row["barrier"] == "lower"


def test_vertical_barrier_labels_zero_when_flat():
    # Flat price with tiny noise that never breaches +/-k*sigma within horizon.
    idx = _dt(60)
    rng = np.random.default_rng(0)
    prices = pd.Series(100.0 + np.cumsum(rng.normal(0, 1e-6, 60)), index=idx)
    out = triple_barrier_labels(
        prices, [idx[5]], max_holding=10, upper_mult=50.0, lower_mult=50.0
    )
    row = out.labels.iloc[0]
    assert row["barrier"] == "vertical"
    assert row["label"] == 0


def test_t1_is_label_end_for_purged_cv():
    idx = _dt(60)
    prices = pd.Series(100.0 * (1.01 ** np.arange(60)), index=idx)
    out = triple_barrier_labels(prices, [idx[5], idx[10]], max_holding=15)
    t1 = out.t1
    assert len(t1) == 2
    # touch time must be strictly after the event time
    assert (t1.values > t1.index.values).all()


def test_cusum_emits_events_on_runs_not_on_noise():
    idx = _dt(200)
    rng = np.random.default_rng(1)
    # Mostly small noise with two deliberate sustained up-runs.
    rets = rng.normal(0, 0.001, 200)
    rets[50:60] += 0.01
    rets[150:160] += 0.01
    prices = pd.Series(100.0 * np.exp(np.cumsum(rets)), index=idx)
    events = cusum_events(prices, threshold=0.02)
    assert len(events) >= 2
    # Far fewer events than bars: it is not a fixed clock.
    assert len(events) < len(idx) // 2


def test_volatility_scaling_makes_barrier_in_sigma_units():
    idx = _dt(120)
    rng = np.random.default_rng(2)
    prices = pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 120))), index=idx)
    vol = ewma_volatility(prices, span=50)
    # The first return's EWMA std is undefined/zero (single sample); once a few
    # returns have accrued the volatility estimate is strictly positive.
    assert vol.iloc[5:].gt(0).all()


def test_side_argument_produces_binary_win_labels():
    # Rising price; a long side should win (label 1), a short side should lose (0).
    idx = _dt(60)
    prices = pd.Series(100.0 * (1.01 ** np.arange(60)), index=idx)
    long_side = pd.Series(1.0, index=[idx[5]])
    short_side = pd.Series(-1.0, index=[idx[5]])
    out_long = triple_barrier_labels(
        prices, [idx[5]], max_holding=20, upper_mult=1.0, lower_mult=1.0, side=long_side
    )
    out_short = triple_barrier_labels(
        prices, [idx[5]], max_holding=20, upper_mult=1.0, lower_mult=1.0, side=short_side
    )
    assert set(np.unique(out_long.labels["label"])) <= {0, 1}
    assert out_long.labels.iloc[0]["label"] == 1
    assert out_short.labels.iloc[0]["label"] == 0
