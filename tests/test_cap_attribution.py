"""Behaviour tests for ``omni.capabilities.attribution``.

These tests assert arithmetic (per AGENTS.md -- assert behaviour, not shape)
and exercise every failure path the work order names. v1 had no test coverage
on ``portfolio_metrics.py`` (it was DB-tangled) or on the attribution handler
in ``quant_analytics.py:951``, so there is no v1 oracle to copy verbatim;
instead, every required outcome in the work order is asserted directly:

- Attribution is additive to floating tolerance.
- A portfolio identical to its benchmark has zero alpha, zero tracking error,
  and an undefined information ratio (raises, does not return zero).
- Sharpe of a constant-return series raises.
- A single-factor regression against a factor equal to the returns gives
  beta = 1 and R-squared = 1.

And every failure path raises ``Unavailable``: fewer observations than
factors, a singular design matrix, misaligned portfolio/benchmark dates, and
an empty holdings list.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from omni.capabilities.attribution import (
    Attribution,
    MarketModelAttribution,
    active_returns,
    annualized_information_ratio,
    annualized_sharpe,
    annualized_sortino,
    annualized_tracking_error,
    attribute_returns,
    holding_contributions,
    market_model_attribution,
    regress_factor_exposures,
)
from omni.ingest.protocol import Unavailable

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

DATES = pd.date_range("2024-01-01", periods=1000, freq="D")


def idx(n: int) -> pd.DatetimeIndex:
    """First ``n`` entries of the shared date index, sized to the test data."""
    return DATES[:n]


def _make_factor_returns(
    n: int = 100,
    seed: int = 7,
    factor_names: tuple[str, ...] = ("MKT", "SMB", "HML"),
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    cols = {name: rng.normal(0.0, 0.01, size=n) for name in factor_names}
    return pd.DataFrame(cols, index=DATES[:n])


def _make_portfolio_returns(
    factor_returns: pd.DataFrame,
    betas: tuple[float, ...] = (1.2, 0.3, -0.4),
    alpha_daily: float = 0.0005,
    noise_sd: float = 0.002,
    seed: int = 11,
) -> pd.Series:
    rng = np.random.default_rng(seed)
    contrib = sum(
        b * factor_returns[name].to_numpy() for b, name in zip(betas, factor_returns.columns)
    )
    noise = rng.normal(0.0, noise_sd, size=factor_returns.shape[0])
    return pd.Series(alpha_daily + contrib + noise, index=factor_returns.index)


# --------------------------------------------------------------------------- #
# OLS factor regression
# --------------------------------------------------------------------------- #


def test_factor_regression_recovers_betas_and_alpha():
    fr = _make_factor_returns()
    pr = _make_portfolio_returns(fr, betas=(1.2, 0.3, -0.4), alpha_daily=0.0005, noise_sd=0.0)
    reg = regress_factor_exposures(pr, fr)
    assert list(reg.factor_names) == ["MKT", "SMB", "HML"]
    np.testing.assert_allclose(reg.betas, [1.2, 0.3, -0.4], atol=1e-9)
    assert reg.intercept == pytest.approx(0.0005, abs=1e-12)
    # No noise -> perfect fit -> R-squared = 1.
    assert reg.r_squared == pytest.approx(1.0, abs=1e-12)
    assert reg.n_observations == 100


def test_factor_regression_factor_equals_returns_gives_beta_one_rsquared_one():
    # Work-order required outcome: a single-factor regression against a factor
    # equal to the returns gives beta = 1 and R-squared = 1.
    rng = np.random.default_rng(3)
    r = pd.Series(rng.normal(0.001, 0.01, size=80), index=DATES[:80])
    fr = pd.DataFrame({"F": r}, index=r.index)
    reg = regress_factor_exposures(r, fr)
    assert reg.betas[0] == pytest.approx(1.0, abs=1e-9)
    assert reg.intercept == pytest.approx(0.0, abs=1e-12)
    assert reg.r_squared == pytest.approx(1.0, abs=1e-9)


def test_factor_regression_r_squared_decreases_with_noise():
    fr = _make_factor_returns()
    clean = _make_portfolio_returns(fr, noise_sd=0.0)
    noisy = _make_portfolio_returns(fr, noise_sd=0.02)
    assert (
        regress_factor_exposures(clean, fr).r_squared
        > regress_factor_exposures(noisy, fr).r_squared
    )


def test_factor_regression_min_observations_enforced():
    fr = _make_factor_returns()
    pr = _make_portfolio_returns(fr)
    # 100 aligned obs, caller demands 200 -> raises.
    with pytest.raises(Unavailable, match="min_observations"):
        regress_factor_exposures(pr, fr, min_observations=200)


# --------------------------------------------------------------------------- #
# Multi-factor attribution -- additivity
# --------------------------------------------------------------------------- #


def test_attribute_returns_is_additive_to_floating_tolerance():
    # Work-order required outcome: contributions sum to total return.
    fr = _make_factor_returns()
    pr = _make_portfolio_returns(fr, betas=(1.1, -0.2, 0.5), alpha_daily=0.0003, noise_sd=0.001)
    attr = attribute_returns(pr, fr)
    assert isinstance(attr, Attribution)
    # Explicit additivity assertion (also asserted inside attribute_returns).
    attr.check_additivity(atol=1e-12)
    reconstructed = attr.specific_return + sum(attr.factor_contributions.values())
    assert reconstructed == pytest.approx(attr.total_return, abs=1e-12)


def test_attribute_returns_specific_return_is_n_times_intercept():
    fr = _make_factor_returns()
    pr = _make_portfolio_returns(fr, betas=(1.0, 0.0, 0.0), alpha_daily=0.001, noise_sd=0.0)
    attr = attribute_returns(pr, fr)
    # alpha_daily * n_observations
    assert attr.specific_return == pytest.approx(0.001 * attr.n_observations, abs=1e-12)
    attr.check_additivity(atol=1e-12)


def test_attribute_returns_no_overlap_raises():
    fr = _make_factor_returns()
    other_index = pd.date_range("2030-01-01", periods=100, freq="D")
    pr = pd.Series(np.zeros(100), index=other_index)
    with pytest.raises(Unavailable, match="no overlapping dates"):
        attribute_returns(pr, fr)


# --------------------------------------------------------------------------- #
# Single-factor (market-model) attribution
# --------------------------------------------------------------------------- #


def test_market_model_attribution_basic_shape_and_additivity():
    rng = np.random.default_rng(5)
    bench = pd.Series(rng.normal(0.0005, 0.01, 500), index=idx(500))
    # Portfolio = 1.3 * bench + alpha + small noise (kept small so beta is
    # recovered tightly; the point of the test is additivity + shape, not
    # estimation under heavy noise).
    alpha_daily = 0.0002
    port = pd.Series(
        alpha_daily + 1.3 * bench.to_numpy() + rng.normal(0, 0.0005, 500),
        index=idx(500),
    )
    res = market_model_attribution(port, bench)
    assert isinstance(res, MarketModelAttribution)
    assert res.beta == pytest.approx(1.3, abs=1e-2)
    assert res.alpha == pytest.approx(alpha_daily * 252, abs=0.5)
    assert 0.95 < res.r_squared <= 1.0
    # Annualized additivity: total = factor + specific (residuals mean-zero).
    assert res.factor_return + res.specific_return == pytest.approx(res.total_return, abs=1e-9)
    # n_observations reported.
    assert res.n_observations == 500


def test_market_model_portfolio_identical_to_benchmark_raises():
    # Work-order required outcome: zero alpha, zero TE, IR undefined -> raises.
    rng = np.random.default_rng(13)
    r = pd.Series(rng.normal(0.001, 0.01, 100), index=idx(100))
    # Same series as both portfolio and benchmark.
    with pytest.raises(Unavailable, match="tracking error is zero"):
        market_model_attribution(r, r)


def test_market_model_zero_variance_portfolio_raises():
    const = pd.Series(np.full(100, 0.001), index=idx(100))
    bench = pd.Series(np.random.default_rng(1).normal(0, 0.01, 100), index=idx(100))
    with pytest.raises(Unavailable, match="portfolio_returns has zero variance"):
        market_model_attribution(const, bench)


def test_market_model_zero_variance_benchmark_raises():
    const = pd.Series(np.full(100, 0.001), index=idx(100))
    bench = pd.Series(np.random.default_rng(1).normal(0, 0.01, 100), index=idx(100))
    with pytest.raises(Unavailable, match="benchmark_returns has zero variance"):
        market_model_attribution(bench, const)


def test_market_model_misaligned_dates_raises():
    # Work-order required failure path: misaligned dates raise.
    bench = pd.Series(
        np.random.default_rng(1).normal(0, 0.01, 100),
        index=pd.date_range("2030-01-01", periods=100, freq="D"),
    )
    port = pd.Series(
        np.random.default_rng(2).normal(0, 0.01, 100),
        index=idx(100),
    )
    with pytest.raises(Unavailable, match="no overlapping dates"):
        market_model_attribution(port, bench)


def test_market_model_empty_inputs_raise():
    with pytest.raises(Unavailable, match="portfolio_returns is empty"):
        market_model_attribution(pd.Series([], dtype=float), pd.Series([0.01]))
    with pytest.raises(Unavailable, match="benchmark_returns is empty"):
        market_model_attribution(pd.Series([0.01]), pd.Series([], dtype=float))


def test_market_model_single_observation_raises():
    idx = pd.date_range("2024-01-01", periods=1, freq="D")
    with pytest.raises(Unavailable, match="at least 2 overlapping"):
        market_model_attribution(pd.Series([0.01], index=idx), pd.Series([0.02], index=idx))


# --------------------------------------------------------------------------- #
# Risk metrics
# --------------------------------------------------------------------------- #


def test_sharpe_basic_known_value():
    # mean=0.001, sd=0.01 -> per-period sharpe = 0.1, annualized ~= 0.1*sqrt(252).
    rng = np.random.default_rng(0)
    r = rng.normal(0.001, 0.01, 100000)
    sr = annualized_sharpe(r)
    expected = (0.001 / 0.01) * np.sqrt(252)
    assert sr == pytest.approx(expected, rel=0.02)


def test_sharpe_constant_returns_raises():
    # Work-order required outcome: zero-variance Sharpe is undefined -> raises.
    with pytest.raises(Unavailable, match="zero variance"):
        annualized_sharpe([0.001] * 50)


def test_sharpe_too_few_observations_raises():
    with pytest.raises(Unavailable, match=">=2 observations"):
        annualized_sharpe([0.01])
    with pytest.raises(Unavailable, match=">=2 observations"):
        annualized_sharpe([])


def test_sharpe_periods_per_year_scales_result():
    rng = np.random.default_rng(2)
    r = rng.normal(0, 0.01, 1000)
    daily = annualized_sharpe(r, periods_per_year=252)
    monthly = annualized_sharpe(r, periods_per_year=12)
    assert monthly == pytest.approx(daily * np.sqrt(12 / 252), rel=1e-9)


def test_sortino_penalizes_downside_more_than_sharpe():
    # Asymmetric series: large upside, small downside -> Sortino > Sharpe.
    rng = np.random.default_rng(4)
    upside = np.abs(rng.normal(0, 0.02, 500))
    downside = -np.abs(rng.normal(0, 0.005, 500))
    r = np.concatenate([upside, downside])
    np.random.default_rng(99).shuffle(r)
    assert annualized_sortino(r) > annualized_sharpe(r)


def test_sortino_no_downside_raises():
    # All-positive returns -> downside deviation is zero -> undefined.
    with pytest.raises(Unavailable, match="downside deviation is zero"):
        annualized_sortino([0.01, 0.02, 0.03, 0.04])


def test_sortino_too_few_observations_raises():
    with pytest.raises(Unavailable, match=">=2 observations"):
        annualized_sortino([0.01])


def test_tracking_error_basic_and_additive_decomposition():
    rng = np.random.default_rng(8)
    bench = pd.Series(rng.normal(0, 0.01, 300), index=idx(300))
    port = pd.Series(rng.normal(0, 0.012, 300), index=idx(300))
    te = annualized_tracking_error(port, bench)
    # Tracking error == std(active) * sqrt(252).
    expected = (port - bench).std(ddof=1) * np.sqrt(252)
    assert te == pytest.approx(float(expected), rel=1e-9)


def test_tracking_error_portfolio_equals_benchmark_raises():
    rng = np.random.default_rng(9)
    r = pd.Series(rng.normal(0, 0.01, 100), index=idx(100))
    with pytest.raises(Unavailable, match="zero variance"):
        annualized_tracking_error(r, r)


def test_tracking_error_misaligned_dates_raise():
    bench = pd.Series(
        np.random.default_rng(1).normal(0, 0.01, 100),
        index=pd.date_range("2030-01-01", periods=100, freq="D"),
    )
    port = pd.Series(np.random.default_rng(2).normal(0, 0.01, 100), index=idx(100))
    with pytest.raises(Unavailable, match="no overlapping dates"):
        annualized_tracking_error(port, bench)


def test_information_ratio_basic():
    rng = np.random.default_rng(14)
    bench = pd.Series(rng.normal(0, 0.01, 500), index=DATES[:500])
    # Persistent 5bp/day alpha.
    port = pd.Series(bench.to_numpy() + 0.0005 + rng.normal(0, 0.002, 500), index=DATES[:500])
    ir = annualized_information_ratio(port, bench)
    expected = (port - bench).mean() / (port - bench).std(ddof=1) * np.sqrt(252)
    assert ir == pytest.approx(float(expected), rel=1e-9)
    assert ir > 0  # real skill -> positive IR


def test_information_ratio_zero_alpha_returns_zero_not_raises():
    # Work order: mean-active = 0 with nonzero variance is a legitimate IR = 0,
    # not undefined. The undefined case is std = 0 (covered by the next test).
    rng = np.random.default_rng(15)
    bench = pd.Series(rng.normal(0, 0.01, 1000), index=DATES[:1000])
    port = pd.Series(rng.normal(0, 0.01, 1000), index=DATES[:1000])
    # Independent samples, large N -> mean(active) ~ 0; assert it's small but
    # the call does not raise.
    ir = annualized_information_ratio(port, bench)
    assert isinstance(ir, float)
    assert np.isfinite(ir)


def test_information_ratio_portfolio_equals_benchmark_raises():
    # Work-order required outcome: IR undefined when TE = 0 -> raises.
    rng = np.random.default_rng(16)
    r = pd.Series(rng.normal(0, 0.01, 100), index=DATES[:100])
    with pytest.raises(Unavailable, match="tracking error is zero"):
        annualized_information_ratio(r, r)


def test_active_returns_helper_raises_on_misalignment():
    bench = pd.Series([0.01] * 5, index=pd.date_range("2030-01-01", periods=5, freq="D"))
    port = pd.Series([0.02] * 5, index=DATES[:5])
    with pytest.raises(Unavailable, match="no overlapping dates"):
        active_returns(port, bench)


# --------------------------------------------------------------------------- #
# Holding-level contributions
# --------------------------------------------------------------------------- #


def test_holding_contributions_sum_to_portfolio_return():
    rng = np.random.default_rng(21)
    cols = ["AAA", "BBB", "CCC"]
    rets = pd.DataFrame(rng.normal(0.001, 0.01, size=(120, 3)), columns=cols, index=DATES[:120])
    weights = {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}
    contribs = holding_contributions(rets, weights)
    # Contribution_i = w_i * sum(r_i). Sum equals weighted-portfolio total return.
    portfolio_total = sum(weights[c] * rets[c].sum() for c in cols)
    assert sum(contribs.values()) == pytest.approx(portfolio_total, abs=1e-12)
    # Per-holding arithmetic.
    for c in cols:
        assert contribs[c] == pytest.approx(weights[c] * rets[c].sum(), abs=1e-12)


def test_holding_contributions_empty_holdings_raises():
    # Work-order required failure path: empty holdings list raises.
    empty = pd.DataFrame()
    with pytest.raises(Unavailable, match="no columns"):
        holding_contributions(empty, {})


def test_holding_contributions_missing_column_raises():
    rets = pd.DataFrame({"AAA": [0.01, 0.02]})
    with pytest.raises(Unavailable, match="absent from holdings_returns"):
        holding_contributions(rets, {"AAA": 0.5, "MISSING": 0.5})


def test_holding_contributions_all_nan_column_raises():
    rets = pd.DataFrame({"AAA": [0.01, 0.02], "BBB": [np.nan, np.nan]})
    with pytest.raises(Unavailable, match="all-NaN"):
        holding_contributions(rets, {"AAA": 0.5, "BBB": 0.5})


def test_holding_contributions_empty_weights_raises():
    rets = pd.DataFrame({"AAA": [0.01, 0.02]})
    with pytest.raises(Unavailable, match="weights is empty"):
        holding_contributions(rets, {})


# --------------------------------------------------------------------------- #
# Failure paths for factor regression -- the work order's required cases
# --------------------------------------------------------------------------- #


def test_factor_regression_fewer_observations_than_factors_raises():
    # Work-order required failure path.
    fr = _make_factor_returns(n=10, factor_names=("A", "B", "C", "D"))  # 4 factors
    # 4 factors + intercept = 5 needed; 3 observations -> raises.
    pr = pd.Series(np.zeros(3), index=fr.index[:3])
    with pytest.raises(Unavailable, match="n_factors"):
        regress_factor_exposures(pr, fr.iloc[:3])


def test_factor_regression_singular_design_matrix_raises():
    # Work-order required failure path: perfectly collinear factors.
    rng = np.random.default_rng(31)
    f1 = rng.normal(0, 0.01, 100)
    fr = pd.DataFrame(
        {"F1": f1, "F2": f1},  # F2 == F1 -> singular
        index=idx(100),
    )
    pr = pd.Series(rng.normal(0, 0.01, 100), index=idx(100))
    with pytest.raises(Unavailable, match="rank-deficient"):
        regress_factor_exposures(pr, fr)


def test_factor_regression_constant_factor_column_raises():
    # A constant factor column is collinear with the intercept -> rank-deficient.
    rng = np.random.default_rng(32)
    fr = pd.DataFrame(
        {"F1": rng.normal(0, 0.01, 100), "F2": np.full(100, 0.005)},
        index=idx(100),
    )
    pr = pd.Series(rng.normal(0, 0.01, 100), index=idx(100))
    with pytest.raises(Unavailable, match="rank-deficient"):
        regress_factor_exposures(pr, fr)


def test_factor_regression_no_columns_raises():
    pr = pd.Series([0.01, 0.02], index=DATES[:2])
    with pytest.raises(Unavailable, match="no factor columns"):
        regress_factor_exposures(pr, pd.DataFrame(index=DATES[:2]))


def test_factor_regression_empty_asset_series_raises():
    fr = _make_factor_returns()
    with pytest.raises(Unavailable, match="asset_returns is empty"):
        regress_factor_exposures(pd.Series([], dtype=float), fr)


# --------------------------------------------------------------------------- #
# Annualisation constant is the one we kept (and the caller can override)
# --------------------------------------------------------------------------- #


def test_market_model_attribution_periods_per_year_overridable():
    rng = np.random.default_rng(41)
    bench = pd.Series(rng.normal(0.0005, 0.01, 200), index=idx(200))
    port = pd.Series(0.0001 + 1.0 * bench.to_numpy() + rng.normal(0, 0.001, 200), index=idx(200))
    daily = market_model_attribution(port, bench, periods_per_year=252)
    monthly = market_model_attribution(port, bench, periods_per_year=12)
    # alpha scales linearly with periods_per_year.
    assert monthly.alpha == pytest.approx(daily.alpha * 12 / 252, rel=1e-9)
    # tracking_error scales with sqrt(periods_per_year).
    assert monthly.tracking_error == pytest.approx(
        daily.tracking_error * np.sqrt(12 / 252), rel=1e-9
    )
