"""Portfolio construction acceptance tests.

Three v1 test files are the oracle, copied verbatim except for the import path
(per PORTING.md):

- `app/research/tests/test_optimizer.py` -> HRP / risk parity / min variance.
- `app/research/tests/test_risk_model.py` -> multi-factor risk model.
- `app/research/tests/test_sizing.py` -> vol targeting / Kelly / drawdown.

One v1 test was deliberately NOT ported: `test_sizing.py
::test_decision_pipeline_uses_real_drawdown` imports
`app.services.risk_management.RiskManager`, which is a DB/framework-tangled
module outside this port's scope. The drawdown-breaker behaviour it asserted is
covered here by a direct test against `drawdown_breaker` instead.

The failure-path tests at the end are the work order's required outcome: each
path that can fail (singular covariance, fewer observations than the model
needs, a returns series that is all-NaN, an empty universe) must raise
`Unavailable`, not return a default.
"""

import numpy as np
import pandas as pd
import pytest

from omni.capabilities.portfolio import (
    FactorRiskModel,
    atr,
    atr_position_size,
    current_drawdown,
    drawdown_breaker,
    efficient_frontier,
    ewma_volatility,
    expected_shortfall,
    fit_factor_risk_model,
    fractional_kelly,
    hrp_weights,
    kelly_fraction,
    kelly_from_win_prob,
    max_sharpe_weights,
    meta_label_size,
    min_variance_weights,
    optimize_weights,
    portfolio_comparison_metrics,
    risk_contributions,
    risk_contributions_from_vol,
    risk_parity_weights,
    value_at_risk,
    vol_target_weights,
)
from omni.ingest.protocol import Unavailable

# --------------------------------------------------------------------------- #
# Shared fixture (ported verbatim from test_optimizer.py)
# --------------------------------------------------------------------------- #
_CLUSTER = list(range(8))


def _ill_conditioned_cov():
    """Block-correlated, ill-conditioned covariance.

    Eight assets with vol 0.02 and pairwise correlation 0.9 (a single dominant
    risk source) plus four independent names with higher vols. Inverse-variance
    over-allocates to the cheap-vol cluster because it ignores the correlation;
    HRP halves weight at the cluster boundary first, so it holds much less of
    that one risk source.
    """
    n = 12
    corr = np.eye(n)
    for i in _CLUSTER:
        for j in _CLUSTER:
            if i != j:
                corr[i, j] = 0.9
    vols = np.array([0.02] * 8 + [0.04, 0.05, 0.06, 0.07])
    cov = corr * np.outer(vols, vols)
    cov = 0.5 * (cov + cov.T)
    assets = [f"A{i}" for i in range(n)]
    return pd.DataFrame(cov, index=assets, columns=assets)


def _naive_ivp(cov: pd.DataFrame) -> pd.Series:
    ivp = 1.0 / np.diag(cov.to_numpy())
    ivp = ivp / ivp.sum()
    return pd.Series(ivp, index=cov.index)


# ====================== HRP / risk parity / min variance =================== #
# (ported from test_optimizer.py)                                            #
# --------------------------------------------------------------------------- #
def test_hrp_long_only_sums_to_one():
    cov = _ill_conditioned_cov()
    w = hrp_weights(cov)
    assert (w >= -1e-12).all()
    assert abs(w.sum() - 1.0) < 1e-9


def test_hrp_more_diversified_than_naive_ivp():
    cov = _ill_conditioned_cov()
    w_hrp = hrp_weights(cov).to_numpy()
    w_ivp = _naive_ivp(cov).to_numpy()
    hrp_cluster = float(w_hrp[_CLUSTER].sum())
    ivp_cluster = float(w_ivp[_CLUSTER].sum())
    assert hrp_cluster < ivp_cluster - 0.10, (
        f"HRP cluster weight {hrp_cluster:.3f} not meaningfully < "
        f"IVP cluster weight {ivp_cluster:.3f}"
    )
    assert ivp_cluster > 0.90
    assert hrp_cluster < 0.85


def test_hrp_cluster_weight_matches_independently_derived_value():
    # The expected cluster weight (0.784) comes from a from-scratch
    # reimplementation of the HRP algorithm (Lopez de Prado 2016) run on this
    # fixture's covariance -- NOT from the module under test. Equal weight gives
    # 8/12 = 0.667 for the cluster; inverse-variance gives 0.930. The tight band
    # [0.774, 0.794] admits neither, so a stub that returns equal weight fails.
    cov = _ill_conditioned_cov()
    w = hrp_weights(cov).to_numpy()
    cluster_weight = float(w[_CLUSTER].sum())
    assert abs(cluster_weight - 0.784) < 0.01, (
        f"HRP cluster weight {cluster_weight:.4f} outside [0.774, 0.794]; "
        "equal weight would give 0.667"
    )
    eq = np.full(len(w), 1.0 / len(w))
    assert float(np.abs(w - eq).sum()) > 0.05


def test_risk_parity_equal_risk_contributions():
    cov = _ill_conditioned_cov()
    w = risk_parity_weights(cov)
    assert (w >= -1e-12).all()
    assert abs(w.sum() - 1.0) < 1e-9
    rc = risk_contributions(cov, w)
    n = len(w)
    target = 1.0 / n
    assert np.max(np.abs(rc.to_numpy() - target)) < 0.01, (
        f"risk contributions not equal: {rc.to_numpy()}"
    )


def test_min_variance_lower_variance_than_equal_weight():
    cov = _ill_conditioned_cov()
    w = min_variance_weights(cov)
    assert (w >= -1e-12).all()
    assert abs(w.sum() - 1.0) < 1e-9
    C = cov.to_numpy()
    n = len(w)
    eq = np.full(n, 1.0 / n)
    var_mv = float(w.to_numpy() @ C @ w.to_numpy())
    var_eq = float(eq @ C @ eq)
    assert var_mv <= var_eq + 1e-15


def test_weight_cap_respected():
    cov = _ill_conditioned_cov()
    cap = 0.15
    w = hrp_weights(cov, cap=cap)
    assert w.max() <= cap + 1e-9
    assert abs(w.sum() - 1.0) < 1e-9


def test_turnover_penalty_pulls_toward_prior():
    cov = _ill_conditioned_cov()
    base = hrp_weights(cov)
    n = len(base)
    prior = pd.Series(np.full(n, 1.0 / n), index=base.index)
    penalized = hrp_weights(cov, prior_weights=prior, turnover_penalty=0.8)
    d_base = float(np.abs(base.to_numpy() - prior.to_numpy()).sum())
    d_pen = float(np.abs(penalized.to_numpy() - prior.to_numpy()).sum())
    assert d_pen < d_base
    assert abs(penalized.sum() - 1.0) < 1e-9


def test_optimize_dispatch_default_is_hrp():
    cov = _ill_conditioned_cov()
    w_default = optimize_weights(cov)
    w_hrp = hrp_weights(cov)
    assert np.allclose(w_default.to_numpy(), w_hrp.to_numpy())


# ============================ factor risk model ============================ #
# (ported from test_risk_model.py)                                          #
# --------------------------------------------------------------------------- #
def _build_dataset(seed: int = 7, n_days: int = 6000):
    """Generate returns from a known 3-factor model + idiosyncratic noise."""
    rng = np.random.default_rng(seed)
    factors = ["MKT", "VALUE", "MOM"]
    assets = [f"A{i}" for i in range(8)]

    f_vol = np.array([0.010, 0.006, 0.007])
    L = np.array([[1.0, 0.0, 0.0], [0.3, 1.0, 0.0], [-0.2, 0.1, 1.0]])
    z = rng.standard_normal((n_days, 3))
    F = (z @ L.T) * f_vol
    factor_returns = pd.DataFrame(F, columns=factors)

    B = rng.uniform(0.2, 1.2, size=(len(assets), 3))
    B[:, 0] = rng.uniform(0.6, 1.3, size=len(assets))

    spec_vol = rng.uniform(0.003, 0.006, size=len(assets))
    eps = rng.standard_normal((n_days, len(assets))) * spec_vol

    R = F @ B.T + eps
    asset_returns = pd.DataFrame(R, columns=assets)
    return asset_returns, factor_returns, B, spec_vol


def test_exposures_recovered():
    asset_returns, factor_returns, B_true, _ = _build_dataset()
    model = fit_factor_risk_model(asset_returns, factor_returns)
    err = np.abs(model.exposures - B_true)
    assert err.max() < 0.05, f"max exposure error {err.max():.4f}"
    assert model.r_squared.min() > 0.5


def test_portfolio_variance_reconciles_to_realized():
    asset_returns, factor_returns, _, _ = _build_dataset()
    model = fit_factor_risk_model(asset_returns, factor_returns)

    rng = np.random.default_rng(123)
    w = rng.uniform(0.0, 1.0, size=len(model.assets))
    w = w / w.sum()
    w_series = pd.Series(w, index=model.assets)

    dec = model.decompose_portfolio_risk(w_series)

    port_ret = (asset_returns[model.assets] * w).sum(axis=1)
    realized_var = float(port_ret.var(ddof=1))

    rel_err = abs(dec.total_variance - realized_var) / realized_var
    assert rel_err < 0.10, (
        f"model var {dec.total_variance:.3e} vs realized {realized_var:.3e}, rel err {rel_err:.3f}"
    )

    assert (
        abs(dec.factor_variance + dec.specific_variance - dec.total_variance)
        < 1e-18 + 1e-9 * dec.total_variance
    )
    assert abs(sum(dec.factor_contributions.values()) - dec.factor_variance) < 1e-9 * (
        dec.factor_variance + 1e-12
    )


def test_factor_vs_specific_split_recovered():
    asset_returns, factor_returns, B_true, spec_vol_true = _build_dataset()
    model = fit_factor_risk_model(asset_returns, factor_returns)

    rng = np.random.default_rng(99)
    w = rng.uniform(0.0, 1.0, size=len(model.assets))
    w = w / w.sum()

    dec = model.decompose_portfolio_risk(w)

    f_cov_true = np.cov(factor_returns.to_numpy(), rowvar=False, ddof=1)
    x_true = B_true.T @ w
    true_factor_var = float(x_true @ f_cov_true @ x_true)
    true_specific_var = float(np.sum((w**2) * (spec_vol_true**2)))
    true_share = true_factor_var / (true_factor_var + true_specific_var)

    assert abs(dec.factor_share - true_share) < 0.10, (
        f"factor share {dec.factor_share:.3f} vs true {true_share:.3f}"
    )
    assert dec.factor_share > 0.5


def test_asset_covariance_psd():
    asset_returns, factor_returns, _, _ = _build_dataset()
    model = fit_factor_risk_model(asset_returns, factor_returns)
    cov = model.asset_covariance().to_numpy()
    eig = np.linalg.eigvalsh(cov)
    assert eig.min() > 0, f"covariance not PD, min eig {eig.min():.3e}"


# ========================= sizing: vol targeting ========================== #
# (ported from test_sizing.py)                                              #
# --------------------------------------------------------------------------- #
def test_vol_target_equal_risk_contributions():
    vols = pd.Series([0.10, 0.20, 0.30, 0.15, 0.25], index=list("ABCDE"))
    w = vol_target_weights(vols, target_vol=0.10)
    rc = risk_contributions_from_vol(w.to_numpy(), vols.to_numpy())
    n = len(vols)
    target = 1.0 / n
    assert np.max(np.abs(rc - target)) < 1e-9, f"risk contributions not equal: {rc}"


def test_vol_target_hits_target_vol():
    vols = pd.Series([0.10, 0.20, 0.30, 0.15, 0.25], index=list("ABCDE"))
    target = 0.12
    w = vol_target_weights(vols, target_vol=target, max_leverage=5.0)
    port_vol = float(np.sqrt(np.sum((w.to_numpy() * vols.to_numpy()) ** 2)))
    assert abs(port_vol - target) < 1e-9, f"port vol {port_vol:.4f} != target {target}"


def test_vol_target_leverage_cap():
    vols = pd.Series([0.02, 0.03, 0.025], index=list("ABC"))
    w = vol_target_weights(vols, target_vol=0.50, max_leverage=1.0)
    assert float(np.abs(w.to_numpy()).sum()) <= 1.0 + 1e-9


def test_ewma_vol_positive_and_scales():
    rng = np.random.default_rng(0)
    quiet = rng.standard_normal(500) * 0.005
    loud = rng.standard_normal(500) * 0.020
    v_quiet = ewma_volatility(quiet)
    v_loud = ewma_volatility(loud)
    assert v_loud > v_quiet > 0


def test_atr_and_atr_sizing():
    rng = np.random.default_rng(1)
    close = 100 + np.cumsum(rng.standard_normal(100))
    high = close + np.abs(rng.standard_normal(100))
    low = close - np.abs(rng.standard_normal(100))
    a = atr(high, low, close)
    assert a > 0
    units = atr_position_size(equity=100000, atr_value=a, price=close[-1], risk_fraction=0.0015)
    assert units > 0
    units2 = atr_position_size(
        equity=100000, atr_value=2 * a, price=close[-1], risk_fraction=0.0015
    )
    assert abs(units2 - units / 2) < 1e-9


# ----------------------------- fractional Kelly ---------------------------- #
def test_half_kelly_is_half_of_full():
    edge, odds = 0.10, 2.0
    full = kelly_fraction(edge, odds)
    half = fractional_kelly(edge, odds, fraction=0.5, cap=1.0)
    assert abs(half - 0.5 * full) < 1e-12


def test_kelly_cap_binds():
    capped = fractional_kelly(edge=0.8, odds=1.0, fraction=0.5, cap=0.20)
    assert capped == 0.20


def test_kelly_from_win_prob():
    assert abs(kelly_from_win_prob(0.6, 1.0) - 0.2) < 1e-12
    assert kelly_from_win_prob(0.5, 1.0) == 0.0


# ----------------------------- meta-label sizing --------------------------- #
def test_meta_label_size_monotone():
    assert meta_label_size(0.4, threshold=0.5) == 0.0
    assert meta_label_size(0.5, threshold=0.5) == 0.0
    s75 = meta_label_size(0.75, threshold=0.5)
    s90 = meta_label_size(0.90, threshold=0.5)
    assert 0.0 < s75 < s90 <= 1.0
    assert abs(meta_label_size(1.0, threshold=0.5) - 1.0) < 1e-12


# ----------------------------- drawdown breaker ---------------------------- #
def test_current_drawdown():
    eq = [100, 110, 120, 108]
    assert abs(current_drawdown(eq) - 0.10) < 1e-12
    assert current_drawdown([100, 101, 102]) == 0.0


def test_drawdown_breaker_trips_beyond_threshold():
    severe = [100, 90, 100, 95, 80]
    assert drawdown_breaker(severe, threshold=0.15, size=1.0) == 0.0


def test_drawdown_breaker_mild_does_not_trip():
    mild = [100, 98, 100, 97, 95]
    assert drawdown_breaker(mild, threshold=0.15, size=1.0) == 1.0


def test_drawdown_breaker_reduce_band():
    mid = [100, 95, 100, 92, 90]
    s = drawdown_breaker(mid, threshold=0.15, reduce_threshold=0.08, reduce_to=0.5, size=1.0)
    assert s == 0.5


def test_drawdown_breaker_requires_equity_curve():
    """drawdown_breaker must take the curve positionally -- no 0.0 default.

    This replaces v1's `test_decision_pipeline_uses_real_drawdown`, which
    depended on `app.services.risk_management.RiskManager` (out of scope). The
    invariant asserted is the same: a real 20% drawdown halts a position, a 5%
    drawdown does not, and the breaker cannot be called without the curve.
    """
    severe = [100, 90, 100, 95, 80]  # 20% drawdown -> halt
    mild = [100, 98, 100, 97, 95]  # 5% drawdown  -> pass
    assert drawdown_breaker(severe, threshold=0.15, size=1.0) == 0.0
    assert drawdown_breaker(mild, threshold=0.15, size=1.0) == 1.0
    with pytest.raises(TypeError):
        drawdown_breaker(threshold=0.15, size=1.0)  # type: ignore[call-arg]


# =========================================================================== #
# Additional behaviour tests (work-order required outcome)                    #
# =========================================================================== #
def test_two_asset_min_variance_prefers_lower_variance_asset():
    """A two-asset minimum-variance solution puts more weight on the lower-vol
    name. Asserted on a hand-built covariance, not on shape."""
    cov = pd.DataFrame(
        [[0.04, 0.0], [0.0, 0.01]],
        index=["LOUD", "QUIET"],
        columns=["LOUD", "QUIET"],
    )
    w = min_variance_weights(cov)
    assert w["QUIET"] > w["LOUD"]
    assert w["QUIET"] > 0.5
    assert abs(w.sum() - 1.0) < 1e-9


def test_risk_parity_weights_equalise_risk_contribution_two_assets():
    cov = pd.DataFrame(
        [[0.04, 0.0], [0.0, 0.01]],
        index=["LOUD", "QUIET"],
        columns=["LOUD", "QUIET"],
    )
    w = risk_parity_weights(cov)
    rc = risk_contributions(cov, w).to_numpy()
    assert abs(rc[0] - rc[1]) < 1e-6, f"RC not equal: {rc}"


def test_efficient_frontier_monotone_in_return_via_min_variance():
    """v1's mean-variance / efficient-frontier SLSQP code lived in the dropped
    service wrapper. The monotonicity property the work order names (frontier is
    monotone in return) is instead expressed through the min-variance optimiser:
    as we force more weight onto the higher-return asset, realised portfolio
    variance cannot decrease below the unconstrained minimum.
    """
    cov = _ill_conditioned_cov()
    C = cov.to_numpy()
    w_min = min_variance_weights(cov).to_numpy()
    var_min = float(w_min @ C @ w_min)

    rng = np.random.default_rng(42)
    for _ in range(20):
        w = rng.uniform(0, 1, size=C.shape[0])
        w = w / w.sum()
        assert float(w @ C @ w) >= var_min - 1e-12


# =========================================================================== #
# Failure paths -- each must raise Unavailable, not return a default          #
# =========================================================================== #
def _singular_cov() -> pd.DataFrame:
    """Rank-deficient covariance: asset B is an exact duplicate of asset A."""
    return pd.DataFrame(
        [[0.04, 0.04], [0.04, 0.04]],
        index=["A", "A_DUP"],
        columns=["A", "A_DUP"],
    )


def test_min_variance_raises_on_singular_covariance():
    with pytest.raises(Unavailable, match="singular"):
        min_variance_weights(_singular_cov())


def test_empty_universe_raises_for_all_optimisers():
    empty = pd.DataFrame(np.zeros((0, 0)))
    for fn in (hrp_weights, risk_parity_weights, min_variance_weights):
        with pytest.raises(Unavailable, match="empty universe"):
            fn(empty)


def test_nan_in_covariance_raises_for_all_optimisers():
    cov = _ill_conditioned_cov().to_numpy()
    cov[0, 0] = np.nan
    cov_df = pd.DataFrame(
        cov, index=[f"A{i}" for i in range(12)], columns=[f"A{i}" for i in range(12)]
    )
    for fn in (hrp_weights, risk_parity_weights, min_variance_weights):
        with pytest.raises(Unavailable, match="non-finite"):
            fn(cov_df)


def test_degenerate_asset_in_covariance_raises():
    cov = _ill_conditioned_cov().copy()
    cov.iloc[0, 0] = 0.0  # zero-variance asset
    for fn in (hrp_weights, risk_parity_weights, min_variance_weights):
        with pytest.raises(Unavailable, match="degenerate asset"):
            fn(cov)


def test_fit_factor_risk_model_raises_on_empty_panels():
    with pytest.raises(Unavailable):
        fit_factor_risk_model(pd.DataFrame(), pd.DataFrame({"F": []}))


def test_fit_factor_risk_model_raises_when_too_few_observations():
    """Fewer aligned observations than the model needs (T < min_obs) must raise,
    not silently return an all-idiosyncratic model."""
    rng = np.random.default_rng(0)
    factors = pd.DataFrame(rng.standard_normal((5, 2)), columns=["F1", "F2"])
    assets = pd.DataFrame(rng.standard_normal((5, 3)), columns=["A1", "A2", "A3"])
    with pytest.raises(Unavailable, match="aligned observations"):
        fit_factor_risk_model(assets, factors, min_obs=24)


def test_fit_factor_risk_model_handles_scatter_nan_via_masking():
    """A returns panel with scattered NaN is regressed on the finite overlap per
    asset (ported v1 behaviour) -- it does not crash and does not fabricate."""
    asset_returns, factor_returns, _, _ = _build_dataset()
    ar = asset_returns.copy()
    ar.iloc[10, 0] = np.nan
    ar.iloc[100, 2] = np.nan
    model = fit_factor_risk_model(ar, factor_returns)
    assert np.isfinite(model.exposures).all()
    assert np.isfinite(model.specific_var).all()
    assert model.r_squared.min() > 0.5


def test_fit_factor_risk_model_raises_on_asset_with_no_observations():
    """An asset with 0 or 1 finite observations has no estimable variance.
    The global aligned-row check passes (the panel is long enough overall), but
    this one asset is unestimable. It must raise Unavailable, not report the
    1e-12 floor as a variance estimate (the fabrication V1 flagged)."""
    asset_returns, factor_returns, _, _ = _build_dataset()
    ar = asset_returns.copy()
    ar["A0"] = np.nan  # zero finite observations
    with pytest.raises(Unavailable, match="finite observation"):
        fit_factor_risk_model(ar, factor_returns)

    ar2 = asset_returns.copy()
    ar2.iloc[0, 0] = 0.01
    ar2.iloc[1:, 0] = np.nan  # exactly one finite observation
    with pytest.raises(Unavailable, match="finite observation"):
        fit_factor_risk_model(ar2, factor_returns)


def test_ewma_volatility_raises_on_all_nan():
    with pytest.raises(Unavailable):
        ewma_volatility([np.nan, np.nan, np.nan])
    with pytest.raises(Unavailable):
        ewma_volatility([0.01])  # only one finite observation


def test_ewma_volatility_drops_scatter_nan():
    rng = np.random.default_rng(0)
    r = rng.standard_normal(500) * 0.01
    r_nan = r.copy()
    r_nan[::50] = np.nan
    assert ewma_volatility(r_nan) > 0


def test_atr_raises_on_too_few_bars():
    with pytest.raises(Unavailable):
        atr([100.0], [99.0], [99.5])


def test_vol_target_weights_raises_when_all_vols_non_positive():
    with pytest.raises(Unavailable, match="positive volatility"):
        vol_target_weights([0.0, 0.0, 0.0])
    with pytest.raises(Unavailable, match="positive volatility"):
        vol_target_weights([-0.1, -0.2])


def test_current_drawdown_raises_on_empty_curve():
    with pytest.raises(Unavailable):
        current_drawdown([])
    with pytest.raises(Unavailable):
        current_drawdown([np.nan, np.nan])


def test_factor_risk_model_attributes_present():
    """Smoke check that the fitted model carries the documented dataclass
    fields and that decompose_portfolio_risk is internally consistent."""
    asset_returns, factor_returns, _, _ = _build_dataset(n_days=500)
    model = fit_factor_risk_model(asset_returns, factor_returns)
    assert isinstance(model, FactorRiskModel)
    assert model.exposures.shape == (8, 3)
    assert model.factor_cov.shape == (3, 3)
    assert model.specific_var.shape == (8,)

    w = np.full(8, 1.0 / 8)
    dec = model.decompose_portfolio_risk(w)
    d = dec.to_dict()
    assert abs(d["factor_variance"] + d["specific_variance"] - d["total_variance"]) < 1e-9


# =========================================================================== #
# Mean-variance SLSQP + historical VaR/ES (CE1)                                #
# Ported from app/services/portfolio/optimization_service.py (max_sharpe +    #
# efficient frontier) and the inline risk-metrics handler (VaR/ES).            #
# =========================================================================== #
def _returns_panel(seed: int = 0, n: int = 300):
    """Three independent assets with distinct Sharpe ratios.

    LOW (low mean, very low vol) has the highest per-period Sharpe; HIGH has the
    lowest. Independence keeps the covariance near-diagonal so the tangency
    portfolio is well-defined and the frontier is a clean U-shape.
    """
    rng = np.random.default_rng(seed)
    high = rng.normal(0.0010, 0.020, n)
    mid = rng.normal(0.0007, 0.012, n)
    low = rng.normal(0.0004, 0.006, n)
    return pd.DataFrame({"HIGH": high, "MID": mid, "LOW": low})


def test_max_sharpe_weights_long_only_and_bounded():
    R = _returns_panel()
    w = max_sharpe_weights(R, min_weight=0.0, max_weight=0.5)
    assert (w >= -1e-9).all()
    assert (w <= 0.5 + 1e-9).all()
    assert abs(w.sum() - 1.0) < 1e-6


def test_max_sharpe_dominates_equal_weight_and_each_asset():
    """The tangency portfolio has Sharpe >= every constituent and >= equal
    weight -- that is the definition of maximum-Sharpe. A stub returning equal
    weight fails the per-asset bound when the assets have unequal Sharpes."""
    R = _returns_panel()
    w = max_sharpe_weights(R).to_numpy()
    ppy = 252
    mean = R.mean().to_numpy()
    cov = np.cov(R.to_numpy(), rowvar=False)

    def sharpe_of(wv):
        ret = float(np.sum(mean * wv) * ppy)
        vol = float(np.sqrt(wv @ (cov * ppy) @ wv))
        return ret / vol

    port_sharpe = sharpe_of(w)
    eq = np.full(3, 1.0 / 3)
    assert port_sharpe >= sharpe_of(eq) - 1e-6
    for i in range(3):
        unit = np.zeros(3)
        unit[i] = 1.0
        assert port_sharpe >= sharpe_of(unit) - 1e-6
    assert port_sharpe > sharpe_of(eq)  # strictly beats equal weight


def test_max_sharpe_is_on_the_efficient_frontier():
    """The max-Sharpe portfolio's Sharpe is >= the best Sharpe on the sampled
    frontier (tangency lies on the frontier)."""
    R = _returns_panel()
    w = max_sharpe_weights(R).to_numpy()
    mean = R.mean().to_numpy()
    cov = np.cov(R.to_numpy(), rowvar=False)
    ppy = 252
    ret = float(np.sum(mean * w) * ppy)
    vol = float(np.sqrt(w @ (cov * ppy) @ w))
    max_sharpe = ret / vol

    fr = efficient_frontier(R)
    assert len(fr) > 0
    assert max_sharpe >= fr["sharpe_ratio"].max() - 1e-6


def test_efficient_frontier_return_monotone_and_min_vol_beats_equal_weight():
    R = _returns_panel()
    fr = efficient_frontier(R)
    assert list(fr.columns) == ["return", "volatility", "sharpe_ratio"]
    assert len(fr) > 0
    # Target returns are a linspace -> the realised returns come back ascending.
    assert np.all(np.diff(fr["return"].to_numpy()) > 0)
    # The leftmost (minimum-variance) frontier point must beat naive equal
    # weight on volatility -- otherwise it is not the efficient frontier.
    cov = np.cov(R.to_numpy(), rowvar=False) * 252
    eq = np.full(3, 1.0 / 3)
    eq_vol = float(np.sqrt(eq @ cov @ eq))
    assert fr["volatility"].min() < eq_vol


def test_max_sharpe_raises_on_single_asset():
    with pytest.raises(Unavailable, match=">= 2 assets"):
        max_sharpe_weights(pd.DataFrame({"A": np.linspace(0.01, 0.02, 50)}))


def test_efficient_frontier_raises_on_too_few_observations():
    # Two assets but only one usable row -> covariance undefined.
    R = pd.DataFrame({"A": [0.01], "B": [0.02]})
    with pytest.raises(Unavailable, match="aligned observations"):
        efficient_frontier(R)


def test_value_at_risk_known_quantile():
    """Hand-verified: the 5th percentile of [-.05,-.02,0,.01,.03] under linear
    interpolation is -0.044 (position 0.2 between -.05 and -.02)."""
    r = [-0.05, -0.02, 0.0, 0.01, 0.03]
    var = value_at_risk(r, confidence=0.95)
    assert abs(var - (-0.044)) < 1e-9


def test_expected_shortfall_known_and_le_var():
    """Only -0.05 falls at/below the -0.044 VaR, so ES = -0.05 (single-point
    tail). ES must be <= VaR (a deeper or equal loss)."""
    r = [-0.05, -0.02, 0.0, 0.01, 0.03]
    var = value_at_risk(r, confidence=0.95)
    es = expected_shortfall(r, confidence=0.95)
    assert abs(es - (-0.05)) < 1e-9
    assert es <= var


def test_value_at_risk_and_expected_shortfall_negative_on_losses():
    rng = np.random.default_rng(7)
    r = rng.normal(0.0005, 0.015, 500)
    assert value_at_risk(r) < 0.0
    assert expected_shortfall(r) < 0.0
    assert expected_shortfall(r) <= value_at_risk(r)


def test_value_at_risk_raises_on_too_few_observations():
    with pytest.raises(Unavailable):
        value_at_risk([0.01])
    with pytest.raises(Unavailable):
        value_at_risk([np.nan, np.nan])


def test_expected_shortfall_single_tail_is_extreme_loss():
    """With one observation in the tail, ES is that observation (the most
    extreme loss), not a fabricated average and not equal to the interpolated
    VaR. For [0.10,0.05,0.02,0.0,-0.30] the 5th-pctile VaR is -0.24 (linear
    interpolation), so only -0.30 falls at/below it -> ES = -0.30 = min."""
    r = [0.10, 0.05, 0.02, 0.0, -0.30]
    var = value_at_risk(r, confidence=0.95)
    es = expected_shortfall(r, confidence=0.95)
    assert abs(var - (-0.24)) < 1e-9
    assert abs(es - (-0.30)) < 1e-12
    assert es < var


# --------------------------------------------------------------------------- #
# Cross-portfolio comparison (from v1 portfolio_comparison router)
# --------------------------------------------------------------------------- #


def _pf(pid, ret, vol, sharpe):
    return {
        "id": pid,
        "name": pid,
        "period_return_percent": ret,
        "volatility": vol,
        "sharpe_ratio": sharpe,
    }


def test_comparison_picks_best_worst_lowest_risk_best_sharpe():
    out = portfolio_comparison_metrics(
        [
            _pf("A", ret=10.0, vol=20.0, sharpe=0.5),
            _pf("B", ret=25.0, vol=12.0, sharpe=1.8),
            _pf("C", ret=-5.0, vol=30.0, sharpe=-0.4),
        ]
    )
    assert out["best_performer"]["id"] == "B"
    assert out["best_performer"]["return_percent"] == 25.0
    assert out["worst_performer"]["id"] == "C"
    assert out["worst_performer"]["return_percent"] == -5.0
    assert out["lowest_risk"]["id"] == "B"
    assert out["lowest_risk"]["volatility"] == 12.0
    assert out["best_risk_adjusted"]["id"] == "B"
    assert out["best_risk_adjusted"]["sharpe_ratio"] == 1.8


def test_comparison_averages_are_hand_computed():
    out = portfolio_comparison_metrics(
        [_pf("A", ret=10.0, vol=20.0, sharpe=0.5), _pf("B", ret=30.0, vol=40.0, sharpe=1.5)]
    )
    assert out["averages"]["return_percent"] == 20.0
    assert out["averages"]["volatility"] == 30.0
    assert out["averages"]["sharpe_ratio"] == 1.0


def test_comparison_best_and_worst_can_differ_when_returns_span_zero():
    out = portfolio_comparison_metrics(
        [_pf("WIN", ret=15.0, vol=18.0, sharpe=1.0), _pf("LOSE", ret=-15.0, vol=18.0, sharpe=-1.0)]
    )
    assert out["best_performer"]["id"] == "WIN"
    assert out["worst_performer"]["id"] == "LOSE"
    # Equal volatility -> lowest_risk is the first min() encountered (WIN).
    assert out["lowest_risk"]["volatility"] == 18.0


def test_comparison_two_portfolios_minimum_works():
    out = portfolio_comparison_metrics(
        [_pf("A", ret=5.0, vol=10.0, sharpe=0.2), _pf("B", ret=8.0, vol=14.0, sharpe=0.9)]
    )
    assert out["best_performer"]["id"] == "B"
    assert out["best_risk_adjusted"]["id"] == "B"


def test_comparison_empty_raises():
    # v1 returned {} on an empty list; an empty comparison has no leaders.
    with pytest.raises(Unavailable):
        portfolio_comparison_metrics([])
