"""Realised volatility estimators as pure capabilities.

v1 spread its volatility logic across two files that disagree with each other
and with the regime endpoint about degrees of freedom (see "ddof
reconciliation" below). This module picks one convention, makes it explicit,
and matches ``capabilities/regime.py`` so the two v2 modules never report
different volatilities for the same series -- the exact v1 defect this rebuild
exists to remove.

Window convention
-----------------
``window`` is the number of observations the statistic is computed over,
everywhere in this module and in ``capabilities/regime.py``:

- ``close_to_close`` / ``ewma`` -- the statistic is a std / weighted variance
  of log returns, so ``window`` is a *return* count. ``window`` returns require
  ``window + 1`` prices; the trailing slice is ``returns[-window:]``. Passing
  the same integer to ``regime.realised_volatility`` and ``close_to_close``
  therefore spans the same returns and yields the same number (pinned in
  ``tests/test_cap_volatility.py``).
- ``parkinson`` / ``garman_klass`` / ``rogers_satchell`` -- the statistic is a
  mean of per-bar variance terms, so ``window`` is a *bar* count.
- ``volatility_of_volatility`` -- the statistic is a std of readings, so
  ``window`` is a *reading* count.

Estimators
----------
- ``close_to_close`` -- log-return std over a trailing window. v1's
  ``_simple_volatility`` (the close-to-close path) used simple returns
  (``pct_change``); this module uses log returns so the annualisation
  invariance property (sigma * sqrt(T) is invariant to sampling frequency)
  holds exactly, as the work order's required outcome demands.
- ``parkinson`` / ``garman_klass`` / ``rogers_satchell`` -- intraday OHLC
  estimators. **These are not in the v1 source.** The work order's "Source"
  line names ``volatility_calculator.py`` and lists these estimators, but the
  file contains only simple / EWMA / GARCH. They are implemented here from
  their standard closed forms (Parkinson 1980, Garman-Klass 1980,
  Rogers-Satchell 1991) because the work order requires them; they are an
  addition, not a port, and there is no v1 oracle to be bit-for-bit against.
- ``ewma`` -- RiskMetrics-style exponentially weighted variance
  (``sigma2_t = lambda * sigma2_{t-1} + (1-lambda) * r_t^2``), restricted to
  the trailing window. v1's ``_ewma_volatility`` used ``pandas.ewm(adjust=
  False).var()``; this module deviates -- see the ewma docstring.
- ``volatility_of_volatility`` -- population std of a series of volatility
  readings.

Every estimator takes an explicit ``window`` and an explicit ``annualisation``
factor. A volatility without a stated period is not a volatility. Where v1
substituted a default on missing or degenerate input -- an assumed 252-day
year on an irregular series, a zero when the window is too short, the
hardcoded ``_get_default_volatility`` per-asset values -- this module raises
``Unavailable`` instead.

ddof reconciliation
-------------------
v1 is self-inconsistent about the standard deviation's degrees of freedom:

- ``volatility_calculator._simple_volatility``: ``recent_returns.std()`` ->
  pandas default **ddof=1** (sample).
- ``volatility_calculator._ewma_volatility``: ``returns.ewm(...).var()`` ->
  pandas EW variance (de-meaned, anchored to the first observation), neither
  sample nor population std in the ddof sense.
- ``research/analytics/volatility.realized_volatility``:
  ``.rolling(w).std(ddof=0)`` -> **ddof=0** (population).
- the inline regime endpoint (ported as ``capabilities/regime.py``):
  ``np.std`` -> **ddof=0** (population).

This module defaults to **ddof=0** (population) everywhere a std of a sample
is taken, exposed as an explicit ``ddof`` argument on ``close_to_close`` and
``volatility_of_volatility``. With that default, and with the shared
return-count window convention above, ``volatility.py`` and ``regime.py``
produce identical volatility for the same return series at the same window
integer (a cross-check is pinned in ``tests/test_cap_volatility.py``). The OHLC
estimators carry no ``ddof`` choice: their per-bar variance contributions are
summed and divided by the bar count (a population mean), exactly as the
closed forms require. EWMA carries no ``ddof`` choice; it is a weighted recursion on squared
returns. Callers needing sample-std semantics pass ``ddof=1`` explicitly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from omni.ingest.protocol import Unavailable

# RiskMetrics (J.P. Morgan 1996) decay for the EWMA recursion. Named, not tuneable
# inline, matching how capabilities/regime.py treats its thresholds.
DEFAULT_LAMBDA = 0.94

# A constant-growth series built the normal way (p*g**i, exp(cumsum(c)),
# cumprod(...)) rounds to values that differ at the last few ULPs, so its spread
# is ~1e-16 * |value| -- numerically nonzero, economically zero. Feeding that
# into np.std yields ~1e-15 of pure rounding noise labelled as a volatility. The
# guard below treats such a spread as zero when it sits at float-rounding scale
# relative to the readings' magnitude. One relative tolerance for the whole
# module: log returns and vol-of-vol readings differ in scale by orders of
# magnitude, so an absolute tolerance would not mean the same thing in both
# callers. Measured noise ratio is ~1e-14; real return/vol spreads are O(0.1),
# so 1e-12 leaves ~2 orders above the noise and ~10 below any real spread.
_ZERO_VARIANCE_REL_TOL = 1e-12


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float


def _check_window(series_len: int, window: int) -> None:
    if window < 2:
        raise Unavailable(f"window must be >= 2, got {window}")
    if series_len < window:
        raise Unavailable(
            f"need >= {window} observations for window={window}, got {series_len}"
        )


def _check_annualisation(annualisation: float) -> None:
    if not annualisation > 0:
        raise Unavailable(
            f"annualisation must be > 0 (variance scales by sqrt(n)); got {annualisation}"
        )


def _is_effectively_constant(series: np.ndarray) -> bool:
    # `np.ptp(series) == 0.0` is exact but fires only on bit-identical values;
    # see _ZERO_VARIANCE_REL_TOL for why that is insufficient.
    magnitude = float(np.max(np.abs(series)))
    if magnitude == 0.0:
        return True
    return float(np.ptp(series)) <= magnitude * _ZERO_VARIANCE_REL_TOL


def _log_returns(prices: np.ndarray) -> np.ndarray:
    if np.isnan(prices).any():
        raise Unavailable("input contains NaN")
    if (prices <= 0).any():
        raise Unavailable("non-positive price: log undefined")
    return np.log(prices[1:] / prices[:-1])


def close_to_close(
    prices: Sequence[float],
    *,
    window: int,
    annualisation: float,
    ddof: int = 0,
) -> float:
    """Annualised close-to-close volatility from a price series.

    ``window`` is a *return* count (the shared convention): the std is taken
    over the trailing ``window`` log returns, which come from ``window + 1``
    prices. Population std by default (``ddof=0``), matching
    ``capabilities/regime.realised_volatility`` -- the same integer passed to
    both spans the same returns. A series with zero return variance (constant
    log return) raises rather than returning 0.0 -- the zero-variance rule
    shared with regime.
    """
    prices = np.asarray(prices, dtype=float)
    _check_window(len(prices), window)
    _check_annualisation(annualisation)
    returns = _log_returns(prices)
    if len(returns) < window:
        raise Unavailable(
            f"need >= {window} returns for window={window} "
            f"(>= {window + 1} prices), got {len(returns)}"
        )
    recent = returns[-window:]
    if _is_effectively_constant(recent):
        raise Unavailable("zero variance in returns; volatility undefined")
    return float(np.std(recent, ddof=ddof)) * np.sqrt(annualisation)


def ewma(
    prices: Sequence[float],
    *,
    window: int,
    annualisation: float,
    lambda_: float = DEFAULT_LAMBDA,
) -> float:
    """Annualised EWMA volatility over the trailing window.

    ``window`` is a *return* count (the shared convention): the weighted
    variance is taken over the trailing ``window`` log returns, which come from
    ``window + 1`` prices. The same integer passed to ``close_to_close`` and to
    ``regime.realised_volatility`` therefore spans the same returns.

    Normalised exponential weights on squared log returns (zero-mean)::

        w_k     = lambda_ ** (n - 1 - k)        # weight on r_k, newest gets 1
        sigma2  = sum(w_k r_k^2) / sum(w_k)

    As ``lambda_`` -> 1 every weight -> 1 and ``sigma2`` -> ``mean(r^2)`` --
    the equally-weighted population variance of zero-mean returns. As
    ``lambda_`` -> 0 only the most recent return carries weight and
    ``sigma2`` -> ``r_{n-1}^2``. This is the "adjusted" EWMA (weights
    normalise to 1).

    It deviates from v1's ``_ewma_volatility``, which called
    ``returns.ewm(alpha=1-lambda, adjust=False).var()``. pandas' adjust=False
    is the seeded recursion ``sigma2_t = lambda*sigma2_{t-1} + (1-lambda)*r_t^2``
    with ``sigma2_0 = r_0^2``; its weights sum to ``1 - lambda^n`` (not 1) and
    the seed contributes ``lambda^n * r_0^2``, so as ``lambda_`` -> 1 over a
    finite window it approaches the seed, *not* the equally-weighted mean --
    contradicting the work order's required outcome. The normalised form is
    what makes that property hold and is a standard EWMA definition.
    """
    if not 0.0 < lambda_ < 1.0:
        raise Unavailable(f"lambda_ must be in (0, 1); got {lambda_}")
    prices = np.asarray(prices, dtype=float)
    _check_window(len(prices), window)
    _check_annualisation(annualisation)
    returns = _log_returns(prices)
    if len(returns) < window:
        raise Unavailable(
            f"need >= {window} returns for window={window} "
            f"(>= {window + 1} prices), got {len(returns)}"
        )
    recent = returns[-window:]
    r2 = recent ** 2
    n = len(r2)
    weights = lambda_ ** np.arange(n - 1, -1, -1, dtype=float)
    var = float(np.sum(weights * r2) / np.sum(weights))
    if not np.isfinite(var) or var <= 0.0:
        raise Unavailable("zero or undefined EWMA variance")
    return float(np.sqrt(var) * np.sqrt(annualisation))


def _validate_bars(bars: Sequence[Bar], window: int) -> None:
    _check_window(len(bars), window)
    for b in bars:
        if b.high < b.low:
            raise Unavailable(f"bar with high < low: {b}")
        if not (b.low <= b.close <= b.high):
            raise Unavailable(f"close outside [low, high]: {b}")
        if not (b.low <= b.open <= b.high):
            raise Unavailable(f"open outside [low, high]: {b}")
        if min(b.open, b.high, b.low, b.close) <= 0:
            raise Unavailable(f"non-positive OHLC value; log undefined: {b}")


def parkinson(
    bars: Sequence[Bar], *, window: int, annualisation: float
) -> float:
    """Annualised Parkinson (1980) high-low volatility."""
    _validate_bars(bars, window)
    _check_annualisation(annualisation)
    recent = list(bars[-window:])
    coeff = 1.0 / (4.0 * np.log(2.0))
    variances = np.array([coeff * np.log(b.high / b.low) ** 2 for b in recent])
    var = float(variances.mean())
    if var <= 0.0:
        raise Unavailable("zero variance: every bar has high == low")
    return float(np.sqrt(var * annualisation))


def garman_klass(
    bars: Sequence[Bar], *, window: int, annualisation: float
) -> float:
    """Annualised Garman-Klass (1980) OHLC volatility."""
    _validate_bars(bars, window)
    _check_annualisation(annualisation)
    recent = list(bars[-window:])
    c = 2.0 * np.log(2.0) - 1.0
    variances = np.array(
        [
            0.5 * np.log(b.high / b.low) ** 2 - c * np.log(b.close / b.open) ** 2
            for b in recent
        ]
    )
    if (variances < 0).any():
        raise Unavailable("negative per-bar GK variance; corrupt OHLC")
    var = float(variances.mean())
    if var <= 0.0:
        raise Unavailable("zero variance: Garman-Klass undefined on this window")
    return float(np.sqrt(var * annualisation))


def rogers_satchell(
    bars: Sequence[Bar], *, window: int, annualisation: float
) -> float:
    """Annualised Rogers-Satchell (1991) OHLC volatility."""
    _validate_bars(bars, window)
    _check_annualisation(annualisation)
    recent = list(bars[-window:])
    variances = np.array(
        [
            np.log(b.high / b.close) * np.log(b.high / b.open)
            + np.log(b.low / b.close) * np.log(b.low / b.open)
            for b in recent
        ]
    )
    if (variances < 0).any():
        raise Unavailable("negative per-bar RS variance; corrupt OHLC")
    var = float(variances.mean())
    if var <= 0.0:
        raise Unavailable("zero variance: Rogers-Satchell undefined on this window")
    return float(np.sqrt(var * annualisation))


def volatility_of_volatility(
    volatilities: Sequence[float],
    *,
    window: int,
    annualisation: float,
    ddof: int = 0,
) -> float:
    """Annualised std of a series of volatility readings."""
    series = np.asarray(volatilities, dtype=float)
    _check_window(len(series), window)
    _check_annualisation(annualisation)
    if np.isnan(series).any():
        raise Unavailable("input contains NaN")
    if (series < 0).any():
        raise Unavailable("negative volatility reading")
    recent = series[-window:]
    if _is_effectively_constant(recent):
        raise Unavailable("zero variance in volatilities; vol-of-vol undefined")
    return float(np.std(recent, ddof=ddof) * np.sqrt(annualisation))
