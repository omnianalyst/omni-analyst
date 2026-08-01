"""Portfolio risk: VaR, CVaR, beta, correlation, and stress scenarios.

Ported from v1 `app/services/risk/risk_calculator.py` (the portfolio-risk
pieces only) and `app/services/portfolio_construction/stress.py`. The market- /
economic- / correlation-regime / geopolitical sibling lives in
`capabilities/risk.py`; the two modules do not overlap -- that one scores the
*market's* risk regime, this one measures a *portfolio's* risk against its own
return series and a benchmark. None of the functions there are duplicated here.

What was dropped from `RiskCalculator`:
- The FastAPI / SQLAlchemy / Redis shell (the constructor, the `portfolio_id` +
  `db.Session` entry point, `_get_historical_data`, `_cache_risk_metrics`,
  `_get_sector`, `_calculate_sector_risk`, `_calculate_position_risks`,
  `_calculate_concentration_risk`, `_calculate_risk_score`,
  `_generate_risk_recommendations`). The fetch / store layer is the framework
  tangle PORTING.md says to drop; the composite risk score and the
  recommendations are a "balanced / warning" narrative built on hardcoded
  thresholds, not analysis.
- Sharpe, Sortino, max-drawdown and the position / concentration / sector
  breakouts: out of scope for this work order (titled "VaR, CVaR, beta,
  correlation, stress"). They sit in v1 alongside the in-scope math and can be
  ported later under their own work order.

Where v1 substituted a default on missing input, this module raises
`Unavailable` instead (per the work order):
- `_calculate_portfolio_beta` returned 1.0 -- the "market beta" -- when SPY
  could not be fetched, when the two series differed in length, or when market
  variance was 0. Each is raised here: a beta of 1.0 on no data is a
  fabricated reading.
- `_calculate_correlation_matrix` returned `{"message": "Need at least 2
  positions..."}` (a dict carrying no matrix) on <2 positions or <2 aligned
  observations, and silently produced a matrix of NaNs when a series had zero
  variance. All three raised here.
- `_calculate_var` / `_calculate_cvar` returned `{"error": "No returns data"}`
  on empty input. Raised here, along with two v1 did not guard: a sample too
  short to resolve the (1-c) tail, and a zero-variance (constant) sample.

Service-vs-handler disagreements (detailed in report H2.md):
- VaR quantile. The service interpolates via `np.percentile`; the handler
  indexes `sorted_returns[int((1-c)*N)]` with no interpolation. The service is
  ported; the handler's discrete-index path is recorded as a divergence --
  they produce different numbers on the same input.
- VaR / CVaR sign. The service returns signed returns (a loss is negative);
  the handler negates losses to positive dollars. The service convention is
  kept.
- Monte Carlo. The service includes it, drawing from `np.random.normal(mean,
  std, 10000)` against a hidden global RNG; the handler omits it. Per the work
  order it is ported -- a stated normal model over real supplied inputs -- with
  the seed lifted to an explicit argument and the docstring labelling the
  output as model-derived.
- Beta ddof. v1 divides `np.cov` (ddof=1) by `np.var` (ddof=0), which makes
  `beta(x, x) = N/(N-1)` rather than 1. The work order's required outcome
  (beta of a series against itself is 1) is the oracle, so both terms use
  ddof=1 here and the identity holds exactly. This is a real bug in v1, not a
  style choice.

The stress port reshapes `stress_book` to take `assets`, `factors`,
`exposures` as explicit arguments rather than a `FactorRiskModel` instance:
the model class is out of this work order's scope and pulling it in would drag
a second service with it. The linear algebra is verbatim from
`portfolio_construction/stress.py`; the factor-attribution-sums-to-total
acceptance property is preserved.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from omni.ingest.protocol import Unavailable

_TRADING_DAYS = 252
# Tolerance for the zero-dispersion guard, on the standard deviation (the
# scale-consistent quantity per AGENTS.md). Matches attribution.py:75. A
# constant, non-exactly-representable series -- e.g. [0.05]*50 -- has a true
# variance of 0 but np.var returns ~1e-34 and np.std ~1e-17, so an `== 0.0`
# guard passes; this tolerance refuses it.
_ZERO_STD_ATOL = 1e-12


# ---------------------------------------------------------------------------
# Shared input guards
# ---------------------------------------------------------------------------

def _check_confidence(confidence_level: float) -> None:
    if not 0.0 < confidence_level < 1.0:
        raise ValueError(
            f"confidence_level must be in (0, 1), got {confidence_level}"
        )


def _min_observations(confidence_level: float) -> int:
    # The empirical (1-c) tail must hold at least one whole observation for the
    # historical quantile to be resolved rather than extrapolated. The epsilon
    # guards against float drift making an exact integer (20.0 -> 20.0000001).
    return math.ceil(1.0 / (1.0 - confidence_level) - 1e-9)


def _coerce_returns(
    returns: Sequence[float], *, name: str = "returns"
) -> np.ndarray:
    if returns is None:
        raise Unavailable(f"{name} is None")
    arr = np.asarray(list(returns), dtype=float)
    if arr.size == 0:
        raise Unavailable(f"{name} is empty")
    return arr


def _require_resolvable_tail(
    returns_array: np.ndarray, confidence_level: float
) -> None:
    min_obs = _min_observations(confidence_level)
    if len(returns_array) < min_obs:
        raise Unavailable(
            f"need >= {min_obs} observations to resolve the "
            f"{(1 - confidence_level) * 100:.1f}% tail at confidence "
            f"{confidence_level}, got {len(returns_array)}"
        )


def _require_nonzero_variance(returns_array: np.ndarray, what: str) -> None:
    if np.isclose(float(np.std(returns_array)), 0.0, atol=_ZERO_STD_ATOL):
        raise Unavailable(
            f"{what}: sample variance is 0 (constant series); no risk signal"
        )


# ---------------------------------------------------------------------------
# Value at Risk
# ---------------------------------------------------------------------------

def calculate_var(
    returns: Sequence[float],
    confidence_level: float = 0.95,
    portfolio_value: float | None = None,
    *,
    seed: int | None = None,
) -> dict[str, Any]:
    """Value at Risk by historical, parametric, and Monte-Carlo methods.

    Faithful to v1 `RiskCalculator._calculate_var` plus `_monte_carlo_var`.
    Percentages are signed (a loss is negative); dollar amounts are positive
    (`portfolio_value * abs(return)`), matching the service. As in v1, amounts
    are `None` when `portfolio_value` is falsy (None or 0).

    The Monte-Carlo leg draws from a normal distribution fit to the supplied
    `returns` (its mean and std). It is a MODEL OUTPUT, not a measurement:
    empirical return distributions are fat-tailed and left-skewed relative to
    a normal, so this figure systematically under-prices tail risk. Treat it
    as a what-if under a stated assumption, not as ground truth. `seed` makes
    the draw reproducible; v1 drew from a hidden global RNG and could not
    reproduce its own MC VaR.

    Raises `Unavailable` when `returns` is empty, when the empirical (1-c) tail
    cannot hold a whole observation (`len(returns) < 1/(1-c)`; e.g. <20 at
    95%, <100 at 99%), or when the sample has zero variance. Raises
    `ValueError` when `confidence_level` is outside (0, 1).
    """
    _check_confidence(confidence_level)
    returns_array = _coerce_returns(returns)
    _require_resolvable_tail(returns_array, confidence_level)
    _require_nonzero_variance(returns_array, "VaR")

    mean_return = float(np.mean(returns_array))
    std_return = float(np.std(returns_array))

    historical_var = float(
        np.percentile(returns_array, (1 - confidence_level) * 100)
    )

    z_score = float(stats.norm.ppf(1 - confidence_level))
    parametric_var = mean_return + z_score * std_return
    annual_parametric_var = (
        mean_return * _TRADING_DAYS
        + z_score * std_return * math.sqrt(_TRADING_DAYS)
    )

    monte_carlo_var = _monte_carlo_var(
        mean_return, std_return, confidence_level, seed=seed
    )

    return {
        "confidence_level": confidence_level,
        "historical": {
            "daily_var_pct": historical_var * 100,
            "daily_var_amount": (
                portfolio_value * abs(historical_var) if portfolio_value else None
            ),
            "annual_var_pct": historical_var * math.sqrt(_TRADING_DAYS) * 100,
            "annual_var_amount": (
                portfolio_value * abs(historical_var) * math.sqrt(_TRADING_DAYS)
                if portfolio_value
                else None
            ),
        },
        "parametric": {
            "daily_var_pct": parametric_var * 100,
            "daily_var_amount": (
                portfolio_value * abs(parametric_var) if portfolio_value else None
            ),
            "annual_var_pct": annual_parametric_var * 100,
            "annual_var_amount": (
                portfolio_value * abs(annual_parametric_var)
                if portfolio_value
                else None
            ),
        },
        "monte_carlo": {
            "daily_var_pct": monte_carlo_var * 100,
            "daily_var_amount": (
                portfolio_value * abs(monte_carlo_var) if portfolio_value else None
            ),
        },
    }


def _monte_carlo_var(
    mean_return: float,
    std_return: float,
    confidence_level: float,
    *,
    simulations: int = 10000,
    seed: int | None = None,
) -> float:
    """VaR from a normal(mean, std) draw over the supplied moments.

    MODEL-DERIVED, not measured: real return distributions are fat-tailed and
    left-skewed relative to a normal, so this figure systematically
    under-prices tail risk. It is kept because it is a stated model over real
    inputs (the caller's return series), never an invented series -- the line
    the work order draws. `seed` is explicit so the draw is reproducible; v1's
    `np.random.normal` drew from a hidden global RNG that could not reproduce
    its own output.
    """
    rng = np.random.default_rng(seed)
    simulated = rng.normal(mean_return, std_return, simulations)
    return float(np.percentile(simulated, (1 - confidence_level) * 100))


# ---------------------------------------------------------------------------
# Conditional Value at Risk (Expected Shortfall)
# ---------------------------------------------------------------------------

def calculate_cvar(
    returns: Sequence[float],
    confidence_level: float = 0.95,
    portfolio_value: float | None = None,
) -> dict[str, Any]:
    """Conditional VaR / Expected Shortfall.

    Faithful to v1 `RiskCalculator._calculate_cvar`: the threshold is the
    `np.percentile` historical VaR, and CVaR is the mean of the returns at or
    below that threshold (signed; a loss is negative). Dollar amounts are
    positive; `tail_risk_ratio` is `abs(cvar / var_threshold)` (1 when the
    threshold is 0, matching v1).

    Same refusal rules as `calculate_var`: empty, unresolvable tail, or zero
    variance each raise `Unavailable`; confidence outside (0, 1) raises
    `ValueError`.
    """
    _check_confidence(confidence_level)
    returns_array = _coerce_returns(returns)
    _require_resolvable_tail(returns_array, confidence_level)
    _require_nonzero_variance(returns_array, "CVaR")

    var_threshold = float(
        np.percentile(returns_array, (1 - confidence_level) * 100)
    )
    tail_returns = returns_array[returns_array <= var_threshold]
    cvar = float(np.mean(tail_returns)) if len(tail_returns) > 0 else var_threshold

    return {
        "confidence_level": confidence_level,
        "daily_cvar_pct": cvar * 100,
        "daily_cvar_amount": (
            portfolio_value * abs(cvar) if portfolio_value else None
        ),
        "annual_cvar_pct": cvar * math.sqrt(_TRADING_DAYS) * 100,
        "annual_cvar_amount": (
            portfolio_value * abs(cvar) * math.sqrt(_TRADING_DAYS)
            if portfolio_value
            else None
        ),
        "tail_risk_ratio": abs(cvar / var_threshold) if var_threshold != 0 else 1,
    }


# ---------------------------------------------------------------------------
# Beta
# ---------------------------------------------------------------------------

def calculate_beta(
    asset_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> float:
    """Beta of an asset's returns against a benchmark's.

    `beta = cov(asset, benchmark) / var(benchmark)`, with both terms at ddof=1
    so that `calculate_beta(x, x) == 1` exactly.

    DEVIATION FROM v1: `risk_calculator._calculate_portfolio_beta` and the
    `/portfolio/beta` handler both divided `np.cov(...)[0, 1]` (ddof=1) by
    `np.var(...)` (ddof=0), which makes `beta(x, x) = N / (N - 1)` rather than
    1. The work order's required outcome (beta of a series against itself is 1)
    is the oracle, so the denominator uses ddof=1 here. This is a real bug in
    v1, not a style difference: the beta identity does not hold under v1's
    code.

    v1 also returned a default 1.0 ("the market beta") on missing SPY data, on
    length mismatch, and on zero market variance; each is raised here.
    """
    asset = _coerce_returns(asset_returns, name="asset_returns")
    benchmark = _coerce_returns(benchmark_returns, name="benchmark_returns")

    if len(asset) != len(benchmark):
        raise ValueError(
            f"asset_returns and benchmark_returns must be equal length, "
            f"got {len(asset)} and {len(benchmark)}"
        )
    if len(asset) < 2:
        raise Unavailable(
            f"need >=2 paired observations for beta, got {len(asset)}"
        )

    benchmark_variance = float(np.var(benchmark, ddof=1))
    if np.isclose(float(np.std(benchmark, ddof=1)), 0.0, atol=_ZERO_STD_ATOL):
        raise Unavailable("benchmark variance is 0; beta is undefined")

    covariance = float(np.cov(asset, benchmark, ddof=1)[0, 1])
    return covariance / benchmark_variance


# ---------------------------------------------------------------------------
# Correlation matrix
# ---------------------------------------------------------------------------

def calculate_correlation_matrix(
    returns_by_symbol: Mapping[str, Sequence[float]],
    *,
    high_correlation_threshold: float = 0.7,
) -> dict[str, Any]:
    """Pearson correlation matrix across the supplied return series.

    Faithful to v1 `RiskCalculator._calculate_correlation_matrix`: builds a
    DataFrame keyed by symbol, computes `df.corr()`, and reports the full
    matrix, the pairs whose `abs(corr)` exceeds `high_correlation_threshold`
    (default 0.7, as in v1), and the mean of the upper-triangular entries.

    v1 truncated each series to the shared minimum length before building the
    frame; this port builds the frame directly and `dropna()`s, which keeps the
    same aligned rows (the intersection is identical). `pd.corr` is invariant
    to the difference.

    Raises `Unavailable` when fewer than two symbols are supplied, when fewer
    than two aligned observations remain, or when any series has zero variance
    (its correlations are undefined NaN). v1 returned a `{"message": ...}`
    dict on the first two and silently returned a matrix of NaNs on the third.
    """
    if len(returns_by_symbol) < 2:
        raise Unavailable(
            f"need >=2 symbols for correlation, got {len(returns_by_symbol)}"
        )

    df = pd.DataFrame(dict(returns_by_symbol)).dropna()
    if len(df) < 2:
        raise Unavailable("fewer than 2 aligned return observations after dropna")

    stds = df.std()
    zero_var = list(stds[np.isclose(stds, 0.0, atol=_ZERO_STD_ATOL)].index)
    if zero_var:
        raise Unavailable(
            f"zero-variance series; correlation undefined for: {zero_var}"
        )

    corr_matrix = df.corr()
    symbols = list(corr_matrix.columns)

    high_correlations: list[dict[str, Any]] = []
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            c = float(corr_matrix.iloc[i, j])
            if abs(c) > high_correlation_threshold:
                high_correlations.append(
                    {"pair": f"{symbols[i]}-{symbols[j]}", "correlation": c}
                )

    avg = float(
        corr_matrix.values[np.triu_indices_from(corr_matrix.values, k=1)].mean()
    )

    return {
        "matrix": corr_matrix.to_dict(),
        "high_correlations": high_correlations,
        "average_correlation": avg,
    }


# ---------------------------------------------------------------------------
# Stress
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    """A named stress scenario expressed as shocks to factor returns.

    Ported from `portfolio_construction/stress.py`. `factor_shocks` maps a
    factor name to a return shock (e.g. +0.01 = +100bp on a 'RATES' factor, or
    +0.20 for 'OIL'); factors not listed are 0. `specific_shocks` maps an asset
    to an idiosyncratic return shock applied on top of the factor move.
    """

    name: str
    factor_shocks: Mapping[str, float] = field(default_factory=dict)
    specific_shocks: Mapping[str, float] = field(default_factory=dict)
    description: str = ""


@dataclass
class StressResult:
    """The repriced P&L of a book under a scenario, with factor attribution."""

    scenario: str
    total_pnl: float
    factor_pnl: float
    specific_pnl: float
    factor_attribution: dict[str, float]
    asset_pnl: dict[str, float]
    total_value: float

    @property
    def total_return(self) -> float:
        return self.total_pnl / self.total_value if self.total_value else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "total_pnl": self.total_pnl,
            "factor_pnl": self.factor_pnl,
            "specific_pnl": self.specific_pnl,
            "factor_attribution": dict(self.factor_attribution),
            "asset_pnl": dict(self.asset_pnl),
            "total_value": self.total_value,
            "total_return": self.total_return,
        }


def stress_book(
    assets: Sequence[str],
    factors: Sequence[str],
    exposures: Sequence[Sequence[float]] | np.ndarray,
    positions: Mapping[str, float],
    scenario: Scenario,
) -> StressResult:
    """Reprice a book of positions under a scenario via the linear factor model.

    Ported from `portfolio_construction/stress.py::stress_book`. The math is
    verbatim; the interface is reshaped -- v1 took a fitted `FactorRiskModel`,
    this takes the model's three components (`assets`, `factors`, `exposures`)
    directly, because the model class is out of this work order's scope and
    pulling it in would drag a second service with it.

    An asset's shocked return is its factor exposures dotted with the factor
    shock vector plus any idiosyncratic shock:

        r_i = sum_k beta_i,k * shock_k  (+ specific_shock_i)

    Portfolio P&L = sum_i value_i * r_i. Because the model is linear, the total
    decomposes exactly into per-factor contributions plus the specific
    contribution (to floating-point tolerance) -- the acceptance property from
    `stress.py`.

    `positions` maps asset name to position VALUE (signed for long/short).
    Assets in `positions` but not in `assets` are ignored (no factor view),
    matching v1.

    Raises `Unavailable` when `positions` is empty or when the gross position
    value is 0: v1 silently returned an all-zero `StressResult`, which masks
    that nothing was actually priced.
    """
    if not positions:
        raise Unavailable("no positions supplied to stress_book")

    asset_index = {a: i for i, a in enumerate(assets)}
    factor_index = {f: i for i, f in enumerate(factors)}

    v = np.zeros(len(assets), dtype=float)
    for a, val in positions.items():
        idx = asset_index.get(a)
        if idx is not None:
            v[idx] = float(val)

    if float(np.abs(v).sum()) == 0.0:
        raise Unavailable("total gross position value is 0; cannot scale P&L")

    B = np.asarray(exposures, dtype=float).reshape(len(assets), len(factors))

    s = np.zeros(len(factors), dtype=float)
    for f, shock in scenario.factor_shocks.items():
        idx = factor_index.get(f)
        if idx is not None:
            s[idx] = float(shock)

    spec = np.zeros(len(assets), dtype=float)
    for a, shock in scenario.specific_shocks.items():
        idx = asset_index.get(a)
        if idx is not None:
            spec[idx] = float(shock)

    asset_factor_ret = B @ s
    asset_ret = asset_factor_ret + spec
    asset_pnl_vec = v * asset_ret

    dollar_factor_exposure = v @ B
    factor_pnl_vec = dollar_factor_exposure * s
    factor_pnl = float(factor_pnl_vec.sum())
    specific_pnl = float((v * spec).sum())
    total_pnl = float(asset_pnl_vec.sum())

    return StressResult(
        scenario=scenario.name,
        total_pnl=total_pnl,
        factor_pnl=factor_pnl,
        specific_pnl=specific_pnl,
        factor_attribution={
            f: float(p) for f, p in zip(factors, factor_pnl_vec)
        },
        asset_pnl={a: float(p) for a, p in zip(assets, asset_pnl_vec)},
        total_value=float(np.abs(v).sum()),
    )


def run_scenarios(
    assets: Sequence[str],
    factors: Sequence[str],
    exposures: Sequence[Sequence[float]] | np.ndarray,
    positions: Mapping[str, float],
    scenarios: Sequence[Scenario],
) -> dict[str, StressResult]:
    """Reprice a book under several scenarios; returns name -> StressResult."""
    return {
        sc.name: stress_book(assets, factors, exposures, positions, sc)
        for sc in scenarios
    }
