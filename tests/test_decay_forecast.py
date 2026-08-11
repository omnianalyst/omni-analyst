"""Tests for the decay forecast's pure statistics.

The discriminating test is the regime-break one: a series whose recent third
defies the early-window trend must be detected as a break, because the entire
value of the forecast rests on the holdout catching exactly that case.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.decay_forecast import crossover_month, fit_and_validate, fit_linear


def test_fit_recovers_a_known_linear_slope():
    months = np.arange(12, dtype=float)
    values = 20.0 - 1.0 * months
    fit = fit_linear(months, values)
    assert abs(fit.slope_pp_per_month - (-1.0)) < 1e-9
    assert abs(fit.intercept_pct - 20.0) < 1e-9
    assert fit.r_squared > 0.999


def test_validation_passes_when_the_recent_third_continues_the_trend():
    months = np.arange(12, dtype=float)
    values = 20.0 - 1.0 * months
    v = fit_and_validate(months, values)
    assert v.holdout_n == 4
    assert abs(v.holdout_bias_pp) < 1e-9
    assert v.holdout_rmse_pp < 1e-9
    assert v.recent_third_within_band is True


def test_validation_detects_a_regime_break_in_the_recent_third():
    # The first eight points decline at -1/month. The last four jump back up.
    # A validator that cannot flag this is the thing this forecast exists to be.
    months = np.arange(12, dtype=float)
    values = np.array([20, 19, 18, 17, 16, 15, 14, 13, 18, 17, 16, 15], dtype=float)
    v = fit_and_validate(months, values)
    assert v.holdout_n == 4
    assert v.holdout_bias_pp > 5.0
    assert v.recent_third_within_band is False


def test_crossover_month_for_a_declining_line():
    fit = fit_linear(np.arange(10, dtype=float), 20.0 - 2.0 * np.arange(10, dtype=float))
    assert crossover_month(fit, 6.0) == 7.0


def test_no_crossover_for_a_rising_or_flat_line():
    rising = fit_linear(np.arange(10, dtype=float), 5.0 + 1.0 * np.arange(10, dtype=float))
    assert crossover_month(rising, 6.0) is None
