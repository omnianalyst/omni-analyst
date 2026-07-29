"""Performance attribution and factor models -- the real (non-simulated) path.

v1 split this across three sources in ``../software/backend/`` (read-only):

- ``app/services/quant/factor_models.py``: factor exposures via OLS regression
  and the multi-factor attribution. The factor-return LOADER in v1
  (``FactorModel.load_factor_returns``) silently fell through to a correlated
  random-number generator when ``ENABLE_SIMULATED_DATA`` was set, then
  regressed the asset against the very returns it had just invented. v2 has no
  simulation switch and never will. This module consumes factor returns the
  caller supplies and never generates them.
- ``app/services/analytics/portfolio_metrics.py``: per-period Sharpe / Sortino /
  tracking error / information ratio / holding-level contribution. The v1
  implementations were tangled with SQLAlchemy sessions, used a fabricated
  ``risk_free=2.0`` default, and substituted 0.0 wherever a statistic was
  undefined. Only the pure arithmetic is ported and the fabricated defaults
  are gone.
- ``app/api/v1/endpoints/quant_analytics.py:951`` -- read for the intended
  output shape only. Its single-factor market-model handler is reproduced
  here in pure form.

Contract this module defends (see AGENTS.md -- the invariants):

1. Attribution is additive. The factor contributions plus the specific return
   equal the total portfolio return to floating tolerance. This falls out of
   OLS with an intercept (residuals sum to zero by construction); v1's
   ``avg_exposure * sum(factor)`` heuristic in ``factor_models.py`` did NOT
   sum exactly and is not reproduced. ``Attribution.check_additivity`` is
   called inside ``attribute_returns`` so a future edit that breaks the
   invariant fails loudly.
2. Undefined statistics raise ``Unavailable``, never return a fabricated zero.
   Sharpe of a constant series, information ratio of a portfolio identical to
   its benchmark, regression with a singular design matrix, attribution with
   no factor/asset date overlap -- each is genuinely undefined. A caller that
   gets ``Unavailable`` knows the gap is real. (``backtest.sharpe_ratio``
   returns NaN for its own aggregative reasons; that contract is unchanged
   here -- different consumer, different rule.)
3. No defaults that hide missing data. No zero risk-free rate, no benchmark of
   zeros, no fabricated factor returns. The caller asserts what they have.
4. The annualisation factor (``TRADING_DAYS_PER_YEAR``) is the only constant
   v1 took for granted that is kept; daily-equity is the only frequency it is
   meaningful for, and the caller can override it per call. Annualisation is
   a scalar multiply and so preserves additivity -- an annualized attribution
   also sums to the annualized total.

This module is pure and stateless. It does not register with the capability
registry and does not query coverage; bring it the inputs and it returns the
decomposition.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from omni.ingest.protocol import Unavailable

# v1 assumed 252 trading days for daily equity. The work order permits keeping
# annualisation factors that v1 treated as a genuine constant; this is the only
# one. It is a per-call default, not a global -- the caller overrides it for
# non-daily data.
TRADING_DAYS_PER_YEAR = 252

# Tolerance for "is this degenerate?" checks, applied uniformly to the sample
# standard deviation (ddof=1) of a series. Floating-point summation can leave a
# mathematically-zero std at ~1e-18; real daily-equity values sit orders of
# magnitude above this floor. Using a single tolerance on a single quantity
# (std) -- instead of one tolerance on sum-of-squared-deviations and another on
# std -- keeps degeneracy detection consistent across every function in the
# module. Two functions disagreeing on what counts as constant is exactly the
# v1 defect this rebuild exists to remove.
_ZERO_STD_ATOL = 1e-12

# Above this condition number the factor design matrix is treated as
# ill-conditioned (near-collinear factors). ``matrix_rank`` only fires on exact
# collinearity; a cond check catches factors that agree to ~1e-10, where lstsq
# returns huge cancelling coefficients that pass the additivity identity while
# being individually meaningless. 1e10 sits between well-conditioned random
# designs (~1e2) and the near-collinear case (~1e10+); ~10 digits of precision
# lost is the standard "do not trust the result" line.
_MAX_CONDITION_NUMBER = 1e10


# --------------------------------------------------------------------------- #
# OLS factor regression
# --------------------------------------------------------------------------- #


@dataclass
class FactorRegression:
    factor_names: list[str]
    betas: np.ndarray  # shape (k,)
    intercept: float  # per-period alpha
    r_squared: float
    residuals: np.ndarray  # shape (n,)
    n_observations: int

    def to_dict(self) -> dict:
        return {
            "factor_names": list(self.factor_names),
            "betas": {n: float(b) for n, b in zip(self.factor_names, self.betas)},
            "intercept": float(self.intercept),
            "r_squared": float(self.r_squared),
            "n_observations": int(self.n_observations),
        }


def regress_factor_exposures(
    asset_returns: pd.Series,
    factor_returns: pd.DataFrame,
    *,
    min_observations: int | None = None,
) -> FactorRegression:
    """OLS time-series regression of asset returns on factor returns.

    Reproduces the real-path arithmetic of v1's
    ``MultiFactorRiskModel.compute_factor_exposures``. v1 used ``statsmodels``
    and silently skipped assets with fewer than 60 observations; plain OLS is
    what numpy is for (PORTING.md forbids swapping in scipy/statsmodels when
    numpy already does it), and silent skipping is a fabrication-by-omission
    this module does not perform.

    Raises ``Unavailable`` when:
    - factor_returns has no factor columns, or asset_returns is empty
    - the time indices do not overlap (no aligned observations)
    - aligned observation count < (n_factors + 1) -- regression underdetermined
    - the design matrix [1, F] is rank-deficient (perfect collinearity or a
      constant factor)
    - ``min_observations`` (if supplied) is not met
    """
    if factor_returns.shape[1] == 0:
        raise Unavailable("factor_returns has no factor columns")
    if asset_returns.shape[0] == 0:
        raise Unavailable("asset_returns is empty")

    common = asset_returns.index.intersection(factor_returns.index)
    if len(common) == 0:
        raise Unavailable("no overlapping dates between asset_returns and factor_returns")

    y_full = asset_returns.loc[common].to_numpy(dtype=float)
    F_full = factor_returns.loc[common].to_numpy(dtype=float)
    # Drop rows with NaN in either side; an NaN is missing data, not a zero.
    ok = ~(np.isnan(y_full) | np.isnan(F_full).any(axis=1))
    y = y_full[ok]
    F = F_full[ok]
    n, k = F.shape

    needed = k + 1  # factors + intercept
    if n < needed:
        raise Unavailable(
            f"factor regression requires at least n_factors+1={needed} aligned "
            f"observations, got {n}"
        )
    if min_observations is not None and n < min_observations:
        raise Unavailable(
            f"only {n} aligned observations, caller required min_observations={min_observations}"
        )

    design = np.column_stack([np.ones(n), F])
    rank = int(np.linalg.matrix_rank(design))
    if rank < needed:
        raise Unavailable(
            f"factor design matrix is rank-deficient (rank {rank} < {needed}); "
            "factors are perfectly collinear or constant"
        )
    if np.linalg.cond(design) > _MAX_CONDITION_NUMBER:
        raise Unavailable(
            "factor design matrix is ill-conditioned (near-collinear factors); "
            "attribution would be numerically unstable"
        )

    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    intercept = float(coef[0])
    betas = coef[1:]
    predicted = design @ coef
    residuals = y - predicted
    ss_res = float(np.sum(residuals**2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    # Zero-variance asset series: R-squared is 0/0 and undefined. The check is
    # on std (ddof=1), not on ss_tot == 0: a constant series built from a value
    # not exactly representable in binary64 (e.g. 0.005) leaves ss_tot at
    # ~1e-34 after summation, so an exact comparison misses it and the function
    # returns a fabricated negative R-squared from dividing two noise quantities.
    if np.isclose(np.sqrt(ss_tot / (n - 1)), 0.0, atol=_ZERO_STD_ATOL):
        raise Unavailable(
            "asset_returns has zero variance over the window; R-squared is undefined"
        )
    r_squared = 1.0 - ss_res / ss_tot

    return FactorRegression(
        factor_names=list(factor_returns.columns),
        betas=betas,
        intercept=intercept,
        r_squared=r_squared,
        residuals=residuals,
        n_observations=n,
    )


# --------------------------------------------------------------------------- #
# Multi-factor attribution (additive)
# --------------------------------------------------------------------------- #


@dataclass
class Attribution:
    total_return: float
    factor_contributions: dict[str, float]
    specific_return: float
    factor_exposures: dict[str, float]
    n_observations: int

    def to_dict(self) -> dict:
        return {
            "total_return": self.total_return,
            "factor_contributions": dict(self.factor_contributions),
            "specific_return": self.specific_return,
            "factor_exposures": dict(self.factor_exposures),
            "n_observations": int(self.n_observations),
        }

    def check_additivity(self, atol: float = 1e-9) -> None:
        total = self.specific_return + sum(self.factor_contributions.values())
        if not np.isclose(total, self.total_return, atol=atol):
            raise AssertionError(
                f"attribution not additive: factors + specific = {total}, "
                f"total_return = {self.total_return}"
            )


def attribute_returns(
    portfolio_returns: pd.Series,
    factor_returns: pd.DataFrame,
    *,
    min_observations: int | None = None,
) -> Attribution:
    """Decompose portfolio returns into factor contributions + specific.

    The decomposition is regression-based: regress portfolio returns on the
    factor returns with an intercept, then

        contribution[factor_i] = beta_i * sum(factor_i)
        specific_return        = n * intercept
                               == sum(portfolio_returns) - sum(contributions)

    ``total_return`` is the sum of portfolio returns over the dates where
    ``portfolio_returns`` and ``factor_returns`` overlap (the alignment used
    internally for the regression). Factors are required for attribution, so
    portfolio-only dates -- those absent from ``factor_returns`` -- cannot be
    decomposed and are excluded from both the regression and the total. When
    the two indices are coextensive this equals ``sum(portfolio_returns)``;
    when they partially overlap it does not, and the discrepancy can flip the
    sign versus the full-series sum. Callers comparing ``total_return`` to a
    precomputed full-series sum must align first.

    Because OLS with an intercept yields zero-mean residuals, the contributions
    plus the specific return equal ``total_return`` to floating
    tolerance. This is the additive property the work order requires; v1's
    mean-exposure heuristic (``avg_exposure * sum(factor)``) did not satisfy it
    and is not reproduced. ``check_additivity`` is asserted before returning.

    Raises ``Unavailable`` on the same conditions as ``regress_factor_exposures``.
    """
    reg = regress_factor_exposures(
        portfolio_returns, factor_returns, min_observations=min_observations
    )

    common = portfolio_returns.index.intersection(factor_returns.index)
    F_full = factor_returns.loc[common].to_numpy(dtype=float)
    y_full = portfolio_returns.loc[common].to_numpy(dtype=float)
    ok = ~(np.isnan(y_full) | np.isnan(F_full).any(axis=1))
    F = F_full[ok]
    y = y_full[ok]

    factor_sums = F.sum(axis=0)
    contributions = {
        name: float(beta * s) for name, beta, s in zip(reg.factor_names, reg.betas, factor_sums)
    }
    specific = reg.n_observations * reg.intercept
    total = float(y.sum())

    attr = Attribution(
        total_return=total,
        factor_contributions=contributions,
        specific_return=specific,
        factor_exposures={n_: float(b) for n_, b in zip(reg.factor_names, reg.betas)},
        n_observations=reg.n_observations,
    )
    attr.check_additivity()
    return attr


# --------------------------------------------------------------------------- #
# Single-factor (market-model) attribution
# --------------------------------------------------------------------------- #


@dataclass
class MarketModelAttribution:
    alpha: float  # annualized intercept
    beta: float
    r_squared: float
    total_return: float  # annualized
    factor_return: float  # = beta * benchmark annualized total
    specific_return: float  # = alpha (annualized); residuals mean-zero
    tracking_error: float  # annualized std of residuals
    information_ratio: float
    n_observations: int

    def to_dict(self) -> dict:
        return {
            "alpha": self.alpha,
            "beta": self.beta,
            "r_squared": self.r_squared,
            "total_return": self.total_return,
            "factor_return": self.factor_return,
            "specific_return": self.specific_return,
            "tracking_error": self.tracking_error,
            "information_ratio": self.information_ratio,
            "n_observations": int(self.n_observations),
        }


def market_model_attribution(
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> MarketModelAttribution:
    """Single-factor (market-model) performance attribution.

    Mirrors the output shape of v1's ``GET /attribution/{portfolio_id}``
    handler (``quant_analytics.py:951``) with fabricated defaults removed:

    - The handler did ``np.nan_to_num(rets)`` on price-derived returns,
      silently converting missing prices into zero returns. This function does
      not; NaNs cause the date to be dropped from the alignment, and an empty
      alignment raises.
    - The handler returned ``information_ratio = 0`` when tracking error was
      negligible. A zero tracking error means the IR is mathematically
      undefined; this function raises ``Unavailable`` instead, so a portfolio
      identical to its benchmark surfaces as the degenerate case it is.

    Raises ``Unavailable`` when:
    - portfolio or benchmark series is empty
    - the time indices do not overlap (misaligned dates)
    - fewer than 2 aligned observations (variance undefined)
    - portfolio OR benchmark variance is zero (regression undefined)
    - tracking error is zero (information ratio undefined; this is the
      portfolio-identical-to-benchmark case)
    """
    if portfolio_returns.shape[0] == 0:
        raise Unavailable("portfolio_returns is empty")
    if benchmark_returns.shape[0] == 0:
        raise Unavailable("benchmark_returns is empty")

    common = portfolio_returns.index.intersection(benchmark_returns.index)
    if len(common) == 0:
        raise Unavailable("no overlapping dates between portfolio_returns and benchmark_returns")

    p_full = portfolio_returns.loc[common].to_numpy(dtype=float)
    b_full = benchmark_returns.loc[common].to_numpy(dtype=float)
    ok = ~(np.isnan(p_full) | np.isnan(b_full))
    p = p_full[ok]
    b = b_full[ok]
    n = p.shape[0]
    if n < 2:
        raise Unavailable(f"need at least 2 overlapping observations to compute variance, got {n}")

    b_var = float(np.sum((b - b.mean()) ** 2))
    if np.isclose(np.sqrt(b_var / (n - 1)), 0.0, atol=_ZERO_STD_ATOL):
        raise Unavailable(
            "benchmark_returns has zero variance over the window; "
            "single-factor regression is undefined"
        )
    p_var = float(np.sum((p - p.mean()) ** 2))
    if np.isclose(np.sqrt(p_var / (n - 1)), 0.0, atol=_ZERO_STD_ATOL):
        raise Unavailable(
            "portfolio_returns has zero variance over the window; "
            "single-factor regression is undefined"
        )

    # Single-factor OLS via least squares on [1, b] (matches v1 endpoint's
    # np.polyfit deg-1 in exact arithmetic; we use lstsq to also detect the
    # degenerate cases above before the call).
    design = np.column_stack([np.ones(n), b])
    coef, *_ = np.linalg.lstsq(design, p, rcond=None)
    alpha_daily = float(coef[0])
    beta = float(coef[1])
    residuals = p - (alpha_daily + beta * b)
    ss_res = float(np.sum(residuals**2))
    r_squared = 1.0 - ss_res / p_var

    # Annualization (constant factor -- see module docstring).
    alpha_annual = alpha_daily * periods_per_year
    bench_total_annual = float(b.mean()) * periods_per_year
    port_total_annual = float(p.mean()) * periods_per_year
    factor_annual = beta * bench_total_annual
    # Residuals are mean-zero by OLS construction (intercept in the design), so
    # the annualized specific return equals the annualized alpha exactly. This
    # is what makes the decomposition additive.
    specific_annual = alpha_annual

    # Tracking error: sample std (ddof=1) of residuals, annualized. v1's
    # endpoint used np.std (ddof=0); there is no test coverage on that path
    # and ddof=1 is the standard finance convention, so this function deviates
    # from v1 here -- see report.
    resid_std = float(residuals.std(ddof=1))
    if np.isclose(resid_std, 0.0, atol=_ZERO_STD_ATOL):
        raise Unavailable(
            "tracking error is zero (portfolio is a linear function of the "
            "benchmark over the window); information ratio is undefined"
        )
    tracking_error = resid_std * np.sqrt(periods_per_year)
    information_ratio = alpha_annual / tracking_error

    return MarketModelAttribution(
        alpha=alpha_annual,
        beta=beta,
        r_squared=float(r_squared),
        total_return=port_total_annual,
        factor_return=factor_annual,
        specific_return=specific_annual,
        tracking_error=tracking_error,
        information_ratio=information_ratio,
        n_observations=n,
    )


# --------------------------------------------------------------------------- #
# Standalone risk/performance metrics
# --------------------------------------------------------------------------- #


def annualized_sharpe(
    returns: pd.Series | Sequence[float],
    *,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Annualized Sharpe ratio of an excess-return series.

    The caller supplies EXCESS returns (asset minus risk-free); v1's
    ``_calculate_sharpe`` defaulted ``risk_free=2.0`` which is a fabrication.
    There is no risk-free default here.

    Raises ``Unavailable`` when:
    - fewer than 2 finite observations (variance undefined)
    - variance is zero (constant-return series; Sharpe undefined)
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        raise Unavailable(f"sharpe requires >=2 observations, got {r.size}")
    sd = float(r.std(ddof=1))
    if np.isclose(sd, 0.0, atol=_ZERO_STD_ATOL):
        raise Unavailable("sharpe ratio is undefined: return series has zero variance")
    return float(r.mean() / sd * np.sqrt(periods_per_year))


def annualized_sortino(
    returns: pd.Series | Sequence[float],
    *,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Annualized Sortino ratio (downside-deviation denominator, target = 0).

    Raises ``Unavailable`` when:
    - fewer than 2 finite observations
    - no observations fall below target (downside deviation is zero; Sortino
      undefined). This is the upside-case: a series with no losses has an
      infinite Sortino, which we surface as undefined rather than +inf.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        raise Unavailable(f"sortino requires >=2 observations, got {r.size}")
    downside = np.minimum(r, 0.0)
    downside_dev = float(np.sqrt(np.mean(downside**2)))
    if np.isclose(downside_dev, 0.0, atol=_ZERO_STD_ATOL):
        raise Unavailable(
            "sortino ratio is undefined: no observations below target (downside deviation is zero)"
        )
    return float(r.mean() / downside_dev * np.sqrt(periods_per_year))


def active_returns(
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
) -> pd.Series:
    """Date-aligned ``portfolio - benchmark`` returns.

    Raises ``Unavailable`` when:
    - either series is empty
    - the time indices do not overlap (misaligned dates)
    - fewer than 2 aligned observations
    """
    if portfolio_returns.shape[0] == 0:
        raise Unavailable("portfolio_returns is empty")
    if benchmark_returns.shape[0] == 0:
        raise Unavailable("benchmark_returns is empty")
    common = portfolio_returns.index.intersection(benchmark_returns.index)
    if len(common) == 0:
        raise Unavailable("no overlapping dates between portfolio_returns and benchmark_returns")
    p = portfolio_returns.loc[common]
    b = benchmark_returns.loc[common]
    active = (p - b).dropna()
    if active.shape[0] < 2:
        raise Unavailable(
            f"need at least 2 overlapping observations for variance, got {active.shape[0]}"
        )
    return active


def annualized_tracking_error(
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Annualized tracking error: sample std of active returns.

    Raises ``Unavailable`` on the same conditions as ``active_returns``, and
    when active returns have zero variance (portfolio tracks benchmark
    exactly -- TE undefined).
    """
    active = active_returns(portfolio_returns, benchmark_returns)
    sd = float(active.to_numpy(dtype=float).std(ddof=1))
    if np.isclose(sd, 0.0, atol=_ZERO_STD_ATOL):
        raise Unavailable(
            "tracking error is undefined: active returns have zero variance "
            "(portfolio tracks benchmark exactly)"
        )
    return float(sd * np.sqrt(periods_per_year))


def annualized_information_ratio(
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Annualized information ratio: mean(active) / std(active) * sqrt(periods).

    Note: a mean-active-return of exactly zero with nonzero variance is a
    legitimate IR of 0.0 and is returned. The UNDEFINED case is std == 0
    (portfolio identical to benchmark), which raises.

    Raises ``Unavailable`` on the same conditions as ``active_returns`` and
    when active-return variance is zero.
    """
    active = active_returns(portfolio_returns, benchmark_returns)
    a = active.to_numpy(dtype=float)
    sd = float(a.std(ddof=1))
    if np.isclose(sd, 0.0, atol=_ZERO_STD_ATOL):
        raise Unavailable(
            "information ratio is undefined: tracking error is zero (portfolio == benchmark)"
        )
    return float(a.mean() / sd * np.sqrt(periods_per_year))


# --------------------------------------------------------------------------- #
# Contribution by holding
# --------------------------------------------------------------------------- #


def holding_contributions(
    holdings_returns: pd.DataFrame,
    weights: Mapping[str, float] | pd.Series,
) -> dict[str, float]:
    """Per-holding contribution to total portfolio return.

    For a static-weight portfolio,

        contribution_i = weight_i * sum(returns_i over the period)
        sum(contributions) == sum_t(sum_i w_i * r_i,t) == total portfolio return

    The equality holds to floating tolerance -- this is the additive property
    the work order requires, at the holding level.

    Raises ``Unavailable`` when:
    - ``holdings_returns`` has no columns (empty holdings list)
    - a weighted holding has no matching column in ``holdings_returns``
    - a weighted holding's return column is all-NaN (cannot contribute)
    """
    if holdings_returns.shape[1] == 0:
        raise Unavailable("holdings_returns has no columns (empty holdings list)")
    weights_series = pd.Series(weights, dtype=float)
    if weights_series.shape[0] == 0:
        raise Unavailable("weights is empty (no holdings requested)")
    missing = [h for h in weights_series.index if h not in holdings_returns.columns]
    if missing:
        raise Unavailable(f"weights reference holdings absent from holdings_returns: {missing}")
    # Only the holdings named in `weights` contribute; other columns ignored.
    sub = holdings_returns[list(weights_series.index)]
    holding_sums = sub.sum(axis=0, skipna=False)
    if holding_sums.isna().any():
        bad = holding_sums[holding_sums.isna()].index.tolist()
        raise Unavailable(f"holdings with all-NaN returns cannot contribute: {bad}")
    return {h: float(weights_series[h] * holding_sums[h]) for h in weights_series.index}
