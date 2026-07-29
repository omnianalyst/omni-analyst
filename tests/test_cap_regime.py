"""Behaviour tests for the market-regime capabilities.

Every assertion is on a constructed series with a known answer, not on shape.
Every failure path (short series, zero variance, NaN, window of 0/1) raises
`Unavailable` -- v1's inline endpoint shrank its window or returned an empty
result on these, which is how a covered-looking-but-empty network enters the
store. There is no v1 test file for regime detection; these tests are the
oracle.
"""

import numpy as np
import pytest

from omni.capabilities.regime import (
    classify_trend,
    classify_volatility,
    detect_regime_changes,
    realised_volatility,
    volatility_regime_path,
)
from omni.ingest.protocol import Unavailable

# ---------------------------------------------------------------------------
# realised_volatility -- trailing population std over exactly `window` returns
# ---------------------------------------------------------------------------

class TestRealisedVolatility:
    def test_trailing_population_std_uses_window_returns(self):
        # window is the number of returns each statistic spans: the interior
        # slice is returns[i-window+1:i+1] -- exactly window points. (v1's
        # inline endpoint sliced [max(0,i-window):i+1], window+1 points; fixing
        # that is the deliberate deviation recorded in regime.py.)
        #
        # Hand-derived (population std, ddof=0):
        #   std([1.0])        = 0.0
        #   std([1.0, 2.0])   = 0.5      (mean 1.5, var 0.25)
        #   std([2.0, 3.0])   = 0.5      (mean 2.5, var 0.25)
        #   std([3.0, 9.0])   = 3.0      (mean 6.0, var 9.0)
        #   std([9.0, 20.0])  = 5.5      (mean 14.5, var 30.25)
        # The last two pin the trailing window: v1's window+1 slice would give
        # std([2,3,9])=3.091 and std([3,9,20])=7.040, neither of which matches.
        r = [1.0, 2.0, 3.0, 9.0, 20.0]
        vol = realised_volatility(r, window=2)
        assert vol[0] == pytest.approx(0.0)                       # std([1.0])
        assert vol[1] == pytest.approx(0.5)                       # std([1, 2])
        assert vol[2] == pytest.approx(0.5)                       # std([2, 3])
        assert vol[3] == pytest.approx(np.std([3.0, 9.0]))        # std([3, 9]) = 3.0
        assert vol[4] == pytest.approx(np.std([9.0, 20.0]))       # std([9, 20]) = 5.5

    def test_uses_population_std_not_sample_std(self):
        # np.std is ddof=0; sample std (ddof=1) would differ for n=2.
        vol = realised_volatility([1.0, 2.0], window=2)
        assert vol[1] == pytest.approx(0.5)            # population std of [1,2]
        assert vol[1] != pytest.approx(np.std([1.0, 2.0], ddof=1))


# ---------------------------------------------------------------------------
# classify_volatility -- required outcome
# ---------------------------------------------------------------------------

class TestClassifyVolatility:
    def test_rising_volatility_classifies_volatile(self):
        # Realised vol rises from a low regime to a high regime: 40 low-variance
        # bars then a 20-bar ramp to high variance. The current (last) bar's vol
        # is the series maximum; the median sits in the low majority, so current
        # clears median * 2.0.
        rng = np.random.RandomState(1)
        low = rng.normal(0.0, 0.001, size=40)
        ramp_mag = np.linspace(0.02, 0.12, 20)
        high = ramp_mag * np.where(rng.random(20) < 0.5, 1.0, -1.0)
        rising = np.concatenate([low, high])

        out = classify_volatility(rising, window=20)
        assert out["regime"] == "volatile"
        assert out["current_volatility"] > out["median_volatility"] * 2.0
        assert out["window"] == 20
        # F3: pin the absolute magnitude, not just the label/relative comparison.
        # The last bar's vol is the population std of its trailing window bars
        # (rolling_vol[i] = std(returns[max(0,i-window+1):i+1])); the slice is
        # exactly window, not window+1. A wrong magnitude -- variance,
        # wrong ddof, percent-vs-decimal, or the v1 window+1 off-by-one --
        # still labels "volatile" but cannot match this value.
        last = len(rising) - 1
        expected_current = float(np.std(rising[max(0, last - 20 + 1): last + 1]))
        assert out["current_volatility"] == pytest.approx(expected_current)

    def test_lower_variance_counterpart_is_quiet(self):
        # Uniformly low variance: current vol == median vol -> quiet.
        rng = np.random.RandomState(2)
        calm = rng.normal(0.0, 0.001, size=60)
        out = classify_volatility(calm, window=20)
        assert out["regime"] == "quiet"

    def test_label_carries_its_window(self):
        rng = np.random.RandomState(3)
        r = rng.normal(0.0, 0.01, size=50)
        out = classify_volatility(r, window=15)
        assert out["window"] == 15


# ---------------------------------------------------------------------------
# classify_trend -- required outcome
# ---------------------------------------------------------------------------

class TestClassifyTrend:
    def test_monotonic_rise_classifies_uptrend(self):
        # Monotonically rising returns: recent mean (MA20) exceeds long mean
        # (MA60) -> uptrend.
        rising = np.linspace(0.001, 0.01, 80)
        out = classify_trend(rising)
        assert out["regime"] == "uptrend"
        assert out["ma_short"] > out["ma_long"]
        assert out["short_window"] == 20
        assert out["long_window"] == 60

    def test_mean_reverting_series_is_not_uptrend(self):
        # Perfectly oscillating around zero: every even-length window has mean
        # exactly 0, so MA20 == MA60 -> neutral (not uptrend).
        reverting = np.array([0.01, -0.01] * 40)
        out = classify_trend(reverting)
        assert out["regime"] == "neutral"
        assert out["ma_short"] == pytest.approx(0.0)
        assert out["ma_long"] == pytest.approx(0.0)

    def test_declining_returns_classify_downtrend(self):
        declining = np.linspace(0.01, 0.001, 80)
        out = classify_trend(declining)
        assert out["regime"] == "downtrend"


# ---------------------------------------------------------------------------
# detect_regime_changes -- required outcome
# ---------------------------------------------------------------------------

class TestDetectRegimeChanges:
    def test_transition_at_actual_change_index(self):
        # Generating process changes at index 30: 30 low-variance bars then 15
        # high-variance bars. With window=3 the rolling vol jumps immediately at
        # index 30 (the first high-variance bar enters the trailing window); the
        # median stays pinned in the low majority, so bar 30 crosses median*2.0
        # into 'volatile'. The detector reports index 30 -- the change point --
        # not one window later.
        low = [0.001, -0.001] * 15           # 30 bars, tiny variance
        high = [0.2, -0.2] * 8               # 16 bars, large variance
        returns = low + high
        path = volatility_regime_path(returns, window=3)
        assert path[:30] == ["quiet"] * 30
        assert path[30] == "volatile"
        changes = detect_regime_changes(path)
        assert changes[0]["index"] == 30
        assert changes[0]["from_regime"] == "quiet"
        assert changes[0]["to_regime"] == "volatile"

    def test_no_changes_in_constant_regime(self):
        assert detect_regime_changes(["quiet", "quiet", "quiet"]) == []

    def test_multiple_changes_all_reported(self):
        path = ["quiet", "quiet", "volatile", "quiet", "volatile"]
        changes = detect_regime_changes(path)
        assert [c["index"] for c in changes] == [2, 3, 4]
        assert changes == [
            {"index": 2, "from_regime": "quiet", "to_regime": "volatile"},
            {"index": 3, "from_regime": "volatile", "to_regime": "quiet"},
            {"index": 4, "from_regime": "quiet", "to_regime": "volatile"},
        ]

    def test_single_element_no_changes(self):
        assert detect_regime_changes(["quiet"]) == []


# ---------------------------------------------------------------------------
# Failure paths -- each raises Unavailable
# ---------------------------------------------------------------------------

class TestFailurePaths:
    def test_series_shorter_than_window_raises(self):
        with pytest.raises(Unavailable, match="need >= 20"):
            classify_volatility([0.01, 0.02, 0.03], window=20)

    def test_constant_series_zero_variance_raises(self):
        with pytest.raises(Unavailable, match="zero variance"):
            classify_volatility([5.0] * 40, window=20)

    def test_nan_in_input_raises(self):
        series = [0.01] * 30 + [float("nan")]
        with pytest.raises(Unavailable, match="NaN"):
            classify_volatility(series, window=20)

    def test_inf_in_input_raises(self):
        # F1: +inf must be refused. np.isnan(inf) is False, so the old NaN-only
        # guard passed it; np.std then returned NaN and the bar was labelled
        # "quiet". inf is a realistic return (zero price -> inf return).
        series = [0.01] * 30 + [float("inf")]
        with pytest.raises(Unavailable, match="non-finite"):
            classify_volatility(series, window=20)

    def test_neg_inf_in_input_raises(self):
        # F1: -inf must be refused for the same reason.
        series = [0.01] * 30 + [float("-inf")]
        with pytest.raises(Unavailable, match="non-finite"):
            classify_volatility(series, window=20)

    def test_window_zero_raises(self):
        with pytest.raises(Unavailable, match="window must be >= 2"):
            classify_volatility([0.01] * 40, window=0)

    def test_window_one_raises(self):
        with pytest.raises(Unavailable, match="window must be >= 2"):
            classify_volatility([0.01] * 40, window=1)

    def test_trend_short_window_below_two_raises(self):
        with pytest.raises(Unavailable, match="short_window must be >= 2"):
            classify_trend([0.01] * 80, short_window=1, long_window=60)

    def test_trend_series_shorter_than_long_window_raises(self):
        with pytest.raises(Unavailable, match="need >= 60"):
            classify_trend([0.01] * 30, short_window=20, long_window=60)

    def test_trend_constant_series_raises(self):
        with pytest.raises(Unavailable, match="zero variance"):
            classify_trend([0.05] * 80)

    def test_trend_nan_raises(self):
        series = [0.01] * 79 + [float("nan")]
        with pytest.raises(Unavailable, match="NaN"):
            classify_trend(series)

    def test_trend_inf_raises(self):
        # F1: classify_trend shares _validate, so inf must be refused here too.
        series = [0.01] * 79 + [float("inf")]
        with pytest.raises(Unavailable, match="non-finite"):
            classify_trend(series)

    def test_trend_short_window_exceeds_series_raises(self):
        # F2: short_window > len(series) >= long_window. The old code validated
        # only against long_window, so rolling(short_window).mean().iloc[-1] was
        # NaN; both ma_short > ma_long and < were False, so the branch fell
        # through to "neutral" -- a regime label emitted with a NaN MA. Must
        # refuse instead.
        with pytest.raises(Unavailable, match="need >= 60"):
            classify_trend(
                np.linspace(0.001, 0.01, 50), short_window=60, long_window=20
            )

    def test_volatility_regime_path_refuses_zero_variance(self):
        # F4: the zero-variance refusal was covered only transitively via
        # classify_volatility. Exercise the path function directly.
        with pytest.raises(Unavailable, match="zero variance"):
            volatility_regime_path([5.0] * 40, window=20)
