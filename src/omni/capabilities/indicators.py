"""Technical indicators as pure capabilities.

Ported from v1 ``app/services/technical_indicators.py`` (454 lines). The
source is the cleanest remaining port in the census: pure list-in/list-out
arithmetic with no framework dependency. The ``calculate_*`` static methods
are ported here as standalone functions; every place v1 substituted a
default on missing or degenerate input is replaced with ``Unavailable``
(when the indicator is undefined everywhere) or ``None`` at the offending
index (when it is undefined there only).

Dropped (per work order)
------------------------
- ``get_from_cache`` / ``set_cache`` -- in-memory cache on the service
  object. Not portable, not wanted.
- The ``_calculate_*`` instance methods -- thin delegates to the
  ``calculate_*`` statics. One set is ported, not both.
- ``IndicatorType`` enum, ``calculate_indicators`` dispatcher and the
  ``technical_indicator_service`` singleton -- framework tangle belonging to
  the FastAPI layer, not a pure capability.

The None-vs-Unavailable distinction
------------------------------------
v1 returns ``List[Optional[float]]`` with ``None`` in the leading positions
where the window is not yet full: an SMA(20) over 100 prices returns 100
entries, the first 19 ``None``. This shape is preserved. ``None`` is an
honest statement that the indicator is undefined *at that index* (window not
yet full, or a per-index degeneracy such as a zero-range stochastic window).
``Unavailable`` is raised when the indicator is undefined *everywhere* --
the series is shorter than the period, the period is below 2, the input
contains NaN, or (for volume indicators) a volume is negative.

**Undefined-at-an-index is ``None``; undefined-everywhere is a refusal.**

Per-indicator decisions
-----------------------
- **RSI zero-loss.** When ``avg_loss == 0`` and ``avg_gain > 0`` (every bar
  in the window rose), RS -> +inf and RSI -> 100 is the genuine
  mathematical limit of the formula; v1 returns 100 here and this module
  keeps it. When ``avg_loss == 0`` *and* ``avg_gain == 0`` (a flat window),
  the ratio is 0/0 and RSI is undefined; v1 returns 100 here too, which
  fabricates a maximally-bullish reading on a series that did not move.
  This module returns ``None`` at that index instead.
- **Bollinger zero-variance.** A zero-variance window has std == 0, so
  upper == middle == lower == mean. This is mathematically well-defined --
  the band genuinely has zero width -- and returning it is truthful, not a
  fabrication. (Contrast with ``volatility.py``, where zero return
  variance means the volatility estimator itself is undefined and raises.)
  No raise; a zero-width band is returned.
- **Stochastic zero-range.** When ``period_high == period_low`` the %K
  formula ``(close - low) / (high - low)`` is 0/0. v1 returns 50 (a neutral
  default); this module returns ``None``.
- **ATR length.** v1 requires ``len(high) >= period`` but the first bar has
  no true range (no prior close), so ``period`` true ranges need
  ``period + 1`` bars. v1's off-by-one silently averages ``period - 1``
  values divided by ``period`` in the initial ATR. This module requires
  ``len >= period + 1`` and raises otherwise.

Bit-for-bit faithfulness
------------------------
On every path the arithmetic matches v1: the EMA multiplier
``2/(period+1)``, the SMA-seeded EMA recursion, Wilder's RSI smoothing
``avg := (avg*(period-1) + gain) / period``, Wilder's ATR smoothing, the
MACD signal-line construction via EMA of the defined MACD values, and
``np.std`` (ddof=0) for Bollinger. Floating-point operation order is
preserved.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from omni.ingest.protocol import Unavailable


def _as_float_array(seq: Sequence[float]) -> np.ndarray:
    return np.asarray(seq, dtype=float)


def _check_period(period: int) -> None:
    if period < 2:
        raise Unavailable(f"period must be >= 2, got {period}")


def _check_min_len(n: int, minimum: int) -> None:
    if n < minimum:
        raise Unavailable(f"need >= {minimum} observations, got {n}")


def _check_finite(*arrs: np.ndarray) -> None:
    for a in arrs:
        if not np.isfinite(a).all():
            raise Unavailable("input contains NaN or inf")


def _check_equal_len(*arrs: np.ndarray) -> None:
    n = len(arrs[0])
    for a in arrs[1:]:
        if len(a) != n:
            raise Unavailable("mismatched input lengths")


def sma(prices: Sequence[float], *, period: int) -> list[float | None]:
    """Simple moving average. ``None`` in the first ``period - 1`` positions."""
    arr = _as_float_array(prices)
    _check_period(period)
    _check_finite(arr)
    _check_min_len(len(arr), period)
    out: list[float | None] = [None] * (period - 1)
    for i in range(period - 1, len(arr)):
        out.append(float(sum(arr[i - period + 1 : i + 1]) / period))
    return out


def ema(prices: Sequence[float], *, period: int) -> list[float | None]:
    """Exponential moving average, seeded with the SMA of the first ``period``.

    ``None`` in the first ``period - 1`` positions. The recursion is
    ``ema_t = price_t * m + ema_{t-1} * (1 - m)`` with ``m = 2/(period+1)``.
    """
    arr = _as_float_array(prices)
    _check_period(period)
    _check_finite(arr)
    _check_min_len(len(arr), period)
    multiplier = 2.0 / (period + 1)
    out: list[float | None] = [None] * (period - 1)
    prev = float(sum(arr[:period]) / period)
    out.append(prev)
    for i in range(period, len(arr)):
        prev = (float(arr[i]) * multiplier) + (prev * (1.0 - multiplier))
        out.append(prev)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float | None:
    # avg_loss == 0 with real gains is the genuine RSI -> 100 limit (RS -> inf).
    # avg_loss == 0 with zero gains is 0/0: flat window, RSI undefined -> None.
    if avg_loss == 0.0:
        if avg_gain == 0.0:
            return None
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rsi(prices: Sequence[float], *, period: int) -> list[float | None]:
    """Relative Strength Index via Wilder's smoothing.

    ``None`` in the first ``period`` positions (RSI needs ``period`` deltas,
    hence ``period + 1`` prices). A flat window -- ``avg_gain`` and
    ``avg_loss`` both zero -- yields ``None`` at that index; a purely rising
    window yields 100 (the genuine limit).
    """
    arr = _as_float_array(prices)
    _check_period(period)
    _check_finite(arr)
    _check_min_len(len(arr), period + 1)
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    out: list[float | None] = [None] * period
    avg_gain = float(np.sum(gains[:period]) / period)
    avg_loss = float(np.sum(losses[:period]) / period)
    out.append(_rsi_value(avg_gain, avg_loss))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period
        out.append(_rsi_value(avg_gain, avg_loss))
    return out


def macd(
    prices: Sequence[float],
    *,
    fast_period: int,
    slow_period: int,
    signal_period: int,
) -> dict[str, list[float | None]]:
    """MACD line, signal line, and histogram.

    The MACD line is ``ema(fast) - ema(slow)``; the signal line is an EMA of
    the MACD line over ``signal_period``; the histogram is their difference.
    Leading ``None`` entries align with wherever either parent is undefined.
    """
    arr = _as_float_array(prices)
    _check_period(fast_period)
    _check_period(slow_period)
    _check_period(signal_period)
    _check_finite(arr)
    _check_min_len(len(arr), slow_period)
    ema_fast = ema(arr, period=fast_period)
    ema_slow = ema(arr, period=slow_period)
    macd_line: list[float | None] = []
    for i in range(len(arr)):
        ef = ema_fast[i]
        es = ema_slow[i]
        if ef is None or es is None:
            macd_line.append(None)
        else:
            macd_line.append(ef - es)
    macd_values = [v for v in macd_line if v is not None]
    if len(macd_values) >= signal_period:
        signal_values = ema(macd_values, period=signal_period)
        signal_line: list[float | None] = []
        sig_idx = 0
        for v in macd_line:
            if v is None:
                signal_line.append(None)
            elif sig_idx < len(signal_values):
                signal_line.append(signal_values[sig_idx])
                sig_idx += 1
            else:
                signal_line.append(None)
    else:
        signal_line = [None] * len(arr)
    histogram: list[float | None] = []
    for i in range(len(arr)):
        m = macd_line[i]
        s = signal_line[i]
        if m is not None and s is not None:
            histogram.append(m - s)
        else:
            histogram.append(None)
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def bollinger_bands(
    prices: Sequence[float], *, period: int, num_std: float = 2.0
) -> dict[str, list[float | None]]:
    """Bollinger bands. Population std (``ddof=0``), matching v1.

    A zero-variance window collapses to a zero-width band
    (upper == middle == lower); std == 0 is a valid, honest width, so no
    raise. See the module docstring for the contrast with volatility.
    """
    arr = _as_float_array(prices)
    _check_period(period)
    _check_finite(arr)
    _check_min_len(len(arr), period)
    upper: list[float | None] = [None] * len(arr)
    middle: list[float | None] = [None] * len(arr)
    lower: list[float | None] = [None] * len(arr)
    for i in range(period - 1, len(arr)):
        window = arr[i - period + 1 : i + 1]
        mid = float(sum(window) / period)
        std = float(np.std(window))
        middle[i] = mid
        upper[i] = mid + num_std * std
        lower[i] = mid - num_std * std
    return {"upper": upper, "middle": middle, "lower": lower}


def stochastic(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    *,
    k_period: int,
    d_period: int = 3,
) -> dict[str, list[float | None]]:
    """Stochastic oscillator %K and %D (SMA of %K).

    %K is ``(close - low) / (high - low) * 100`` over the trailing
    ``k_period`` bars. A zero-range window (``high == low``) is 0/0; v1
    returns a neutral 50 there, this module returns ``None``.

    %D is the SMA of the trailing ``d_period`` %K values taken **by index**.
    A %D window that contains any ``None`` — warmup prefix or a mid-series
    zero-range gap — yields ``None`` at that index. The earlier
    filter-then-realign approach silently averaged non-contiguous %K values
    across gaps; the index-based window does not.
    """
    hi = _as_float_array(high)
    lo = _as_float_array(low)
    cl = _as_float_array(close)
    _check_period(k_period)
    _check_period(d_period)
    _check_finite(hi, lo, cl)
    _check_equal_len(hi, lo, cl)
    _check_min_len(len(hi), k_period)
    k_values: list[float | None] = []
    for i in range(len(cl)):
        if i < k_period - 1:
            k_values.append(None)
            continue
        window_hi = float(np.max(hi[i - k_period + 1 : i + 1]))
        window_lo = float(np.min(lo[i - k_period + 1 : i + 1]))
        if window_hi == window_lo:
            k_values.append(None)
        else:
            k_values.append(
                ((float(cl[i]) - window_lo) / (window_hi - window_lo)) * 100.0
            )
    d_values: list[float | None] = [None] * len(cl)
    for i in range(d_period - 1, len(cl)):
        window = k_values[i - d_period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        d_values[i] = sum(window) / d_period
    return {"k": k_values, "d": d_values}


def atr(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    *,
    period: int,
) -> list[float | None]:
    """Average True Range via Wilder's smoothing.

    The first bar has no true range (no prior close), so ``period`` true
    ranges require ``period + 1`` bars; v1 required only ``period`` and
    silently under-filled the initial average. This module raises on
    ``len < period + 1``.
    """
    hi = _as_float_array(high)
    lo = _as_float_array(low)
    cl = _as_float_array(close)
    _check_period(period)
    _check_finite(hi, lo, cl)
    _check_equal_len(hi, lo, cl)
    _check_min_len(len(hi), period + 1)
    tr = [0.0]
    for i in range(1, len(hi)):
        tr.append(
            max(
                float(hi[i] - lo[i]),
                abs(float(hi[i] - cl[i - 1])),
                abs(float(lo[i] - cl[i - 1])),
            )
        )
    out: list[float | None] = [None] * period
    prev = sum(tr[1 : period + 1]) / period
    out.append(float(prev))
    for i in range(period + 1, len(tr)):
        prev = ((prev * (period - 1)) + tr[i]) / period
        out.append(float(prev))
    return out


def vwap(
    prices: Sequence[float],
    volumes: Sequence[float],
    *,
    high: Sequence[float] | None = None,
    low: Sequence[float] | None = None,
) -> list[float | None]:
    """Cumulative volume-weighted average price using typical price.

    Typical price is ``(high + low + close) / 3``; when ``high``/``low`` are
    omitted the typical price reduces to ``close``, so VWAP with uniform
    volume equals the running simple mean of price. The cumulative is
    point-in-time from the first bar (no window, no leading ``None`` unless
    cumulative volume is zero).
    """
    px = _as_float_array(prices)
    vol = _as_float_array(volumes)
    _check_finite(px, vol)
    _check_equal_len(px, vol)
    if (vol < 0).any():
        raise Unavailable("negative volume")
    hi = px if high is None else _as_float_array(high)
    lo = px if low is None else _as_float_array(low)
    if high is not None:
        _check_equal_len(px, hi)
    if low is not None:
        _check_equal_len(px, lo)
    out: list[float | None] = []
    cum_pv = 0.0
    cum_vol = 0.0
    for i in range(len(px)):
        typical = (float(hi[i]) + float(lo[i]) + float(px[i])) / 3.0
        cum_pv += typical * float(vol[i])
        cum_vol += float(vol[i])
        if cum_vol > 0.0:
            out.append(cum_pv / cum_vol)
        else:
            out.append(None)
    return out


def obv(prices: Sequence[float], volumes: Sequence[float]) -> list[float]:
    """On-balance volume.

    Cumulative: ``+volume`` when price rose, ``-volume`` when it fell, flat
    when unchanged. The final value equals the signed sum of volumes weighted
    by price direction. ``obv[0]`` is always ``0.0``.
    """
    px = _as_float_array(prices)
    vol = _as_float_array(volumes)
    _check_finite(px, vol)
    _check_equal_len(px, vol)
    _check_min_len(len(px), 2)
    if (vol < 0).any():
        raise Unavailable("negative volume")
    out: list[float] = [0.0]
    for i in range(1, len(px)):
        if px[i] > px[i - 1]:
            out.append(out[-1] + float(vol[i]))
        elif px[i] < px[i - 1]:
            out.append(out[-1] - float(vol[i]))
        else:
            out.append(out[-1])
    return out
