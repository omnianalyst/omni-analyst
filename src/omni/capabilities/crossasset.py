"""Cross-asset and edge metrics as pure capabilities.

Two v1 modules were lifted into this file:

1. `app/services/cross_asset_engine.py` -- relationships between asset classes
   (rolling correlations, risk-on/risk-off, sector rotation). Only the
   computation was lifted; the `_fetch_all_returns` fetch layer, the
   `market_data_service` dependency, the singleton instance and its
   `correlation_cache` / `roro_history` state are gone. The data each function
   needs arrives as a plain argument: a dict of `{symbol: returns}` for the
   correlation and RORO work, and a dict of `{sector: close-prices}` for
   rotation.

2. `app/research/edge_metrics.py` -- information-coefficient / quantile / hit
   rate statistics that decide whether a signal has any edge at all. That module
   was already pure (numpy/pandas/scipy, no fetch, no sessions); it is ported
   verbatim. It does not fabricate -- it returns NaN / None and an explicit
   "INSUFFICIENT DATA" verdict when a relationship cannot be measured -- so no
   `Unavailable` substitutions were needed there.

Where v1's cross-asset engine substituted a default on missing input -- an empty
matrix with `data_quality: "insufficient"`, a fabricated `0` credit-spread
reading when HYG had fewer than 10 bars, a `"NEUTRAL"` RORO classification when
no component could be computed, an `"unknown"` cycle phase when no sector had
enough data -- this module raises `Unavailable` instead. A capability that
returns a plausible-looking number on no data is how hallucinated coverage
enters the store.

Entry points (the analyses the orchestrator calls) are async. The leaf
mathematical helpers, and all of the edge-metrics statistics, are sync because
they do no IO -- matching `macro.py`'s shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from omni.ingest.protocol import Unavailable

# ---------------------------------------------------------------------------
# Cross-asset engine
# ---------------------------------------------------------------------------

def infer_cycle_phase(leaders: set[str]) -> str:
    """Infer economic cycle phase from sector leadership."""
    early_cycle = {"Financials", "Consumer Discretionary", "Industrials", "Real Estate"}
    mid_cycle = {"Technology", "Communication", "Industrials"}
    late_cycle = {"Energy", "Materials", "Healthcare"}
    recession = {"Utilities", "Consumer Staples", "Healthcare"}

    early_count = len(leaders & early_cycle)
    mid_count = len(leaders & mid_cycle)
    late_count = len(leaders & late_cycle)
    recess_count = len(leaders & recession)

    max_count = max(early_count, mid_count, late_count, recess_count)
    if max_count == 0:
        return "unknown"

    if early_count == max_count:
        return "early_cycle"
    elif mid_count == max_count:
        return "mid_cycle"
    elif late_count == max_count:
        return "late_cycle"
    else:
        return "recession"


def detect_divergences(corr_dict: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """Detect notable intermarket divergences from historical correlation norms."""
    divergences: list[dict[str, Any]] = []

    expected = {
        ("SPY", "VIX"): (-0.7, "stocks_volatility"),
        ("GLD", "TLT"): (0.3, "gold_bonds"),
        ("SPY", "HYG"): (0.6, "stocks_credit"),
    }

    for (sym1, sym2), (expected_corr, label) in expected.items():
        if sym1 in corr_dict and sym2 in corr_dict.get(sym1, {}):
            actual = corr_dict[sym1][sym2]
            diff = actual - expected_corr
            if abs(diff) > 0.3:
                divergences.append({
                    "pair": f"{sym1}/{sym2}",
                    "expected_correlation": expected_corr,
                    "actual_correlation": actual,
                    "divergence": round(diff, 3),
                    "label": label,
                    "significance": "high" if abs(diff) > 0.5 else "moderate",
                })

    return divergences


async def cross_asset_correlations(
    returns_data: dict[str, list[float]],
    long_returns: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    """Rolling correlation matrix across asset classes plus notable divergences.

    `returns_data` maps each symbol to its daily-return series (the caller owns
    the price->return conversion and lookback window). `long_returns`, when
    given, drives the short-vs-long `correlation_shifts` comparison; it is
    optional because the short-term matrix is the primary output.
    """
    if len(returns_data) < 4:
        raise Unavailable(
            f"need >=4 symbols for a correlation matrix, got {len(returns_data)}"
        )

    symbols = list(returns_data.keys())
    min_len = min(len(r) for r in returns_data.values())
    if min_len < 20:
        raise Unavailable(
            f"need >=20 returns per symbol to correlate, shortest series is {min_len}"
        )

    matrix = np.array([returns_data[s][:min_len] for s in symbols])
    corr_matrix = np.corrcoef(matrix)

    corr_dict: dict[str, dict[str, float]] = {}
    for i, sym1 in enumerate(symbols):
        corr_dict[sym1] = {}
        for j, sym2 in enumerate(symbols):
            corr_dict[sym1][sym2] = round(float(corr_matrix[i, j]), 3)

    divergences = detect_divergences(corr_dict)

    correlation_shifts: list[dict[str, Any]] = []
    if long_returns is not None and len(long_returns) >= 4:
        long_min = min(len(r) for r in long_returns.values())
        if long_min >= 50:
            long_symbols = list(long_returns.keys())
            long_matrix = np.array([long_returns[s][:long_min] for s in long_symbols])
            long_corr = np.corrcoef(long_matrix)

            for i, sym1 in enumerate(long_symbols):
                for j, sym2 in enumerate(long_symbols):
                    if i < j and sym1 in corr_dict and sym2 in corr_dict.get(sym1, {}):
                        short_corr = corr_dict[sym1][sym2]
                        long_c = float(long_corr[i, j])
                        diff = short_corr - long_c
                        if abs(diff) > 0.25:
                            correlation_shifts.append({
                                "pair": f"{sym1}/{sym2}",
                                "short_term_corr": short_corr,
                                "long_term_corr": round(long_c, 3),
                                "divergence": round(diff, 3),
                                "signal": (
                                    "correlation_breakdown"
                                    if diff < -0.25
                                    else "correlation_spike"
                                ),
                            })

    return {
        "matrix": corr_dict,
        "divergences": divergences,
        "correlation_shifts": correlation_shifts,
        "symbols_available": symbols,
        "data_points": min_len,
    }


async def roro_indicator(returns: dict[str, list[float]]) -> dict[str, Any]:
    """Risk-On/Risk-Off composite indicator.

    Combines VIX direction (30%), credit-spread proxy HYG-vs-TLT (25%), dollar
    strength UUP (20%) and small-cap breadth IWM-vs-SPY (25%) into a score from
    -1 (extreme risk-off) to +1 (extreme risk-on).

    Each component contributes only when every symbol it needs is present with
    enough history. v1 fabricated a `0` reading for any short series and so
    contributed a literal zero; that is gone -- a short series is treated like
    an absent one, which is mathematically identical for the composite (zero
    contributes nothing) but no longer lies in the `components` dict.
    """
    components: dict[str, float] = {}
    scores: list[float] = []

    if "VIX" in returns and len(returns["VIX"]) >= 5:
        vix_5d_return = sum(returns["VIX"][-5:])
        vix_score = max(-1.0, min(1.0, -vix_5d_return * 10))
        components["vix_direction"] = round(vix_score, 3)
        scores.append(vix_score * 0.30)

    if (
        "HYG" in returns
        and "TLT" in returns
        and len(returns["HYG"]) >= 10
        and len(returns["TLT"]) >= 10
    ):
        hyg_ret = sum(returns["HYG"][-10:])
        tlt_ret = sum(returns["TLT"][-10:])
        credit_score = max(-1.0, min(1.0, (hyg_ret - tlt_ret) * 20))
        components["credit_spread"] = round(credit_score, 3)
        scores.append(credit_score * 0.25)

    if "UUP" in returns and len(returns["UUP"]) >= 10:
        dollar_ret = sum(returns["UUP"][-10:])
        dollar_score = max(-1.0, min(1.0, -dollar_ret * 15))
        components["dollar_strength"] = round(dollar_score, 3)
        scores.append(dollar_score * 0.20)

    if (
        "IWM" in returns
        and "SPY" in returns
        and len(returns["IWM"]) >= 10
        and len(returns["SPY"]) >= 10
    ):
        iwm_ret = sum(returns["IWM"][-10:])
        spy_ret = sum(returns["SPY"][-10:])
        breadth_score = max(-1.0, min(1.0, (iwm_ret - spy_ret) * 15))
        components["breadth"] = round(breadth_score, 3)
        scores.append(breadth_score * 0.25)

    if not scores:
        raise Unavailable(
            "no RORO components could be computed from the supplied returns"
        )

    composite = max(-1.0, min(1.0, sum(scores)))

    if composite > 0.4:
        classification = "STRONG_RISK_ON"
    elif composite > 0.15:
        classification = "RISK_ON"
    elif composite > -0.15:
        classification = "NEUTRAL"
    elif composite > -0.4:
        classification = "RISK_OFF"
    else:
        classification = "STRONG_RISK_OFF"

    return {
        "score": round(composite, 3),
        "classification": classification,
        "components": components,
    }


async def sector_rotation(sector_prices: dict[str, Sequence[float]]) -> dict[str, Any]:
    """Sector rotation from injected close-price series.

    `sector_prices` maps a sector name to its daily close prices; the caller
    owns the fetch. Returns per-sector 5d/20d momentum, the top-3 / bottom-3
    ranked sectors and the inferred economic cycle phase.
    """
    momentum: dict[str, dict[str, Any]] = {}
    for sector_name, prices in sector_prices.items():
        positives = [p for p in prices if p > 0]
        if len(positives) >= 20:
            ret_20d = (positives[-1] - positives[-20]) / positives[-20]
            ret_5d = (positives[-1] - positives[-5]) / positives[-5]
            momentum[sector_name] = {
                "return_20d": round(ret_20d * 100, 2),
                "return_5d": round(ret_5d * 100, 2),
                "momentum_score": round(ret_20d * 0.6 + ret_5d * 0.4, 4),
            }

    if not momentum:
        raise Unavailable(
            "no sectors with >=20 positive prices; cannot rank rotation"
        )

    ranked = sorted(
        momentum.items(), key=lambda x: x[1]["momentum_score"], reverse=True
    )
    leaders = [{"sector": s, **d} for s, d in ranked[:3]]
    laggards = [{"sector": s, **d} for s, d in ranked[-3:]]

    leader_names = {s for s, _ in ranked[:3]}
    cycle_phase = infer_cycle_phase(leader_names)

    return {
        "sectors": momentum,
        "leaders": leaders,
        "laggards": laggards,
        "cycle_phase": cycle_phase,
    }


# ---------------------------------------------------------------------------
# Edge metrics -- information coefficient, quantile spread, hit rate
# (ported verbatim from app/research/edge_metrics.py; already pure)
# ---------------------------------------------------------------------------

# Minimum cross-section per date for a within-date correlation to be meaningful.
_MIN_CROSS_SECTION = 5
# Minimum number of periods (dates) for an averaged IC to be reported with a t-stat.
_MIN_PERIODS = 12


@dataclass
class ICResult:
    """Result of an information-coefficient analysis."""

    mean_ic: float
    ic_std: float
    ic_ir: float  # information ratio of the IC series = mean_ic / ic_std
    t_stat: float
    p_value: float
    n_periods: int
    positive_ic_rate: float  # fraction of periods with IC > 0
    method: str
    by_period: pd.Series = field(default_factory=lambda: pd.Series(dtype=float), repr=False)

    @property
    def is_significant(self) -> bool:
        """True if the IC is statistically distinguishable from zero (95%) with
        enough periods to trust it."""
        return (
            self.n_periods >= _MIN_PERIODS
            and np.isfinite(self.p_value)
            and self.p_value < 0.05
        )

    def to_dict(self) -> dict:
        return {
            "mean_ic": _finite(self.mean_ic),
            "ic_std": _finite(self.ic_std),
            "ic_ir": _finite(self.ic_ir),
            "t_stat": _finite(self.t_stat),
            "p_value": _finite(self.p_value),
            "n_periods": int(self.n_periods),
            "positive_ic_rate": _finite(self.positive_ic_rate),
            "method": self.method,
            "is_significant": bool(self.is_significant),
        }


@dataclass
class QuantileResult:
    """Result of a quantile (decile) analysis of a cross-sectional signal."""

    n_quantiles: int
    quantile_returns: pd.Series  # mean forward return per quantile (index 1..n)
    top_minus_bottom: float
    monotonicity: float  # Spearman corr of quantile rank vs mean return in [-1, 1]
    long_short_sharpe: float  # annualized Sharpe of the top-minus-bottom portfolio
    long_short_return_ann: float
    n_periods: int
    long_short_returns: pd.Series = field(default_factory=lambda: pd.Series(dtype=float), repr=False)

    def to_dict(self) -> dict:
        return {
            "n_quantiles": int(self.n_quantiles),
            "quantile_returns": {int(k): _finite(v) for k, v in self.quantile_returns.items()},
            "top_minus_bottom": _finite(self.top_minus_bottom),
            "monotonicity": _finite(self.monotonicity),
            "long_short_sharpe": _finite(self.long_short_sharpe),
            "long_short_return_ann": _finite(self.long_short_return_ann),
            "n_periods": int(self.n_periods),
        }


@dataclass
class SignalEvaluation:
    """Combined edge report for one signal."""

    name: str
    ic: ICResult
    quantiles: QuantileResult | None
    hit_rate: float
    n_observations: int
    verdict: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ic": self.ic.to_dict(),
            "quantiles": self.quantiles.to_dict() if self.quantiles else None,
            "hit_rate": _finite(self.hit_rate),
            "n_observations": int(self.n_observations),
            "verdict": self.verdict,
        }


def _finite(x: float) -> float | None:
    """JSON-safe float (NaN/inf -> None)."""
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    return xf if np.isfinite(xf) else None


def _corr(a: np.ndarray, b: np.ndarray, method: str) -> float:
    """Correlation of two arrays, NaN-safe, returning NaN on degenerate input."""
    mask = np.isfinite(a) & np.isfinite(b)
    a, b = a[mask], b[mask]
    if a.size < 3 or np.allclose(a, a[0]) or np.allclose(b, b[0]):
        return np.nan
    if method == "spearman":
        r, _ = stats.spearmanr(a, b)
    elif method == "pearson":
        r, _ = stats.pearsonr(a, b)
    else:
        raise ValueError(f"Unknown correlation method: {method!r}")
    return float(r)


def information_coefficient(
    panel: pd.DataFrame,
    signal_col: str,
    forward_return_col: str,
    date_col: str = "date",
    asset_col: str = "asset",
    method: str = "spearman",
    min_cross_section: int = _MIN_CROSS_SECTION,
) -> ICResult:
    """Cross-sectional information coefficient.

    For each date, correlate the signal across assets with their forward returns,
    then summarize the per-date IC series. The IC information ratio
    (mean_ic / std_ic) and its t-stat (`ic_ir * sqrt(n_periods)`) tell you whether
    the predictive relationship is consistent, not just present on average.
    """
    required = {date_col, asset_col, signal_col, forward_return_col}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"panel missing columns: {sorted(missing)}")

    ics: dict = {}
    for date, group in panel.groupby(date_col):
        if len(group) < min_cross_section:
            continue
        ic = _corr(
            group[signal_col].to_numpy(dtype=float),
            group[forward_return_col].to_numpy(dtype=float),
            method,
        )
        if np.isfinite(ic):
            ics[date] = ic

    by_period = pd.Series(ics, dtype=float).sort_index()
    return _summarize_ic(by_period, method)


def time_series_ic(
    signal: pd.Series,
    forward_return: pd.Series,
    method: str = "spearman",
    rolling_window: int | None = None,
    overlap: int = 1,
) -> ICResult:
    """Time-series information coefficient for a single-series signal.

    Correlates a signal series against an aligned forward-return series (e.g. a
    macro indicator predicting next-month SPY return). Both series must share an
    index (dates). If `rolling_window` is given, the per-period IC series is the
    rolling correlation, which lets the caller see how the relationship evolves.

    `overlap` is the number of periods of overlap between consecutive forward
    returns (= the forecast horizon when h-period returns are sampled every
    period). When > 1, consecutive observations are NOT independent, so the
    significance test is corrected via an effective sample size n/overlap --
    otherwise overlapping returns make pure noise look strongly significant.
    """
    s = pd.Series(signal, dtype=float)
    r = pd.Series(forward_return, dtype=float)
    joined = pd.concat([s.rename("s"), r.rename("r")], axis=1).dropna()
    if len(joined) < 3:
        return _empty_ic(method)

    if rolling_window and rolling_window >= 3 and len(joined) > rolling_window:
        if method == "spearman":
            ranks = joined.rank()
            by_period = ranks["s"].rolling(rolling_window).corr(ranks["r"]).dropna()
        else:
            by_period = joined["s"].rolling(rolling_window).corr(joined["r"]).dropna()
        return _summarize_ic(by_period, method)

    # Single full-sample correlation. The p-value uses an effective sample size
    # n/overlap so overlapping forward returns don't manufacture significance.
    a = joined["s"].to_numpy()
    b = joined["r"].to_numpy()
    if method == "spearman":
        r_val, _ = stats.spearmanr(a, b)
    else:
        r_val, _ = stats.pearsonr(a, b)
    r_val = float(r_val)
    n = len(joined)
    n_eff = max(n / max(overlap, 1), 3.0)
    denom = max(1.0 - r_val**2, 1e-12)
    t_stat = r_val * np.sqrt(n_eff - 2.0) / np.sqrt(denom)
    p_val = 2.0 * stats.t.sf(abs(t_stat), df=max(n_eff - 2.0, 1.0))
    return ICResult(
        mean_ic=r_val,
        ic_std=np.nan,
        ic_ir=np.nan,
        t_stat=float(t_stat),
        p_value=float(p_val),
        n_periods=n,
        positive_ic_rate=1.0 if r_val > 0 else 0.0,
        method=method,
        by_period=pd.Series({joined.index[-1]: r_val}, dtype=float),
    )


def _summarize_ic(by_period: pd.Series, method: str) -> ICResult:
    by_period = by_period.dropna()
    n = int(by_period.size)
    if n == 0:
        return _empty_ic(method)
    mean_ic = float(by_period.mean())
    ic_std = float(by_period.std(ddof=1)) if n > 1 else np.nan
    if n < 2:
        ic_ir = np.nan
        t_stat = np.nan
        p_value = np.nan
    elif not np.isfinite(ic_std) or ic_std == 0.0:
        # IC is perfectly constant across periods. If it's a non-zero constant the
        # relationship is trivially, perfectly consistent (infinite IR); if it's a
        # constant zero there is no relationship.
        if mean_ic != 0.0:
            ic_ir = np.inf
            t_stat = np.inf
            p_value = 0.0
        else:
            ic_ir = np.nan
            t_stat = 0.0
            p_value = 1.0
    else:
        ic_ir = mean_ic / ic_std
        t_stat = ic_ir * np.sqrt(n)
        p_value = 2.0 * stats.t.sf(abs(t_stat), df=n - 1)
    positive_rate = float((by_period > 0).mean())
    return ICResult(
        mean_ic=mean_ic,
        ic_std=ic_std,
        ic_ir=ic_ir,
        t_stat=float(t_stat) if np.isfinite(t_stat) else np.nan,
        p_value=float(p_value) if np.isfinite(p_value) else np.nan,
        n_periods=n,
        positive_ic_rate=positive_rate,
        method=method,
        by_period=by_period,
    )


def _empty_ic(method: str) -> ICResult:
    return ICResult(
        mean_ic=np.nan,
        ic_std=np.nan,
        ic_ir=np.nan,
        t_stat=np.nan,
        p_value=np.nan,
        n_periods=0,
        positive_ic_rate=np.nan,
        method=method,
        by_period=pd.Series(dtype=float),
    )


def quantile_analysis(
    panel: pd.DataFrame,
    signal_col: str,
    forward_return_col: str,
    date_col: str = "date",
    asset_col: str = "asset",
    n_quantiles: int = 5,
    periods_per_year: int = 252,
    min_cross_section: int = _MIN_CROSS_SECTION,
) -> QuantileResult:
    """Sort assets into quantiles by signal each date and measure the spread.

    Returns per-quantile mean forward returns, the top-minus-bottom spread, the
    monotonicity (does return increase with quantile?), and the annualized Sharpe
    of a dollar-neutral long-top / short-bottom portfolio. Monotonicity matters:
    a real signal should be roughly monotone across quantiles, not just have a
    big top-vs-bottom gap driven by one bucket.
    """
    required = {date_col, asset_col, signal_col, forward_return_col}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"panel missing columns: {sorted(missing)}")

    per_quantile_returns: dict = {q: [] for q in range(1, n_quantiles + 1)}
    long_short: dict = {}

    for date, group in panel.groupby(date_col):
        g = group[[signal_col, forward_return_col]].dropna()
        if len(g) < max(min_cross_section, n_quantiles):
            continue
        # qcut on ranks avoids failures from duplicate signal values. Use integer
        # codes (labels=False) rather than a categorical to keep a plain integer
        # index on the grouped means (categorical indexes break the membership and
        # .loc lookups below).
        try:
            codes = pd.qcut(g[signal_col].rank(method="first"), n_quantiles, labels=False)
        except ValueError:
            continue
        quant = pd.Series(codes, index=g.index) + 1  # 1..n_quantiles
        means = g[forward_return_col].groupby(quant).mean()
        for q in range(1, n_quantiles + 1):
            if q in means.index:
                per_quantile_returns[q].append(float(means.loc[q]))
        if n_quantiles in means.index and 1 in means.index:
            long_short[date] = float(means.loc[n_quantiles] - means.loc[1])

    quantile_means = pd.Series(
        {q: (np.mean(v) if v else np.nan) for q, v in per_quantile_returns.items()},
        dtype=float,
    )
    ls_returns = pd.Series(long_short, dtype=float).sort_index()
    n_periods = int(ls_returns.size)

    top_minus_bottom = float(quantile_means.get(n_quantiles, np.nan) - quantile_means.get(1, np.nan))

    valid = quantile_means.dropna()
    if valid.size >= 3:
        monotonicity = _corr(
            valid.index.to_numpy(dtype=float), valid.to_numpy(dtype=float), "spearman"
        )
    else:
        monotonicity = np.nan

    if n_periods >= 2:
        mu = float(ls_returns.mean())
        sd = float(ls_returns.std(ddof=1))
        ls_sharpe = (mu / sd * np.sqrt(periods_per_year)) if sd > 0 else np.nan
        ls_ann = mu * periods_per_year
    else:
        ls_sharpe = np.nan
        ls_ann = np.nan

    return QuantileResult(
        n_quantiles=n_quantiles,
        quantile_returns=quantile_means,
        top_minus_bottom=top_minus_bottom,
        monotonicity=monotonicity if np.isfinite(monotonicity) else np.nan,
        long_short_sharpe=ls_sharpe,
        long_short_return_ann=ls_ann,
        n_periods=n_periods,
        long_short_returns=ls_returns,
    )


def hit_rate(signal: np.ndarray, forward_return: np.ndarray) -> float:
    """Directional hit rate: fraction of observations where sign(signal) matches
    sign(forward_return). NaN-safe; returns NaN with no valid pairs.

    Note: a hit rate near 0.5 is no edge for a symmetric signal -- interpret it
    relative to the base rate, not in absolute terms.
    """
    s = np.asarray(signal, dtype=float)
    r = np.asarray(forward_return, dtype=float)
    mask = np.isfinite(s) & np.isfinite(r) & (s != 0)
    if mask.sum() == 0:
        return float("nan")
    return float((np.sign(s[mask]) == np.sign(r[mask])).mean())


def evaluate_signal(
    panel: pd.DataFrame,
    signal_col: str,
    forward_return_col: str,
    date_col: str = "date",
    asset_col: str = "asset",
    name: str | None = None,
    n_quantiles: int = 5,
    method: str = "spearman",
    periods_per_year: int = 252,
) -> SignalEvaluation:
    """Full edge report for one cross-sectional signal.

    Combines IC, quantile spread, and hit rate, and emits a plain-language verdict
    so a non-quant reader can tell signal from noise. Quantile analysis is skipped
    automatically when the cross-section is too thin to bucket.
    """
    ic = information_coefficient(
        panel, signal_col, forward_return_col, date_col, asset_col, method
    )

    # Only attempt quantiles if some date has enough names to bucket.
    max_cs = panel.groupby(date_col)[signal_col].count().max() if len(panel) else 0
    quantiles = None
    if max_cs and max_cs >= max(_MIN_CROSS_SECTION, n_quantiles):
        quantiles = quantile_analysis(
            panel, signal_col, forward_return_col, date_col, asset_col,
            n_quantiles, periods_per_year,
        )

    hr = hit_rate(
        panel[signal_col].to_numpy(dtype=float),
        panel[forward_return_col].to_numpy(dtype=float),
    )

    return SignalEvaluation(
        name=name or signal_col,
        ic=ic,
        quantiles=quantiles,
        hit_rate=hr,
        n_observations=len(panel),
        verdict=_verdict(ic, quantiles),
    )


def _verdict(ic: ICResult, quantiles: QuantileResult | None) -> str:
    """Plain-language read of whether a signal shows edge."""
    if ic.n_periods < _MIN_PERIODS:
        return "INSUFFICIENT DATA — too few periods to judge edge."
    if not np.isfinite(ic.mean_ic):
        return "INSUFFICIENT DATA — IC could not be computed."
    if not ic.is_significant:
        return "NO MEASURABLE EDGE — IC is not statistically distinguishable from zero."
    strength = abs(ic.mean_ic)
    if strength < 0.02:
        tier = "WEAK"
    elif strength < 0.05:
        tier = "MODEST"
    else:
        tier = "STRONG"
    direction = "positive" if ic.mean_ic > 0 else "inverted (signal predicts the opposite)"
    note = ""
    if (
        quantiles is not None
        and np.isfinite(quantiles.monotonicity)
        and quantiles.monotonicity < 0.5
        and ic.mean_ic > 0
    ):
        note = " Caution: quantile returns are not monotone — the spread may be driven by one bucket."
    return f"{tier} EDGE — significant {direction} IC ({ic.mean_ic:.3f}, IR {ic.ic_ir:.2f}).{note}"
