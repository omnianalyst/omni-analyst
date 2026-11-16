"""Market regime detection as pure capabilities.

Ported from v1 `app/services/quant/regime_detection.py` (570 lines, the
"service") and the inline classifier at
`app/api/v1/endpoints/risk_analysis.py:203-281` (the "inline"). The two
disagree, and reconciling them is the work; the full disagreement table is in the
operator's archived research notes.

What carried, and from where:

- Volatility regime -- the INLINE endpoint's logic. It computes a trailing
  rolling realised vol from returns and bands each bar against the median of
  that rolling-vol series (`quiet` / `transition` / `volatile`). The service's
  `composite_regime_model` instead consumed a caller-supplied volatility series
  and banded it by its own 0.33 / 0.67 quantiles; that path depends on a
  separate vol feed this capability does not own, so the inline (which derives
  vol from returns) is the self-contained one and is what is ported.
- Trend regime -- the SERVICE's moving-average crossover (MA short vs MA long on
  returns -> uptrend / downtrend / neutral). The inline endpoint has no trend
  dimension at all.
- Regime-change detection -- both implementations agree (label at i differs from
  i-1) and is ported verbatim.

What did NOT carry:

- `markov_regime_switching` (Gaussian HMM), `threshold_autoregression`
  (TAR + OLS), and `dynamic_correlation_model` (DCC-GARCH). They need
  `hmmlearn`, `statsmodels` and `arch`; only `statsmodels` is in pyproject.toml,
  the other two are not, and PORTING.md forbids adding deps without
  justification. They are untested in v1, and the required outcome is covered by
  the threshold classifiers.
- `_test_threshold_nonlinearity` returns the hardcoded literals
  {5.23, 0.073, False} regardless of input -- fabrication by this repo's rule --
  and is dropped along with the TAR model that called it.
- `get_current_market_regime` returns a hardcoded `Bull_LowVol` / 0.72 /
  recommended-allocation dict with no input -- fabrication -- and is dropped.

Where v1 substituted a default on missing input -- the inline endpoint's
`window = min(20, len(returns))` (a shrinking lookback) and its empty-result
return when fewer than 20 bars were available -- this module raises
`Unavailable` instead. An undersized lookback is not a regime reading, and a
classifier that silently shrinks its window is how a stale-looking-but-covered
network enters the store.

Window convention (and a deliberate deviation from v1). Throughout this module
and ``capabilities/volatility.py``, ``window`` is the number of observations
the statistic is computed over. For ``realised_volatility`` that is returns, so
the interior slice is ``returns[i - window + 1 : i + 1]``. v1's inline endpoint
sliced ``returns[max(0, i - window) : i + 1]`` -- ``window + 1`` points -- which
disagreed with both its own ">= window" contract and the volatility
estimators; ``realised_volatility`` corrects it and therefore differs from v1
by one observation per window. See the function docstring and report S8.

Thresholds v1 hardcoded (the 1.3 / 2.0 vol multipliers and the 20 / 60 trend
windows) are named module constants below, exactly as `capabilities/risk.py`
treated its credit-spread anchors. None are tuned and none are invented.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from omni.ingest.protocol import Unavailable

# Inline /market-regime/history: band each bar's rolling vol against the median
# of the rolling-vol series.
_VOL_WINDOW = 20
_VOL_HIGH_MULT = 2.0  # vol > median * 2.0 -> "volatile"
_VOL_TRANS_MULT = 1.3  # vol > median * 1.3 -> "transition", else "quiet"

# Service composite_regime_model: trend = MA(short) vs MA(long) on returns.
_TREND_SHORT_WINDOW = 20
_TREND_LONG_WINDOW = 60


def _validate(series: np.ndarray, window: int) -> None:
    if window < 2:
        raise Unavailable(f"window must be >= 2, got {window}")
    if len(series) < window:
        raise Unavailable(
            f"need >= {window} observations for window={window}, got {len(series)}"
        )
    if not np.isfinite(series).all():
        raise Unavailable("input contains NaN or non-finite value")
    if np.ptp(series) == 0.0:
        raise Unavailable("input has zero variance; regime is undefined")


def realised_volatility(
    returns: Sequence[float], window: int = _VOL_WINDOW
) -> np.ndarray:
    """Trailing rolling population std of returns.

    ``window`` is the number of returns each statistic is computed over: the
    interior slice is ``returns[i - window + 1 : i + 1]`` -- exactly ``window``
    points. Uses population std (ddof=0), matching the inline; the volatility
    calculator's ``_simple_volatility`` uses sample std (pandas ddof=1) -- a
    disagreement noted in the report, not reconciled here. This is the one
    window convention shared with ``capabilities/volatility.close_to_close``
    and ``ewma``: the same integer passed to all three spans the same returns.

    DEVIATION FROM v1 (deliberate; see PORTING.md). The inline endpoint ported
    here sliced ``returns[max(0, i - window) : i + 1]`` -- ``window + 1`` points
    for an interior ``i`` -- which is faithful to v1 but wrong relative to the
    docstring's ">= window observations" contract and out of step with the
    volatility estimators. Fixing it makes this module disagree with v1's inline
    endpoint by one observation per window. That is a considered exception to
    bit-for-bit fidelity, recorded here, not an oversight.

    The first ``window - 1`` entries are computed over the SHRINKING window
    available so far (entry 0 is the std of one return, exactly 0). They are
    kept so the series stays aligned with the input, but they are not regime
    readings and neither classifier below uses them: see ``_full_window_vol``.
    """
    returns = np.asarray(returns, dtype=float)
    _validate(returns, window)
    out = np.empty(len(returns), dtype=float)
    for i in range(len(returns)):
        out[i] = np.std(returns[max(0, i - window + 1): i + 1])
    return out


WARMUP_LABEL = "warmup"


def _full_window_vol(returns: Sequence[float], window: int) -> np.ndarray:
    """Rolling vols computed over a FULL window only (entries ``window-1`` on).

    The warm-up entries of ``realised_volatility`` are shrinking-window stds --
    systematically small, and exactly 0 at index 0 -- so including them drags
    the median down and the current bar looks more volatile than it is. On iid
    returns (n=40, window=20) that put 9.6% of series in a non-quiet regime
    against 3.3% using full windows. The module docstring already says an
    undersized lookback is not a regime reading; this is where that is enforced.
    """
    return realised_volatility(returns, window)[window - 1:]


def _band(vol: float, median: float) -> str:
    if vol > median * _VOL_HIGH_MULT:
        return "volatile"
    if vol > median * _VOL_TRANS_MULT:
        return "transition"
    return "quiet"


def volatility_regime_path(
    returns: Sequence[float], window: int = _VOL_WINDOW
) -> list[str]:
    """Per-bar volatility regime label, causal.

    Returns one label per input bar: ``warmup`` for the first ``window - 1``
    bars (no full window yet, so no reading), then ``quiet`` / ``transition`` /
    ``volatile``. Each bar is banded against the EXPANDING median of the
    full-window vols seen up to and including that bar.

    DEVIATION FROM v1 (deliberate). The inline endpoint banded every historical
    bar against the median of the WHOLE series, so the label at bar ``i``
    depended on volatility that had not happened yet -- look-ahead the moment
    the path is used as a backtest feature or a change detector. The final
    bar's label is unchanged by this (its expanding median is the whole-series
    one) and agrees with ``classify_volatility``.
    """
    vol = _full_window_vol(returns, window)
    labels: list[str] = [WARMUP_LABEL] * (window - 1)
    seen: list[float] = []
    for v in vol:
        seen.append(float(v))
        labels.append(_band(float(v), float(np.median(seen))))
    return labels


def classify_volatility(
    returns: Sequence[float], window: int = _VOL_WINDOW
) -> dict[str, Any]:
    """Classify the current (most recent) bar's volatility regime.

    A regime label is a claim about the world and carries the window it was
    computed over; both are in the result. The median is taken over
    full-window vols only (see ``_full_window_vol``); with exactly ``window``
    returns there is one such vol, it is its own median, and the reading is
    ``quiet`` by construction -- more history is needed for it to mean anything.
    """
    vol = _full_window_vol(returns, window)
    current = float(vol[-1])
    median = float(np.median(vol))
    regime = _band(current, median)
    return {
        "regime": regime,
        "current_volatility": current,
        "median_volatility": median,
        "window": window,
    }


def classify_trend(
    returns: Sequence[float],
    short_window: int = _TREND_SHORT_WINDOW,
    long_window: int = _TREND_LONG_WINDOW,
) -> dict[str, Any]:
    """Classify the current trend regime via MA crossover on returns.

    Ported from the service's `composite_regime_model`: MA(short) > MA(long) ->
    `uptrend`, < -> `downtrend`, else `neutral`. The inline endpoint has no
    trend dimension.
    """
    returns_arr = np.asarray(returns, dtype=float)
    if short_window < 2:
        raise Unavailable(f"short_window must be >= 2, got {short_window}")
    _validate(returns_arr, max(short_window, long_window))

    s = pd.Series(returns_arr)
    ma_short = float(s.rolling(short_window).mean().iloc[-1])
    ma_long = float(s.rolling(long_window).mean().iloc[-1])

    if ma_short > ma_long:
        regime = "uptrend"
    elif ma_short < ma_long:
        regime = "downtrend"
    else:
        regime = "neutral"
    return {
        "regime": regime,
        "ma_short": ma_short,
        "ma_long": ma_long,
        "short_window": short_window,
        "long_window": long_window,
    }


def detect_regime_changes(regimes: Sequence[Any]) -> list[dict[str, Any]]:
    """Indices where consecutive regime labels differ.

    Both v1 implementations detect transitions this way (label at i differs
    from i-1). The detector introduces no lag of its own: it reports the exact
    index of the label flip. Any lag in a regime path comes from the
    classifier's rolling window, not from this function.

    A flip out of ``warmup`` is not a regime change -- nothing was being read
    before it -- and is not reported.
    """
    regimes = list(regimes)
    changes: list[dict[str, Any]] = []
    for i in range(1, len(regimes)):
        if regimes[i - 1] == WARMUP_LABEL:
            continue
        if regimes[i] != regimes[i - 1]:
            changes.append(
                {
                    "index": i,
                    "from_regime": regimes[i - 1],
                    "to_regime": regimes[i],
                }
            )
    return changes
