"""Portfolio construction: optimisation, factor risk model, position sizing.

Ported from three v1 service modules under
`app/services/portfolio_construction/`:

- `optimizer.py` -- HRP (Hierarchical Risk Parity), equal-risk-contribution
  risk parity, long-only minimum variance, and risk-contribution attribution.
- `risk_model.py` -- the multi-factor risk model (Barra/Axioma-style): per-asset
  time-series regression of returns on factors, implied asset covariance
  ``B Sigma_f B' + diag(specific)``, and the factor-vs-specific variance split.
- `sizing.py` -- EWMA volatility, ATR, vol-target / inverse-vol weighting,
  fractional Kelly, meta-label sizing, and the active drawdown breaker.

The thin DB-backed wrapper `app/services/portfolio/optimization_service.py` was
read for call shape and dropped: it held an SQLAlchemy session and dispatched to
scipy SLSQP for max-Sharpe / mean-variance / the efficient frontier. PORTING.md
says framework sessions do not carry, so that wrapper (and the SLSQP objectives
living only inside it) is not ported. Note: the work order's "Why" describes
``optimizer.py`` as holding "mean-variance, max-Sharpe, the efficient frontier"
-- it does not. Those live in the dropped service wrapper. ``optimizer.py``
holds HRP / risk parity / min variance, which is what is ported below.

CE1 later revisited that wrapper: its SQLAlchemy session lives only in the
returns fetch (``_fetch_returns_matrix``), not in the optimisation math. The
SLSQP kernel itself is pure numpy/scipy, so ``max_sharpe_weights`` and
``efficient_frontier`` are lifted here with the caller supplying the returns
matrix. ``value_at_risk`` / ``expected_shortfall`` are lifted from the inline
risk-metrics handler; the rest of that handler (Sharpe/Sortino/max-drawdown)
was already covered by ``attribution.py`` and ``fundamentals.py``.


Where v1 substitutes a default on missing input -- a zero EWMA volatility on
too few observations, a zero ATR, a silent equal-weight fallback for an
un-invertible covariance, a fabricated unit correlation for a zero-variance
asset -- this module raises ``Unavailable`` instead. A capability that returns a
plausible number on no data is how hallucinated coverage enters the store.

Drawdown is a required argument, not a hardcoded 0.0. v1's
``decision_pipeline`` passed ``portfolio_drawdown_pct = 0.0`` which disabled the
drawdown circuit breaker in ``risk_management``; that caller bug is not
reproduced here. ``drawdown_breaker`` takes the equity curve positionally with
no default -- if the caller does not have a curve, it does not get a size.

Everything is pure numpy/pandas/scipy (scipy only for HRP's hierarchical
linkage, exactly as v1 used it). No IO, no DB session, no claim access. All
entry points are sync leaf math.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import linkage
from scipy.optimize import minimize
from scipy.spatial.distance import squareform

from omni.ingest.protocol import Unavailable

TRADING_DAYS = 252


# --------------------------------------------------------------------------- #
# Covariance validation (shared by the three optimisers)
# --------------------------------------------------------------------------- #
def _check_cov(C: np.ndarray, *, require_pd: bool = False) -> None:
    """Reject a covariance matrix this module will not honestly solve on.

    ``require_pd`` is set by the minimum-variance optimiser, whose analytic
    solution inverts the covariance. A singular / non-PD matrix has no unique
    min-variance answer; v1 papered over it with ``pinv`` plus a silent
    equal-weight fallback, and that fallback is the default-substitution this
    repo removes. HRP and risk parity keep working on the ill-conditioned-but-
    valid matrices their v1 tests cover (HRP avoids inversion by design), so
    they do not set ``require_pd``.
    """
    if not np.all(np.isfinite(C)):
        raise Unavailable("covariance contains non-finite values")
    if np.any(np.diag(C) <= 0):
        raise Unavailable("covariance has a non-positive diagonal entry (degenerate asset)")
    if require_pd:
        sym = 0.5 * (C + C.T)
        eig = np.linalg.eigvalsh(sym)
        emax = eig[-1]
        if eig[0] <= 1e-12 * max(emax, 1e-300):
            raise Unavailable(
                "covariance is singular / not positive-definite; "
                "no unique minimum-variance solution"
            )


# =========================================================================== #
# Optimiser (ported from optimizer.py)
# =========================================================================== #
def _as_cov_df(cov) -> pd.DataFrame:
    if isinstance(cov, pd.DataFrame):
        return cov
    cov = np.asarray(cov, dtype=float)
    idx = [f"A{i}" for i in range(cov.shape[0])]
    return pd.DataFrame(cov, index=idx, columns=idx)


def _cov_to_corr(cov: np.ndarray) -> np.ndarray:
    d = np.sqrt(np.diag(cov))
    d_safe = np.where(d > 0, d, 1.0)
    corr = cov / np.outer(d_safe, d_safe)
    corr = np.clip(corr, -1.0, 1.0)
    np.fill_diagonal(corr, 1.0)
    return corr


def _apply_cap(weights: np.ndarray, cap: float, n_iter: int = 1000) -> np.ndarray:
    """Cap each weight at ``cap`` and redistribute excess to uncapped names.

    Long-only, sum-to-1 preserved. If ``cap * n < 1`` the cap is infeasible and
    we fall back to equal weight (the tightest feasible allocation).
    """
    n = weights.size
    if cap is None or cap >= 1.0:
        return weights
    if cap * n < 1.0 - 1e-12:
        return np.full(n, 1.0 / n)
    w = weights.astype(float).copy()
    for _ in range(n_iter):
        over = w > cap + 1e-15
        if not over.any():
            break
        excess = float(np.sum(w[over] - cap))
        w[over] = cap
        under = ~over
        room = w[under]
        room_sum = float(room.sum())
        if room_sum <= 0:
            w[under] = excess / max(under.sum(), 1)
        else:
            w[under] = room + excess * (room / room_sum)
    w = np.clip(w, 0.0, cap)
    s = w.sum()
    if s > 0:
        w = w / s
    return w


def _apply_turnover_penalty(
    target: np.ndarray,
    prior: np.ndarray,
    penalty: float,
    cap: float | None,
) -> np.ndarray:
    """Shrink the target toward the prior to reduce turnover (cost-aware).

    ``penalty`` in [0, 1] is the fraction of the move that is NOT taken: 0 means
    trade fully to target, 1 means stay at prior. Closed-form optimum of a
    quadratic trade-off mapped to a convex blend, then re-normalised / re-capped
    to keep the long-only sum-to-1 (and cap) constraints.
    """
    penalty = float(np.clip(penalty, 0.0, 1.0))
    w = (1.0 - penalty) * target + penalty * prior
    w = np.clip(w, 0.0, None)
    s = w.sum()
    if s > 0:
        w = w / s
    if cap is not None:
        w = _apply_cap(w, cap)
    return w


# --------------------------------------------------------------------------- #
# HRP (Appendix A.13)
# --------------------------------------------------------------------------- #
def _quasi_diagonal_order(link: np.ndarray, n: int) -> list:
    """Recover the leaf order from a scipy linkage matrix (cluster ordering)."""
    link = link.astype(int)
    sort_ix = [link[-1, 0], link[-1, 1]]
    while max(sort_ix) >= n:
        new = []
        for i in sort_ix:
            if i < n:
                new.append(i)
            else:
                row = link[i - n]
                new.append(row[0])
                new.append(row[1])
        sort_ix = new
    return sort_ix


def _ivp(cov: np.ndarray) -> np.ndarray:
    """Inverse-variance portfolio weights for a covariance block."""
    ivp = 1.0 / np.diag(cov)
    ivp = ivp / ivp.sum()
    return ivp


def _cluster_var(cov: np.ndarray, idx: Sequence[int]) -> float:
    """Variance of an inverse-variance-weighted sub-cluster."""
    sub = cov[np.ix_(idx, idx)]
    w = _ivp(sub)
    return float(w @ sub @ w)


def _recursive_bisection(cov: np.ndarray, order: list) -> np.ndarray:
    """Allocate by recursively splitting the quasi-diagonalised order."""
    n = cov.shape[0]
    w = np.ones(n)
    clusters = [list(order)]
    while clusters:
        new_clusters = []
        for cl in clusters:
            if len(cl) <= 1:
                continue
            half = len(cl) // 2
            left = cl[:half]
            right = cl[half:]
            var_left = _cluster_var(cov, left)
            var_right = _cluster_var(cov, right)
            alpha = 1.0 - var_left / (var_left + var_right)
            for i in left:
                w[i] *= alpha
            for i in right:
                w[i] *= 1.0 - alpha
            new_clusters.append(left)
            new_clusters.append(right)
        clusters = new_clusters
    return w


def hrp_weights(
    cov,
    *,
    cap: float | None = None,
    prior_weights=None,
    turnover_penalty: float = 0.0,
) -> pd.Series:
    """Hierarchical Risk Parity weights (the default optimiser)."""
    cov_df = _as_cov_df(cov)
    assets = list(cov_df.index)
    C = cov_df.to_numpy(dtype=float)
    n = C.shape[0]
    if n == 0:
        raise Unavailable("empty universe: covariance has 0 assets")
    _check_cov(C)
    if n == 1:
        return pd.Series([1.0], index=assets)

    corr = _cov_to_corr(C)
    dist = np.sqrt(np.clip(0.5 * (1.0 - corr), 0.0, None))
    np.fill_diagonal(dist, 0.0)
    condensed = squareform(dist, checks=False)
    link = linkage(condensed, method="single")
    order = _quasi_diagonal_order(link, n)
    w = _recursive_bisection(C, order)
    w = w / w.sum()

    return _finalize(w, assets, cap, prior_weights, turnover_penalty)


# --------------------------------------------------------------------------- #
# Risk parity (equal risk contribution)
# --------------------------------------------------------------------------- #
def risk_parity_weights(
    cov,
    *,
    cap: float | None = None,
    prior_weights=None,
    turnover_penalty: float = 0.0,
    max_iter: int = 10000,
    tol: float = 1e-12,
) -> pd.Series:
    """Equal-risk-contribution weights via the standard fixed-point iteration.

    Each asset's risk contribution RC_i = w_i (Sigma w)_i is driven toward equal
    across assets. Long-only; the cyclical multiplicative update converges for
    PSD covariance.
    """
    cov_df = _as_cov_df(cov)
    assets = list(cov_df.index)
    C = cov_df.to_numpy(dtype=float)
    n = C.shape[0]
    if n == 0:
        raise Unavailable("empty universe: covariance has 0 assets")
    _check_cov(C)
    if n == 1:
        return pd.Series([1.0], index=assets)

    target = 1.0 / n
    w = 1.0 / np.sqrt(np.diag(C))
    w = w / w.sum()

    for _ in range(max_iter):
        sigma_w = C @ w
        port_var = float(w @ sigma_w)
        rc = w * sigma_w / port_var
        w = w * (target / np.maximum(rc, 1e-300)) ** 0.5
        w = np.clip(w, 0.0, None)
        w = w / w.sum()
        if np.max(np.abs(rc - target)) < tol:
            break

    return _finalize(w, assets, cap, prior_weights, turnover_penalty)


# --------------------------------------------------------------------------- #
# Minimum variance
# --------------------------------------------------------------------------- #
def _project_simplex(v: np.ndarray) -> np.ndarray:
    """Euclidean projection onto {w >= 0, sum w = 1} (Duchi et al.)."""
    n = v.size
    u = np.sort(v)[::-1]
    cssv = np.cumsum(u) - 1.0
    ind = np.arange(1, n + 1)
    cond = u - cssv / ind > 0
    if not cond.any():
        return np.full(n, 1.0 / n)
    rho = ind[cond][-1]
    theta = cssv[cond][-1] / rho
    w = np.maximum(v - theta, 0.0)
    s = w.sum()
    return w / s if s > 0 else np.full(n, 1.0 / n)


def min_variance_weights(
    cov,
    *,
    cap: float | None = None,
    prior_weights=None,
    turnover_penalty: float = 0.0,
    max_iter: int = 5000,
    tol: float = 1e-12,
) -> pd.Series:
    """Long-only minimum-variance weights.

    Uses the analytic global-minimum-variance solution when it is already
    non-negative; otherwise projects to the long-only simplex via a
    multiplicative-weights / active-set style iteration that keeps weights >= 0
    and summing to 1 while reducing variance.

    A singular covariance has no unique minimum-variance solution; v1 substituted
    equal weight via a ``pinv`` + except-fallback. That is gone: a singular /
    non-PD covariance raises ``Unavailable`` at the gate below.
    """
    cov_df = _as_cov_df(cov)
    assets = list(cov_df.index)
    C = cov_df.to_numpy(dtype=float)
    n = C.shape[0]
    if n == 0:
        raise Unavailable("empty universe: covariance has 0 assets")
    _check_cov(C, require_pd=True)
    if n == 1:
        return pd.Series([1.0], index=assets)

    ones = np.ones(n)
    try:
        inv = np.linalg.pinv(C)
        w_analytic = inv @ ones
        denom = float(ones @ w_analytic)
        if denom != 0:
            w_analytic = w_analytic / denom
        else:
            w_analytic = ones / n
    except np.linalg.LinAlgError:
        w_analytic = ones / n

    if np.all(w_analytic >= -1e-12):
        w = np.clip(w_analytic, 0.0, None)
        w = w / w.sum()
    else:
        w = ones / n
        lr = 1.0 / (np.linalg.norm(C, 2) + 1e-12)
        prev_var = float(w @ C @ w)
        for _ in range(max_iter):
            grad = 2.0 * (C @ w)
            w = w - lr * grad
            w = _project_simplex(w)
            cur_var = float(w @ C @ w)
            if abs(prev_var - cur_var) < tol:
                break
            prev_var = cur_var

    return _finalize(w, assets, cap, prior_weights, turnover_penalty)


# --------------------------------------------------------------------------- #
# Dispatch + finalise
# --------------------------------------------------------------------------- #
def _align_prior(prior_weights, assets: list) -> np.ndarray:
    if isinstance(prior_weights, pd.Series):
        return prior_weights.reindex(assets).fillna(0.0).to_numpy(dtype=float)
    p = np.asarray(prior_weights, dtype=float)
    if p.shape[0] != len(assets):
        raise ValueError("prior_weights length mismatch")
    return p


def _finalize(
    w: np.ndarray,
    assets: list,
    cap: float | None,
    prior_weights,
    turnover_penalty: float,
) -> pd.Series:
    if cap is not None:
        w = _apply_cap(w, cap)
    if prior_weights is not None and turnover_penalty > 0.0:
        prior = _align_prior(prior_weights, assets)
        w = _apply_turnover_penalty(w, prior, turnover_penalty, cap)
    return pd.Series(w, index=assets)


def optimize_weights(cov, method: str = "hrp", **kwargs) -> pd.Series:
    """Dispatch to an optimiser. Default method is HRP (Appendix A.13)."""
    method = (method or "hrp").lower()
    if method == "hrp":
        return hrp_weights(cov, **kwargs)
    if method in ("risk_parity", "erc", "risk-parity"):
        return risk_parity_weights(cov, **kwargs)
    if method in ("min_variance", "min_var", "minvar"):
        return min_variance_weights(cov, **kwargs)
    raise ValueError(f"unknown method '{method}' (hrp, risk_parity, min_variance)")


def risk_contributions(cov, weights) -> pd.Series:
    """Normalised risk contributions RC_i = w_i (Sigma w)_i / (w' Sigma w)."""
    cov_df = _as_cov_df(cov)
    assets = list(cov_df.index)
    C = cov_df.to_numpy(dtype=float)
    if isinstance(weights, pd.Series):
        w = weights.reindex(assets).fillna(0.0).to_numpy(dtype=float)
    else:
        w = np.asarray(weights, dtype=float)
    sigma_w = C @ w
    pv = float(w @ sigma_w)
    rc = w * sigma_w / pv if pv > 0 else np.zeros_like(w)
    return pd.Series(rc, index=assets)


# =========================================================================== #
# Mean-variance optimisation (SLSQP)
# =========================================================================== #
# Ported from app/services/portfolio/optimization_service.py. H1 dropped that
# file citing its SQLAlchemy session; the session lives only in the returns
# fetch (``_fetch_returns_matrix``), not in the optimisation math. The SLSQP
# kernel is pure numpy/scipy and is lifted here; the caller supplies the returns
# matrix. No DB, no session, no fabricated risk-free rate.
def _as_returns_df(returns) -> pd.DataFrame:
    if isinstance(returns, pd.DataFrame):
        return returns
    arr = np.asarray(returns, dtype=float)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    cols = [f"A{i}" for i in range(arr.shape[1])]
    return pd.DataFrame(arr, columns=cols)


def _returns_moments(returns):
    """Per-period mean vector and covariance matrix from a returns panel.

    Rows containing any non-finite value are dropped (scatter-NaN tolerance,
    matching ``fit_factor_risk_model``). Raises ``Unavailable`` when there are
    fewer than two assets or fewer than two usable observations -- a covariance
    on one row is not a covariance.
    """
    df = _as_returns_df(returns)
    assets = list(df.columns)
    R = df.to_numpy(dtype=float)
    R = R[np.all(np.isfinite(R), axis=1)]
    n_assets = R.shape[1]
    if n_assets < 2:
        raise Unavailable("need >= 2 assets for mean-variance optimisation")
    if R.shape[0] < 2:
        raise Unavailable(
            f"need >= 2 aligned observations for a covariance, got {R.shape[0]}"
        )
    mean_ret = np.mean(R, axis=0)
    cov = np.atleast_2d(np.cov(R, rowvar=False))
    if cov.shape != (n_assets, n_assets):
        cov = cov.reshape(n_assets, n_assets)
    return mean_ret, cov, assets


def _portfolio_stats(w, mean_ret, cov, risk_free_rate, periods_per_year):
    ret = float(np.sum(mean_ret * w) * periods_per_year)
    vol = float(np.sqrt(w @ (cov * periods_per_year) @ w))
    sharpe = (ret - risk_free_rate) / vol if vol > 1e-12 else 0.0
    return ret, vol, sharpe


def max_sharpe_weights(
    returns,
    *,
    risk_free_rate: float = 0.0,
    min_weight: float = 0.0,
    max_weight: float = 1.0,
    periods_per_year: int = TRADING_DAYS,
) -> pd.Series:
    """Long-only maximum-Sharpe (tangency) weights via SLSQP.

    Maximises the annualised Sharpe ``(ret - rf) / vol`` subject to
    ``sum(w) == 1`` and ``min_weight <= w <= max_weight``. Ported from the
    ``max_sharpe`` branch of ``PortfolioOptimizer.optimize``. ``risk_free_rate``
    is an annualised decimal (0.05 = 5%); there is no fabricated default, 0.0
    means the caller is supplying excess returns.
    """
    mean_ret, cov, assets = _returns_moments(returns)
    n = len(assets)

    def neg_sharpe(w):
        return -_portfolio_stats(w, mean_ret, cov, risk_free_rate, periods_per_year)[2]

    bounds = tuple((min_weight, max_weight) for _ in range(n))
    constraints = ({"type": "eq", "fun": lambda w: np.sum(w) - 1.0},)
    x0 = np.full(n, 1.0 / n)
    result = minimize(
        neg_sharpe, x0, method="SLSQP", bounds=bounds, constraints=constraints,
        options={"maxiter": 500, "ftol": 1e-10},
    )
    w = np.clip(result.x, min_weight, max_weight)
    s = w.sum()
    w = w / s if s > 0 else np.full(n, 1.0 / n)
    return pd.Series(w, index=assets)


def efficient_frontier(
    returns,
    *,
    risk_free_rate: float = 0.0,
    min_weight: float = 0.0,
    max_weight: float = 1.0,
    n_points: int = 50,
    periods_per_year: int = TRADING_DAYS,
) -> pd.DataFrame:
    """Long-only efficient frontier via SLSQP (min-variance-at-target-return).

    For each target return on a linspace from the lowest to the highest
    single-asset annualised return, minimises portfolio variance subject to
    ``sum(w) == 1``, the box bounds and the return target. Ported from
    ``PortfolioOptimizer._efficient_frontier``; only points the optimiser
    converged on are returned (v1 behaviour). Columns match v1's point keys:
    ``return``, ``volatility``, ``sharpe_ratio``.
    """
    mean_ret, cov, _ = _returns_moments(returns)
    n = mean_ret.size

    bounds = tuple((min_weight, max_weight) for _ in range(n))
    eq_sum = {"type": "eq", "fun": lambda w: np.sum(w) - 1.0}
    min_ret = float(np.min(mean_ret)) * periods_per_year
    max_ret = float(np.max(mean_ret)) * periods_per_year
    targets = np.linspace(min_ret, max_ret, n_points)
    x0 = np.full(n, 1.0 / n)

    rows = []
    for target in targets:
        cons = (
            eq_sum,
            {"type": "eq", "fun": lambda w, t=target: np.sum(mean_ret * w) * periods_per_year - t},
        )

        def min_var(w):
            return float(np.sqrt(w @ (cov * periods_per_year) @ w))

        result = minimize(
            min_var, x0, method="SLSQP", bounds=bounds, constraints=cons,
            options={"maxiter": 200, "ftol": 1e-8},
        )
        if result.success:
            ret, vol, sharpe = _portfolio_stats(
                result.x, mean_ret, cov, risk_free_rate, periods_per_year
            )
            rows.append({"return": ret, "volatility": vol, "sharpe_ratio": sharpe})

    return pd.DataFrame(rows, columns=["return", "volatility", "sharpe_ratio"])


# --------------------------------------------------------------------------- #
# Historical risk metrics (VaR / expected shortfall)
# --------------------------------------------------------------------------- #
# Ported from the inline risk-metrics handler's historical VaR/ES kernel. v1
# scaled returns by 100 and reported dollar figures tied to a portfolio value;
# the scaling and dollar conversion are the caller's job here. Fewer than two
# finite observations raises ``Unavailable`` -- v1 returned 0.0, declaring "no
# risk". (Sharpe/Sortino/max-drawdown for the same endpoint are already covered
# by annualized_sharpe/annualized_sortino in attribution.py and max_drawdown in
# fundamentals.py; those are not duplicated here.)
def value_at_risk(returns, *, confidence: float = 0.95) -> float:
    """Historical (non-parametric) Value-at-Risk as a decimal loss.

    ``confidence=0.95`` returns the 5th-percentile return (negative for a
    typical loss). Raises ``Unavailable`` on fewer than two finite observations.
    """
    r = pd.Series(np.asarray(returns, dtype=float)).dropna()
    if r.size < 2:
        raise Unavailable(f"need >= 2 finite returns for VaR, got {r.size}")
    alpha = 1.0 - confidence
    return float(np.percentile(r.to_numpy(), alpha * 100.0))


def expected_shortfall(returns, *, confidence: float = 0.95) -> float:
    """Historical Expected Shortfall (CVaR): mean return in the tail beyond VaR.

    The average of returns at or below the VaR quantile, as a decimal. Always
    ``<= value_at_risk`` (the tail mean cannot exceed the threshold); when only
    one observation falls in the tail it is that observation (the most extreme
    loss). Raises ``Unavailable`` on fewer than two finite observations.
    """
    r = pd.Series(np.asarray(returns, dtype=float)).dropna()
    if r.size < 2:
        raise Unavailable(f"need >= 2 finite returns for ES, got {r.size}")
    var = value_at_risk(r, confidence=confidence)
    arr = r.to_numpy()
    tail = arr[arr <= var]
    if tail.size == 0:
        return var
    return float(tail.mean())


# =========================================================================== #
# Factor risk model (ported from risk_model.py)
# =========================================================================== #
@dataclass
class RiskDecomposition:
    """Portfolio risk split into factor (systematic) vs specific components."""

    total_variance: float
    factor_variance: float
    specific_variance: float
    total_vol: float
    factor_vol: float
    specific_vol: float
    factor_share: float
    specific_share: float
    factor_contributions: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total_variance": self.total_variance,
            "factor_variance": self.factor_variance,
            "specific_variance": self.specific_variance,
            "total_vol": self.total_vol,
            "factor_vol": self.factor_vol,
            "specific_vol": self.specific_vol,
            "factor_share": self.factor_share,
            "specific_share": self.specific_share,
            "factor_contributions": dict(self.factor_contributions),
        }


@dataclass
class FactorRiskModel:
    """A fitted multi-factor risk model.

    ``exposures`` is the (n_assets x n_factors) beta matrix ``B``;
    ``factor_cov`` is the (n_factors x n_factors) factor-return covariance
    ``Sigma_f``; ``specific_var`` is the (n_assets,) idiosyncratic residual
    variance per asset. The implied asset covariance is
    ``B Sigma_f B' + diag(specific_var)``.
    """

    assets: list
    factors: list
    exposures: np.ndarray
    factor_cov: np.ndarray
    specific_var: np.ndarray
    alphas: np.ndarray
    r_squared: np.ndarray
    periods_per_year: int = 252

    def asset_covariance(self) -> pd.DataFrame:
        """Implied asset covariance ``B Sigma_f B' + diag(specific)`` (per period)."""
        cov = self.exposures @ self.factor_cov @ self.exposures.T
        cov = cov + np.diag(self.specific_var)
        cov = 0.5 * (cov + cov.T)
        return pd.DataFrame(cov, index=self.assets, columns=self.assets)

    def decompose_portfolio_risk(self, weights) -> RiskDecomposition:
        """Decompose a weight vector's variance into factor vs specific parts.

        ``weights`` may be a 1-D array aligned to ``self.assets`` or a pandas
        Series keyed by asset id (reindexed to the model's asset order; missing
        assets are treated as zero weight).
        """
        w = self._align_weights(weights)

        x = self.exposures.T @ w
        factor_var = float(x @ self.factor_cov @ x)
        specific_var = float(np.sum((w**2) * self.specific_var))
        total_var = factor_var + specific_var

        sigma_x = self.factor_cov @ x
        contrib = x * sigma_x
        factor_contributions = {f: float(c) for f, c in zip(self.factors, contrib)}

        total_var = max(total_var, 0.0)
        factor_var = max(factor_var, 0.0)
        specific_var = max(specific_var, 0.0)
        share_denom = total_var if total_var > 0 else 1.0

        return RiskDecomposition(
            total_variance=total_var,
            factor_variance=factor_var,
            specific_variance=specific_var,
            total_vol=float(np.sqrt(total_var)),
            factor_vol=float(np.sqrt(factor_var)),
            specific_vol=float(np.sqrt(specific_var)),
            factor_share=factor_var / share_denom,
            specific_share=specific_var / share_denom,
            factor_contributions=factor_contributions,
        )

    def annualized_volatility(self, weights) -> float:
        """Annualised portfolio volatility implied by the model."""
        dec = self.decompose_portfolio_risk(weights)
        return dec.total_vol * np.sqrt(self.periods_per_year)

    def _align_weights(self, weights) -> np.ndarray:
        if isinstance(weights, pd.Series):
            w = weights.reindex(self.assets).fillna(0.0).to_numpy(dtype=float)
        else:
            w = np.asarray(weights, dtype=float)
            if w.shape[0] != len(self.assets):
                raise ValueError(f"weights length {w.shape[0]} != n_assets {len(self.assets)}")
        return w


def fit_factor_risk_model(
    asset_returns: pd.DataFrame,
    factor_returns: pd.DataFrame,
    *,
    periods_per_year: int = 252,
    min_obs: int = 24,
    specific_var_floor: float = 1e-12,
) -> FactorRiskModel:
    """Fit a multi-factor risk model by per-asset time-series regression.

    OLS with an intercept, solved via least squares on the rows where the asset
    and all factors are jointly observed. Assets without enough overlap to
    regress (but with >= 2 finite returns) get zero exposures and a specific
    variance equal to their sample return variance (treated as fully
    idiosyncratic). An asset with fewer than 2 finite returns has no estimable
    variance at all; it raises ``Unavailable`` rather than report a floor
    constant as an estimate.

    Empty panels, or panels too short for even one asset's regression
    (``aligned rows < max(min_obs, n_factors + 2)``), raise ``Unavailable``: a
    factor model fit on no usable overlap is not a model, and v1's silent path
    to an all-idiosyncratic result in that case is the default-substitution this
    repo removes.
    """
    if asset_returns.empty or factor_returns.empty:
        raise Unavailable("asset_returns and factor_returns must be non-empty")

    factors = list(factor_returns.columns)
    assets = list(asset_returns.columns)
    n_factors = len(factors)

    aligned = asset_returns.join(factor_returns, how="inner", rsuffix="_f")
    f_panel = factor_returns.reindex(aligned.index)

    min_needed = max(min_obs, n_factors + 2)
    if aligned.shape[0] < min_needed:
        raise Unavailable(
            f"only {aligned.shape[0]} aligned observations; "
            f"need >= {min_needed} to fit a factor model"
        )

    exposures = np.zeros((len(assets), n_factors), dtype=float)
    alphas = np.zeros(len(assets), dtype=float)
    specific_var = np.zeros(len(assets), dtype=float)
    r_squared = np.zeros(len(assets), dtype=float)

    F_full = f_panel.to_numpy(dtype=float)

    for i, asset in enumerate(assets):
        y_full = asset_returns.reindex(aligned.index)[asset].to_numpy(dtype=float)
        mask = np.isfinite(y_full) & np.all(np.isfinite(F_full), axis=1)
        n = int(mask.sum())
        if n < max(min_obs, n_factors + 2):
            y_obs = y_full[np.isfinite(y_full)]
            if y_obs.size < 2:
                raise Unavailable(
                    f"asset '{asset}' has {y_obs.size} finite observation(s); "
                    "cannot estimate specific variance"
                )
            specific_var[i] = float(np.var(y_obs, ddof=1))
            specific_var[i] = max(specific_var[i], specific_var_floor)
            continue

        y = y_full[mask]
        F = F_full[mask]
        X = np.column_stack([np.ones(n), F])

        coef, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        alphas[i] = coef[0]
        exposures[i] = coef[1:]

        resid = y - X @ coef
        dof = max(n - (n_factors + 1), 1)
        sv = float(resid @ resid) / dof
        specific_var[i] = max(sv, specific_var_floor)

        tss = float(np.sum((y - y.mean()) ** 2))
        rss = float(resid @ resid)
        r_squared[i] = 1.0 - rss / tss if tss > 0 else 0.0

    F_clean = f_panel.dropna()
    if F_clean.shape[0] < 2:
        factor_cov = np.zeros((n_factors, n_factors))
    else:
        factor_cov = np.cov(F_clean.to_numpy(dtype=float), rowvar=False, ddof=1)
        factor_cov = np.atleast_2d(factor_cov)
        if factor_cov.shape != (n_factors, n_factors):
            factor_cov = factor_cov.reshape(n_factors, n_factors)

    return FactorRiskModel(
        assets=assets,
        factors=factors,
        exposures=exposures,
        factor_cov=factor_cov,
        specific_var=specific_var,
        alphas=alphas,
        r_squared=r_squared,
        periods_per_year=periods_per_year,
    )


# =========================================================================== #
# Position sizing (ported from sizing.py)
# =========================================================================== #
def ewma_volatility(
    returns,
    *,
    span: int = 32,
    annualize: bool = True,
    periods_per_year: int = TRADING_DAYS,
) -> float:
    """EWMA (~25-36d) volatility of a return series (Appendix A.14).

    Returns annualised vol by default. v1 returned 0.0 when fewer than two
    finite observations were present -- a fabricated "no risk" reading. Too few
    observations raises ``Unavailable`` here.
    """
    r = pd.Series(np.asarray(returns, dtype=float)).dropna()
    if r.size < 2:
        raise Unavailable(f"need >= 2 finite returns for EWMA vol, got {r.size}")
    var = r.ewm(span=span, adjust=False).var(bias=False).iloc[-1]
    vol = float(np.sqrt(max(var, 0.0)))
    if annualize:
        vol *= np.sqrt(periods_per_year)
    return vol


def atr(high, low, close, *, period: int = 14) -> float:
    """Average True Range (Clenow/Turtles scale for ATR sizing, A.14).

    v1 returned 0.0 on fewer than two observations; that is a fabricated "no
    range" reading. Too few observations raises ``Unavailable`` here.
    """
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    if h.size < 2:
        raise Unavailable(f"need >= 2 bars for ATR, got {h.size}")
    prev_close = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce(
        [
            h - l,
            np.abs(h - prev_close),
            np.abs(l - prev_close),
        ]
    )
    tr = pd.Series(tr)
    return float(tr.ewm(span=period, adjust=False).mean().iloc[-1])


def atr_position_size(
    *,
    equity: float,
    atr_value: float,
    price: float,
    risk_fraction: float = 0.0015,
    point_value: float = 1.0,
) -> float:
    """Clenow/Turtle ATR position size: units = risk_fraction*equity/(ATR*pv).

    ``risk_fraction`` ~0.1-0.2% per position (A.14). Returns number of
    units/contracts (>= 0). ``price`` is accepted for callers that want to
    convert to notional but is not needed for the unit count itself.
    """
    if atr_value <= 0 or point_value <= 0 or equity <= 0:
        return 0.0
    return float(risk_fraction * equity) / (atr_value * point_value)


def vol_target_weights(
    asset_vols,
    *,
    target_vol: float = 0.10,
    corr_matrix: np.ndarray | None = None,
    max_leverage: float = 1.0,
) -> pd.Series:
    """Risk-parity (inverse-vol) weights scaled to a portfolio vol target.

    The raw weights are inverse-vol (equal risk contribution under equal
    correlation). They are then scaled by ``target_vol / realised_portfolio_vol``
    so the portfolio hits the configured annual vol target, capped at
    ``max_leverage``.

    v1 returned a zero vector when every volatility was non-positive (no
    invertible signal) -- silently declaring "no position" rather than admitting
    the inputs were unusable. That raises ``Unavailable`` here.
    """
    if isinstance(asset_vols, pd.Series):
        index = list(asset_vols.index)
        vols = asset_vols.to_numpy(dtype=float)
    else:
        vols = np.asarray(asset_vols, dtype=float)
        index = [f"A{i}" for i in range(vols.size)]

    safe = np.where(vols > 0, vols, np.nan)
    inv = 1.0 / safe
    inv = np.nan_to_num(inv, nan=0.0)
    if inv.sum() == 0:
        raise Unavailable("no asset has a positive volatility; cannot vol-target")
    w = inv / inv.sum()

    if corr_matrix is not None:
        cov = np.outer(vols, vols) * np.asarray(corr_matrix, dtype=float)
        port_vol = float(np.sqrt(max(w @ cov @ w, 0.0)))
    else:
        port_vol = float(np.sqrt(np.sum((w * vols) ** 2)))

    if port_vol > 0:
        scale = target_vol / port_vol
    else:
        scale = 0.0
    w = w * scale
    gross = float(np.abs(w).sum())
    if gross > max_leverage and gross > 0:
        w = w * (max_leverage / gross)
    return pd.Series(w, index=index)


def risk_contributions_from_vol(weights, asset_vols, corr_matrix=None) -> np.ndarray:
    """Normalised per-asset risk contributions for given weights and vols."""
    w = np.asarray(weights, dtype=float)
    vols = np.asarray(asset_vols, dtype=float)
    if corr_matrix is None:
        corr = np.eye(w.size)
    else:
        corr = np.asarray(corr_matrix, dtype=float)
    cov = np.outer(vols, vols) * corr
    sigma_w = cov @ w
    pv = float(w @ sigma_w)
    if pv <= 0:
        return np.zeros_like(w)
    return w * sigma_w / pv


# --------------------------------------------------------------------------- #
# Fractional Kelly (A.15)
# --------------------------------------------------------------------------- #
def kelly_fraction(edge: float, odds: float) -> float:
    """Full Kelly fraction f* = edge / odds (A.15).

    Clamped to >= 0 (no shorting via a negative Kelly here).
    """
    if odds <= 0:
        return 0.0
    return max(edge / odds, 0.0)


def fractional_kelly(
    edge: float,
    odds: float,
    *,
    fraction: float = 0.5,
    cap: float = 0.20,
) -> float:
    """Half-Kelly (default) capped fraction of capital to risk (A.15).

    ``fraction`` = 0.5 is the bible's half-Kelly; ``cap`` is a hard ceiling
    (full Kelly is ruinous if the edge is overestimated). Returns a value in
    [0, cap].
    """
    f_star = kelly_fraction(edge, odds)
    return float(min(fraction * f_star, cap))


def kelly_from_win_prob(p_win: float, win_loss_ratio: float = 1.0) -> float:
    """Full Kelly from win probability: f* = p - (1-p)/b (binary-bet form)."""
    if win_loss_ratio <= 0:
        return 0.0
    f = p_win - (1.0 - p_win) / win_loss_ratio
    return max(f, 0.0)


# --------------------------------------------------------------------------- #
# Meta-label probability -> size
# --------------------------------------------------------------------------- #
def meta_label_size(
    probability: float,
    *,
    threshold: float = 0.5,
    max_size: float = 1.0,
) -> float:
    """Map a meta-label probability to a [0, max_size] size multiplier (S4.1).

    Below ``threshold`` the secondary model says "don't take the primary
    signal" -> size 0. Above it, size scales linearly from 0 at the threshold to
    ``max_size`` at probability 1.0.
    """
    p = float(np.clip(probability, 0.0, 1.0))
    if p <= threshold:
        return 0.0
    return float(max_size * (p - threshold) / (1.0 - threshold))


# --------------------------------------------------------------------------- #
# Active drawdown breaker
# --------------------------------------------------------------------------- #
def current_drawdown(equity_curve) -> float:
    """Current drawdown from the running peak, as a POSITIVE fraction.

    e.g. equity at 0.85 of its prior peak -> returns 0.15. An empty curve raises
    ``Unavailable``: v1 returned 0.0 (fabricated "no drawdown"), which is
    exactly the value that disabled the breaker downstream.
    """
    eq = pd.Series(np.asarray(equity_curve, dtype=float)).dropna()
    if eq.size == 0:
        raise Unavailable("empty equity curve; drawdown is unknown")
    peak = eq.cummax().iloc[-1]
    if peak <= 0:
        return 0.0
    dd = 1.0 - eq.iloc[-1] / peak
    return float(max(dd, 0.0))


def drawdown_breaker(
    equity_curve,
    *,
    threshold: float = 0.15,
    size: float = 1.0,
    reduce_threshold: float | None = None,
    reduce_to: float = 0.5,
) -> float:
    """Scale a desired position ``size`` down based on current drawdown.

    ``equity_curve`` is required (no default): without it the drawdown is
    unknown and no size is produced. v1's decision pipeline hardcoded
    ``portfolio_drawdown_pct = 0.0``, which disabled this breaker entirely; that
    path is not reproduced.

    - Drawdown >= ``threshold``  -> trip the breaker: return 0.0 (halt new risk).
    - Drawdown >= ``reduce_threshold`` (if set) -> scale to ``reduce_to * size``.
    - Otherwise                  -> return ``size`` unchanged.
    """
    dd = current_drawdown(equity_curve)
    if dd >= threshold:
        return 0.0
    if reduce_threshold is not None and dd >= reduce_threshold:
        return float(reduce_to * size)
    return float(size)
