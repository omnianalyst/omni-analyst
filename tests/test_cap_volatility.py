"""Behaviour tests for the volatility capabilities.

Every assertion is on a constructed series with a known answer, not on shape.
Every failure path (window longer than the series, NaN, non-positive prices,
high < low, close outside [low, high], annualisation of zero, zero variance)
raises ``Unavailable`` -- v1's ``_simple_volatility`` returned 0.0 on a flat
window and its ``_get_default_volatility`` fabricated per-asset values; both
are how covered-looking-but-fabricated numbers enter the store.

There is no v1 test file for ``volatility_calculator`` (verified: nothing
under ``../software/backend/tests/`` references it), so the work order's
required outcome is the oracle.
"""

import math

import numpy as np
import pytest

from omni.capabilities import regime
from omni.capabilities.volatility import (
    DEFAULT_LAMBDA,
    Bar,
    close_to_close,
    ewma,
    garman_klass,
    parkinson,
    rogers_satchell,
    volatility_of_volatility,
)
from omni.ingest.protocol import Unavailable


def _noisy_prices(n: int, seed: int = 0, sigma: float = 0.02) -> np.ndarray:
    rng = np.random.RandomState(seed)
    log_ret = rng.normal(0.0, sigma, size=n - 1)
    return 100.0 * np.exp(np.concatenate([[0.0], np.cumsum(log_ret)]))


# ---------------------------------------------------------------------------
# close_to_close
# ---------------------------------------------------------------------------

class TestCloseToClose:
    def test_constant_log_return_raises_zero_variance(self):
        # Powers of 2: every consecutive ratio is exactly 2.0, so every log
        # return is exactly ln(2) -- bit-identical, ptp == 0. (exp-based growth
        # does not round-trip: log(exp(r)) is not bit-equal to r.)
        prices = 100.0 * (2.0 ** np.arange(40))
        with pytest.raises(Unavailable, match="zero variance"):
            close_to_close(prices, window=20, annualisation=252)

    def test_flat_price_is_constant_zero_return_and_raises(self):
        with pytest.raises(Unavailable, match="zero variance"):
            close_to_close([100.0] * 40, window=20, annualisation=252)

    def test_population_std_matches_formula(self):
        # Construct prices with known log returns so std is exact.
        returns = np.array([0.01, -0.02, 0.015, 0.005, -0.01, 0.02, -0.005, 0.01])
        prices = 100.0 * np.exp(np.concatenate([[0.0], np.cumsum(returns)]))
        window = len(prices)  # use every return
        expected = float(np.std(returns, ddof=0)) * math.sqrt(252)
        assert close_to_close(
            prices, window=window, annualisation=252
        ) == pytest.approx(expected)

    def test_sample_std_via_ddof_one_differs(self):
        returns = np.array([0.01, -0.02, 0.015, 0.005, -0.01, 0.02, -0.005, 0.01])
        prices = 100.0 * np.exp(np.concatenate([[0.0], np.cumsum(returns)]))
        window = len(prices)
        pop = close_to_close(prices, window=window, annualisation=1, ddof=0)
        samp = close_to_close(prices, window=window, annualisation=1, ddof=1)
        assert pop != pytest.approx(samp)
        assert samp == pytest.approx(float(np.std(returns, ddof=1)))


# ---------------------------------------------------------------------------
# Annualisation invariance -- the substance of "state your period"
# ---------------------------------------------------------------------------

class TestAnnualisationInvariance:
    def test_daily_sqrt252_equals_weekly_sqrt52_for_same_process(self):
        # Same innovations observed at two frequencies. Weekly return std must
        # be daily std scaled by sqrt(252/52) so that
        #   sigma_d * sqrt(252) == sigma_w * sqrt(52)
        # exactly (the sqrt-time scaling law of a GBM). We scale the same
        # return vector, which preserves "same underlying process".
        rng = np.random.RandomState(7)
        daily_ret = rng.normal(0.0, 0.02, size=60)
        weekly_ret = daily_ret * math.sqrt(252.0 / 52.0)

        daily_px = 100.0 * np.exp(np.concatenate([[0.0], np.cumsum(daily_ret)]))
        weekly_px = 100.0 * np.exp(np.concatenate([[0.0], np.cumsum(weekly_ret)]))

        vol_d = close_to_close(daily_px, window=len(daily_px), annualisation=252)
        vol_w = close_to_close(weekly_px, window=len(weekly_px), annualisation=52)
        assert vol_d == pytest.approx(vol_w, rel=1e-12)


# ---------------------------------------------------------------------------
# EWMA -- lambda -> 1 approaches the equally-weighted result
# ---------------------------------------------------------------------------

class TestEwma:
    def test_lambda_near_one_approaches_population_std(self):
        prices = _noisy_prices(200, seed=3, sigma=0.02)
        window = len(prices)
        # lambda very close to 1 -> weights nearly uniform over the window ->
        # sigma^2 -> mean(r^2), the population variance of zero-mean returns.
        ewma_hi = ewma(prices, window=window, annualisation=1, lambda_=0.99999)
        equally_weighted = close_to_close(
            prices, window=window, annualisation=1, ddof=0
        )
        assert ewma_hi == pytest.approx(equally_weighted, rel=5e-3)

    def test_lambda_low_weights_only_recent(self):
        # lambda -> 0 concentrates all weight on the last return; EWMA variance
        # approaches the last squared return.
        prices = _noisy_prices(50, seed=4, sigma=0.02)
        last_ret = float(np.log(prices[-1] / prices[-2]))
        out = ewma(prices, window=len(prices), annualisation=1, lambda_=1e-6)
        assert out == pytest.approx(abs(last_ret), rel=1e-3)

    def test_default_lambda_is_riskmetrics(self):
        assert DEFAULT_LAMBDA == 0.94

    def test_lambda_out_of_range_raises(self):
        prices = _noisy_prices(40, seed=5)
        with pytest.raises(Unavailable, match="lambda_"):
            ewma(prices, window=len(prices), annualisation=252, lambda_=1.0)
        with pytest.raises(Unavailable, match="lambda_"):
            ewma(prices, window=len(prices), annualisation=252, lambda_=0.0)


# ---------------------------------------------------------------------------
# OHLC estimators
# ---------------------------------------------------------------------------

def _bars_from_rng(n: int, seed: int, base: float = 100.0) -> list[Bar]:
    rng = np.random.RandomState(seed)
    bars = []
    for _ in range(n):
        close = base * (1.0 + rng.normal(0.0, 0.01))
        open_ = base * (1.0 + rng.normal(0.0, 0.01))
        hi = max(open_, close) * (1.0 + abs(rng.normal(0.0, 0.005)))
        lo = min(open_, close) * (1.0 - abs(rng.normal(0.0, 0.005)))
        bars.append(Bar(open=float(open_), high=float(hi), low=float(lo), close=float(close)))
    return bars


class TestParkinson:
    def test_flat_high_equal_low_raises_zero_variance(self):
        bars = [Bar(open=100.0, high=100.0, low=100.0, close=100.0)] * 20
        with pytest.raises(Unavailable, match="zero variance"):
            parkinson(bars, window=20, annualisation=252)

    def test_known_answer(self):
        # A bar with H=110, L=100: per-bar Parkinson variance =
        # (1/(4 ln2)) * (ln(110/100))^2.  Construct a window of identical bars
        # so the mean is that single-bar value.
        h, l = 110.0, 100.0
        bars = [Bar(open=100.0, high=h, low=l, close=100.0)] * 10
        per_bar = (1.0 / (4.0 * math.log(2.0))) * math.log(h / l) ** 2
        expected = math.sqrt(per_bar * 252)
        assert parkinson(bars, window=10, annualisation=252) == pytest.approx(expected)


class TestGarmanKlass:
    def test_known_answer(self):
        o, h, l, c = 100.0, 110.0, 95.0, 105.0
        bars = [Bar(open=o, high=h, low=l, close=c)] * 10
        c_const = 2.0 * math.log(2.0) - 1.0
        per_bar = 0.5 * math.log(h / l) ** 2 - c_const * math.log(c / o) ** 2
        expected = math.sqrt(per_bar * 252)
        assert garman_klass(bars, window=10, annualisation=252) == pytest.approx(expected)


class TestRogersSatchell:
    def test_known_answer(self):
        o, h, l, c = 100.0, 110.0, 95.0, 105.0
        bars = [Bar(open=o, high=h, low=l, close=c)] * 10
        per_bar = (
            math.log(h / c) * math.log(h / o)
            + math.log(l / c) * math.log(l / o)
        )
        expected = math.sqrt(per_bar * 252)
        assert rogers_satchell(bars, window=10, annualisation=252) == pytest.approx(expected)

    def test_rs_non_negative_for_coherent_bars(self):
        bars = _bars_from_rng(60, seed=9)
        out = rogers_satchell(bars, window=60, annualisation=252)
        assert out > 0.0


# ---------------------------------------------------------------------------
# volatility_of_volatility
# ---------------------------------------------------------------------------

class TestVolatilityOfVolatility:
    def test_constant_vol_series_raises(self):
        with pytest.raises(Unavailable, match="zero variance"):
            volatility_of_volatility([0.2] * 30, window=20, annualisation=252)

    def test_known_answer(self):
        vols = np.array([0.1, 0.2, 0.15, 0.3, 0.25, 0.2, 0.1, 0.05])
        expected = float(np.std(vols, ddof=0)) * math.sqrt(252)
        assert volatility_of_volatility(
            vols, window=len(vols), annualisation=252
        ) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Cross-module agreement: volatility.py and regime.py must not disagree
# ---------------------------------------------------------------------------

class TestRegimeAgreement:
    def test_close_to_close_ddof0_equals_regime_realised_vol(self):
        # Both modules, given the same return series, must produce the same
        # trailing-window population std. regime.realised_volatility operates
        # on returns and reports the rolling series; its last value is the
        # trailing population std. close_to_close operates on prices (log
        # returns) with ddof=0. Feeding matching inputs, they agree.
        rng = np.random.RandomState(21)
        returns = rng.normal(0.0, 0.02, size=60)
        prices = 100.0 * np.exp(np.concatenate([[0.0], np.cumsum(returns)]))

        regime_path = regime.realised_volatility(returns.tolist(), window=len(returns))
        regime_vol = float(regime_path[-1])
        vol = close_to_close(prices, window=len(prices), annualisation=1, ddof=0)
        assert vol == pytest.approx(regime_vol, rel=1e-12)


# ---------------------------------------------------------------------------
# Failure paths -- each raises Unavailable
# ---------------------------------------------------------------------------

class TestFailurePaths:
    def test_window_longer_than_series_raises(self):
        with pytest.raises(Unavailable, match="need >= 30"):
            close_to_close(_noisy_prices(10), window=30, annualisation=252)

    def test_nan_in_prices_raises(self):
        prices = _noisy_prices(40)
        prices[5] = float("nan")
        with pytest.raises(Unavailable, match="NaN"):
            close_to_close(prices, window=20, annualisation=252)

    def test_non_positive_price_raises(self):
        prices = _noisy_prices(40)
        prices[5] = 0.0
        with pytest.raises(Unavailable, match="non-positive"):
            close_to_close(prices, window=20, annualisation=252)

    def test_negative_price_raises(self):
        prices = _noisy_prices(40)
        prices[5] = -1.0
        with pytest.raises(Unavailable, match="non-positive"):
            close_to_close(prices, window=20, annualisation=252)

    def test_annualisation_zero_raises(self):
        with pytest.raises(Unavailable, match="annualisation must be > 0"):
            close_to_close(_noisy_prices(40), window=20, annualisation=0)

    def test_annualisation_negative_raises(self):
        with pytest.raises(Unavailable, match="annualisation must be > 0"):
            close_to_close(_noisy_prices(40), window=20, annualisation=-5)

    def test_window_below_two_raises(self):
        with pytest.raises(Unavailable, match="window must be >= 2"):
            close_to_close(_noisy_prices(40), window=1, annualisation=252)

    def test_high_below_low_raises(self):
        bars = [Bar(open=100.0, high=95.0, low=105.0, close=100.0)] * 20
        with pytest.raises(Unavailable, match="high < low"):
            parkinson(bars, window=20, annualisation=252)

    def test_close_above_high_raises(self):
        bars = [Bar(open=100.0, high=105.0, low=95.0, close=110.0)] * 20
        with pytest.raises(Unavailable, match="close outside"):
            garman_klass(bars, window=20, annualisation=252)

    def test_close_below_low_raises(self):
        bars = [Bar(open=100.0, high=105.0, low=95.0, close=90.0)] * 20
        with pytest.raises(Unavailable, match="close outside"):
            rogers_satchell(bars, window=20, annualisation=252)

    def test_open_outside_range_raises(self):
        bars = [Bar(open=110.0, high=105.0, low=95.0, close=100.0)] * 20
        with pytest.raises(Unavailable, match="open outside"):
            garman_klass(bars, window=20, annualisation=252)

    def test_non_positive_ohlc_raises(self):
        bars = [Bar(open=100.0, high=105.0, low=0.0, close=100.0)] * 20
        with pytest.raises(Unavailable, match="non-positive"):
            parkinson(bars, window=20, annualisation=252)

    def test_ohlc_window_longer_than_series_raises(self):
        bars = _bars_from_rng(5, seed=0)
        with pytest.raises(Unavailable, match="need >= 20"):
            parkinson(bars, window=20, annualisation=252)

    def test_vov_window_longer_than_series_raises(self):
        with pytest.raises(Unavailable, match="need >= 30"):
            volatility_of_volatility([0.1, 0.2, 0.3], window=30, annualisation=252)

    def test_vov_nan_raises(self):
        with pytest.raises(Unavailable, match="NaN"):
            volatility_of_volatility(
                [0.1, float("nan"), 0.3] + [0.2] * 20, window=20, annualisation=252
            )

    def test_vov_negative_reading_raises(self):
        with pytest.raises(Unavailable, match="negative volatility"):
            volatility_of_volatility(
                [0.1, -0.2, 0.3] + [0.2] * 20, window=20, annualisation=252
            )

    def test_vov_annualisation_zero_raises(self):
        with pytest.raises(Unavailable, match="annualisation must be > 0"):
            volatility_of_volatility(
                [0.1, 0.2, 0.3, 0.2, 0.15], window=4, annualisation=0
            )
