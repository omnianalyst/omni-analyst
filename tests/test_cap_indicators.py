"""Behaviour tests for the technical-indicator capabilities.

Every assertion is on a constructed series with a known answer, not on
shape. Every failure path (series shorter than the period, period of zero
or one, mismatched input lengths, NaN in the input, negative volume)
raises ``Unavailable``. Where v1 substituted a fabricated default -- a
neutral 50 for a zero-range stochastic, a 100 for a flat-window RSI, an
all-``None`` list for an undersized series -- this module raises or
returns ``None`` at the offending index instead.

There is no v1 test file for ``technical_indicators`` (verified: nothing
under ``../software/backend/tests/`` references it), so the work order's
required outcome is the oracle.
"""

import numpy as np
import pytest

from omni.capabilities.indicators import (
    atr,
    bollinger_bands,
    ema,
    macd,
    obv,
    rsi,
    sma,
    stochastic,
    vwap,
)
from omni.ingest.protocol import Unavailable

# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------

class TestSma:
    def test_constant_series_is_that_constant(self):
        out = sma([50.0] * 30, period=20)
        assert len(out) == 30
        assert out[:19] == [None] * 19
        for v in out[19:]:
            assert v == pytest.approx(50.0)

    def test_linear_ramp_equals_window_midpoint(self):
        n, period = 20, 5
        prices = [float(i + 1) for i in range(n)]
        out = sma(prices, period=period)
        assert out[: period - 1] == [None] * (period - 1)
        for i in range(period - 1, n):
            window = prices[i - period + 1 : i + 1]
            assert out[i] == pytest.approx(sum(window) / period)
        # midpoint of the final window [16,17,18,19,20]
        assert out[-1] == pytest.approx(18.0)

    def test_matches_direct_sum_over_window(self):
        rng = np.random.RandomState(0)
        prices = rng.uniform(90, 110, size=40).tolist()
        period = 12
        out = sma(prices, period=period)
        for i in range(period - 1, len(prices)):
            window = prices[i - period + 1 : i + 1]
            assert out[i] == pytest.approx(sum(window) / period)


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

class TestEma:
    def test_constant_series_is_that_constant(self):
        out = ema([50.0] * 30, period=20)
        assert len(out) == 30
        assert out[:19] == [None] * 19
        for v in out[19:]:
            assert v == pytest.approx(50.0)

    def test_seed_is_sma_of_first_period(self):
        prices = [float(i + 1) for i in range(10)]
        out = ema(prices, period=5)
        assert out[:4] == [None] * 4
        assert out[4] == pytest.approx(sum(prices[:5]) / 5)

    def test_recursion_matches_formula(self):
        prices = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
        period = 3
        m = 2.0 / (period + 1)
        out = ema(prices, period=period)
        prev = sum(prices[:3]) / 3
        assert out[2] == pytest.approx(prev)
        for i in range(period, len(prices)):
            prev = (prices[i] * m) + (prev * (1 - m))
            assert out[i] == pytest.approx(prev)


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

class TestRsi:
    def test_monotonically_rising_is_100(self):
        out = rsi([float(i + 1) for i in range(30)], period=14)
        assert out[:14] == [None] * 14
        for v in out[14:]:
            assert v == pytest.approx(100.0)

    def test_monotonically_falling_is_0(self):
        out = rsi([float(30 - i) for i in range(30)], period=14)
        assert out[:14] == [None] * 14
        for v in out[14:]:
            assert v == pytest.approx(0.0)

    def test_flat_series_returns_none_at_each_index(self):
        out = rsi([100.0] * 30, period=14)
        assert out[:14] == [None] * 14
        for v in out[14:]:
            assert v is None

    def test_alternating_equal_magnitudes_is_50_at_first_window(self):
        prices = [100.0 if i % 2 == 0 else 101.0 for i in range(30)]
        out = rsi(prices, period=14)
        assert out[14] == pytest.approx(50.0)

    def test_wilder_smoothing_recursion_pinned(self):
        # Mixed gain/loss series where post-seed values depend on the Wilder
        # update avg := (avg*(period-1) + gain) / period.  Every existing RSI
        # test only exercises seed-determined paths (all-rising, all-falling,
        # all-flat, or the seed index alone); a frozen-smoothing impl passes
        # all four.  This test fails against frozen smoothing from out[15] on.
        #
        # prices step +2 then -1 alternately; period 14.
        # seed: 7 gains of 2, 7 losses of 1 -> avg_gain=1.0, avg_loss=0.5.
        # Subsequent updates fold gains[14..] = [2,0,2,0,2,0] and
        # losses[14..] = [0,1,0,1,0,1] into the running averages.
        prices = [
            100.0, 102.0, 101.0, 103.0, 102.0, 104.0, 103.0, 105.0,
            104.0, 106.0, 105.0, 107.0, 106.0, 108.0, 107.0, 109.0,
            108.0, 110.0, 109.0, 111.0,
        ]
        out = rsi(prices, period=14)
        assert out[:14] == [None] * 14
        assert out[14] == pytest.approx(66.6667, abs=1e-4)
        assert out[15] == pytest.approx(69.7674, abs=1e-4)
        assert out[16] == pytest.approx(66.4395, abs=1e-4)
        assert out[17] == pytest.approx(69.5663, abs=1e-4)
        assert out[18] == pytest.approx(66.2430, abs=1e-4)
        assert out[19] == pytest.approx(69.3923, abs=1e-4)


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

class TestMacd:
    def test_constant_series_is_zero(self):
        out = macd([50.0] * 60, fast_period=12, slow_period=26, signal_period=9)
        for key in ("macd", "signal", "histogram"):
            assert len(out[key]) == 60
        # MACD line first defined at slow_period-1 = 25; signal line (EMA of 9
        # MACD values) first defined at 25 + (9-1) = 33; histogram needs both.
        assert out["macd"][24] is None
        for v in out["macd"][25:]:
            assert v == pytest.approx(0.0, abs=1e-9)
        assert out["signal"][32] is None
        for v in out["signal"][33:]:
            assert v == pytest.approx(0.0, abs=1e-9)
        assert out["histogram"][32] is None
        for v in out["histogram"][33:]:
            assert v == pytest.approx(0.0, abs=1e-9)

    def test_macd_line_is_fast_minus_slow(self):
        prices = [float(i + 1) for i in range(50)]
        out = macd(prices, fast_period=5, slow_period=10, signal_period=3)
        ef = ema(prices, period=5)
        es = ema(prices, period=10)
        for i in range(len(prices)):
            if ef[i] is None or es[i] is None:
                assert out["macd"][i] is None
            else:
                assert out["macd"][i] == pytest.approx(ef[i] - es[i])

    def test_histogram_is_macd_minus_signal(self):
        prices = [float(i + 1) for i in range(50)]
        out = macd(prices, fast_period=5, slow_period=10, signal_period=3)
        for m, s, h in zip(out["macd"], out["signal"], out["histogram"]):
            if m is not None and s is not None:
                assert h == pytest.approx(m - s)
            else:
                assert h is None


# ---------------------------------------------------------------------------
# Bollinger bands
# ---------------------------------------------------------------------------

class TestBollinger:
    def test_zero_variance_collapses_to_zero_width_band(self):
        out = bollinger_bands([50.0] * 30, period=20)
        for key in ("upper", "middle", "lower"):
            assert len(out[key]) == 30
            assert out[key][:19] == [None] * 19
        for u, m, l in zip(out["upper"][19:], out["middle"][19:], out["lower"][19:]):
            assert u == pytest.approx(50.0)
            assert m == pytest.approx(50.0)
            assert l == pytest.approx(50.0)

    def test_known_width_matches_formula(self):
        prices = [float(i + 1) for i in range(20)]
        period = 5
        out = bollinger_bands(prices, period=period, num_std=2.0)
        for i in range(period - 1, len(prices)):
            window = prices[i - period + 1 : i + 1]
            mean = sum(window) / period
            std = float(np.std(window))
            assert out["middle"][i] == pytest.approx(mean)
            assert out["upper"][i] == pytest.approx(mean + 2.0 * std)
            assert out["lower"][i] == pytest.approx(mean - 2.0 * std)


# ---------------------------------------------------------------------------
# Stochastic
# ---------------------------------------------------------------------------

class TestStochastic:
    def test_zero_range_returns_none_not_50(self):
        out = stochastic(
            [100.0] * 20, [100.0] * 20, [100.0] * 20, k_period=14, d_period=3
        )
        assert out["k"][:13] == [None] * 13
        for v in out["k"][13:]:
            assert v is None

    def test_known_k_value(self):
        high = [100.0] * 16
        low = [0.0] * 16
        close = [50.0] * 16
        out = stochastic(high, low, close, k_period=14, d_period=3)
        assert out["k"][14] == pytest.approx(50.0)

    def test_d_is_sma_of_k(self):
        high = [100.0] * 20
        low = [0.0] * 20
        close = [50.0] * 20
        out = stochastic(high, low, close, k_period=14, d_period=3)
        assert out["d"][16] == pytest.approx(50.0)

    def test_d_is_index_windowed_not_filter_aligned(self):
        # Mid-series zero-range gap at bar 5 produces a None %K.  %D must be
        # a trailing-window SMA *by index*; a window containing the gap None
        # yields None.  The old filter-then-realign code averaged bars 3,4,6
        # (skipping the gap) and returned 40.0 at index 6.
        high = [100.0, 100.0, 100.0, 50.0, 50.0, 50.0, 100.0]
        low = [0.0, 0.0, 0.0, 50.0, 50.0, 50.0, 0.0]
        close = [80.0, 60.0, 90.0, 50.0, 50.0, 50.0, 20.0]
        out = stochastic(high, low, close, k_period=3, d_period=3)
        assert out["k"] == [None, None, 90.0, 50.0, 50.0, None, 20.0]
        # bar 4: contiguous window [90, 50, 50] -> 190/3
        assert out["d"][4] == pytest.approx(190.0 / 3.0)
        # bar 5: window [50, 50, None] -> None
        assert out["d"][5] is None
        # bar 6: window [50, None, 20] -> None (was 40.0 with the bug)
        assert out["d"][6] is None


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

class TestAtr:
    def test_known_value_constant_spread(self):
        spread = 5.0
        high = [105.0] * 16
        low = [100.0] * 16
        close = [102.0] * 16
        out = atr(high, low, close, period=14)
        assert out[:14] == [None] * 14
        assert out[14] == pytest.approx(spread)
        assert out[15] == pytest.approx(spread)

    def test_wilder_smoothing_step(self):
        high = [105.0, 106.0, 104.0, 108.0]
        low = [100.0, 101.0, 99.0, 102.0]
        close = [102.0, 103.0, 101.0, 106.0]
        period = 2
        out = atr(high, low, close, period=period)
        tr = [
            0.0,
            max(106 - 101, abs(106 - 102), abs(101 - 102)),
            max(104 - 99, abs(104 - 103), abs(99 - 103)),
            max(108 - 102, abs(108 - 101), abs(102 - 101)),
        ]
        seed = sum(tr[1:3]) / period
        assert out[2] == pytest.approx(seed)
        expected = ((seed * (period - 1)) + tr[3]) / period
        assert out[3] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

class TestVwap:
    def test_uniform_volume_equals_simple_mean(self):
        prices = [10.0, 20.0, 30.0]
        out = vwap(prices, [1.0, 1.0, 1.0])
        assert out[-1] == pytest.approx(20.0)
        assert out[0] == pytest.approx(10.0)

    def test_typical_price_with_explicit_high_low(self):
        high = [12.0, 22.0, 33.0]
        low = [8.0, 18.0, 27.0]
        close = [10.0, 20.0, 30.0]
        out = vwap(close, [1.0, 1.0, 1.0], high=high, low=low)
        typical = [(h + l + c) / 3 for h, l, c in zip(high, low, close)]
        assert out[-1] == pytest.approx(sum(typical) / len(typical))

    def test_weighted_by_volume(self):
        prices = [10.0, 20.0]
        out = vwap(prices, [3.0, 1.0])
        assert out[-1] == pytest.approx((10.0 * 3 + 20.0 * 1) / 4.0)


# ---------------------------------------------------------------------------
# OBV
# ---------------------------------------------------------------------------

class TestObv:
    def test_final_equals_signed_volume_sum(self):
        prices = [100.0, 101.0, 100.0, 102.0, 101.0]
        volumes = [10.0, 20.0, 30.0, 40.0, 50.0]
        out = obv(prices, volumes)
        assert out[0] == 0.0
        assert out[-1] == pytest.approx(-20.0)
        assert len(out) == 5

    def test_flat_prices_hold_zero(self):
        prices = [100.0, 100.0, 100.0]
        volumes = [10.0, 20.0, 30.0]
        out = obv(prices, volumes)
        assert all(v == 0.0 for v in out)

    def test_cumulative_path(self):
        prices = [10.0, 11.0, 12.0, 11.0]
        volumes = [5.0, 7.0, 3.0, 9.0]
        out = obv(prices, volumes)
        assert out == [0.0, 7.0, 10.0, 1.0]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

class TestPeriodBelowTwo:
    @pytest.mark.parametrize(
        "fn",
        [
            lambda: sma([1.0] * 5, period=1),
            lambda: ema([1.0] * 5, period=1),
            lambda: rsi([1.0] * 5, period=1),
            lambda: bollinger_bands([1.0] * 5, period=1),
            lambda: stochastic([1.0] * 5, [1.0] * 5, [1.0] * 5, k_period=1),
            lambda: atr([1.0] * 5, [1.0] * 5, [1.0] * 5, period=1),
        ],
    )
    def test_period_one_raises(self, fn):
        with pytest.raises(Unavailable, match="period must be >= 2"):
            fn()

    def test_period_zero_raises(self):
        with pytest.raises(Unavailable, match="period must be >= 2"):
            sma([1.0] * 5, period=0)

    def test_macd_period_one_raises(self):
        with pytest.raises(Unavailable, match="period must be >= 2"):
            macd([1.0] * 30, fast_period=1, slow_period=26, signal_period=9)


class TestSeriesShorterThanPeriod:
    def test_sma(self):
        with pytest.raises(Unavailable, match="need >= 10"):
            sma([1.0] * 5, period=10)

    def test_ema(self):
        with pytest.raises(Unavailable, match="need >= 10"):
            ema([1.0] * 5, period=10)

    def test_rsi_needs_period_plus_one(self):
        with pytest.raises(Unavailable, match="need >= 15"):
            rsi([1.0] * 14, period=14)

    def test_macd(self):
        with pytest.raises(Unavailable, match="need >= 26"):
            macd([1.0] * 10, fast_period=12, slow_period=26, signal_period=9)

    def test_bollinger(self):
        with pytest.raises(Unavailable, match="need >= 20"):
            bollinger_bands([1.0] * 5, period=20)

    def test_stochastic(self):
        with pytest.raises(Unavailable, match="need >= 14"):
            stochastic([1.0] * 5, [1.0] * 5, [1.0] * 5, k_period=14)

    def test_atr_needs_period_plus_one(self):
        with pytest.raises(Unavailable, match="need >= 15"):
            atr([1.0] * 14, [1.0] * 14, [1.0] * 14, period=14)


class TestMismatchedLengths:
    def test_stochastic(self):
        with pytest.raises(Unavailable, match="mismatched"):
            stochastic([1.0] * 10, [1.0] * 9, [1.0] * 10, k_period=5)

    def test_atr(self):
        with pytest.raises(Unavailable, match="mismatched"):
            atr([1.0] * 10, [1.0] * 9, [1.0] * 10, period=5)

    def test_vwap(self):
        with pytest.raises(Unavailable, match="mismatched"):
            vwap([1.0] * 10, [1.0] * 9)

    def test_obv(self):
        with pytest.raises(Unavailable, match="mismatched"):
            obv([1.0] * 10, [1.0] * 9)

    def test_vwap_mismatched_high(self):
        with pytest.raises(Unavailable, match="mismatched"):
            vwap([1.0] * 10, [1.0] * 10, high=[1.0] * 9)


class TestNanInInput:
    def test_sma(self):
        with pytest.raises(Unavailable, match="NaN"):
            sma([1.0, float("nan")] + [1.0] * 20, period=10)

    def test_ema(self):
        with pytest.raises(Unavailable, match="NaN"):
            ema([1.0, float("nan")] + [1.0] * 20, period=10)

    def test_rsi(self):
        with pytest.raises(Unavailable, match="NaN"):
            rsi([1.0, float("nan")] + [1.0] * 20, period=10)

    def test_bollinger(self):
        with pytest.raises(Unavailable, match="NaN"):
            bollinger_bands([1.0, float("nan")] + [1.0] * 20, period=10)

    def test_stochastic(self):
        with pytest.raises(Unavailable, match="NaN"):
            stochastic(
                [1.0, float("nan")] + [1.0] * 15,
                [1.0] * 17,
                [1.0] * 17,
                k_period=10,
            )

    def test_atr(self):
        with pytest.raises(Unavailable, match="NaN"):
            atr(
                [1.0, float("nan")] + [1.0] * 15,
                [1.0] * 17,
                [1.0] * 17,
                period=10,
            )

    def test_vwap(self):
        with pytest.raises(Unavailable, match="NaN"):
            vwap([1.0, float("nan"), 3.0], [1.0, 1.0, 1.0])

    def test_obv(self):
        with pytest.raises(Unavailable, match="NaN"):
            obv([1.0, float("nan"), 3.0], [1.0, 1.0, 1.0])


class TestNegativeVolume:
    def test_vwap(self):
        with pytest.raises(Unavailable, match="negative volume"):
            vwap([10.0, 20.0], [1.0, -5.0])

    def test_obv(self):
        with pytest.raises(Unavailable, match="negative volume"):
            obv([10.0, 20.0], [1.0, -5.0])
