"""Triple-barrier labeling + CUSUM event sampling (AFML, López de Prado §3).

Given a price series and a set of *event* timestamps, place three barriers
around each event:

- an **upper** barrier at ``+upper_mult · σ_t`` (profit-take),
- a **lower** barrier at ``−lower_mult · σ_t`` (stop),
- a **vertical** barrier ``max_holding`` bars ahead (time limit).

The label is ``+1`` if the upper barrier is touched first, ``−1`` if the lower
is touched first, and (for a vertical-barrier touch) ``0`` or the sign of the
realized return depending on ``zero_on_vertical``. Barriers are volatility-scaled
using a daily EWMA of returns, so a fixed multiple means a fixed number of
standard deviations regardless of the regime.

Events are sampled with a **symmetric CUSUM filter** rather than a fixed clock:
it triggers only when the cumulative (de-meaned) return run breaches a threshold
``h``, which concentrates labels around genuine moves and avoids labeling noise.

All functions are pure (numpy/pandas), causal, and deterministic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Volatility estimate
# --------------------------------------------------------------------------- #

def ewma_volatility(prices: pd.Series, span: int = 100) -> pd.Series:
    """Daily EWMA volatility of simple returns.

    Returns a series aligned to ``prices`` (first value is NaN — no prior
    return). The estimate at ``t`` uses only returns up to and including ``t``,
    so it is causal as a per-event scale.
    """
    if not isinstance(prices, pd.Series):
        raise TypeError("prices must be a pandas Series")
    rets = prices.pct_change()
    return rets.ewm(span=span, min_periods=1).std()


# --------------------------------------------------------------------------- #
# CUSUM event filter
# --------------------------------------------------------------------------- #

def cusum_events(prices: pd.Series, threshold: float) -> pd.DatetimeIndex:
    """Symmetric CUSUM filter on log-returns.

    Accumulates positive and negative log-return runs; when either crosses
    ``threshold`` an event is emitted and that accumulator resets. ``threshold``
    is in log-return units (e.g. 0.02 for ~2% cumulative runs). A constant or
    per-bar series may be passed for ``threshold``.

    Returns the DatetimeIndex of event timestamps.
    """
    if threshold is None:
        raise ValueError("threshold is required")
    log_ret = np.log(prices).diff().dropna()
    if isinstance(threshold, (pd.Series,)):
        thr = threshold.reindex(log_ret.index).ffill()
    else:
        if threshold <= 0:
            raise ValueError("threshold must be positive")
        thr = pd.Series(float(threshold), index=log_ret.index)

    s_pos = 0.0
    s_neg = 0.0
    events = []
    for ts, r in log_ret.items():
        h = float(thr.loc[ts])
        s_pos = max(0.0, s_pos + r)
        s_neg = min(0.0, s_neg + r)
        if s_pos >= h:
            s_pos = 0.0
            events.append(ts)
        elif s_neg <= -h:
            s_neg = 0.0
            events.append(ts)
    return pd.DatetimeIndex(events)


# --------------------------------------------------------------------------- #
# Triple-barrier labels
# --------------------------------------------------------------------------- #

@dataclass
class TripleBarrierLabels:
    """Result of triple-barrier labeling, one row per event.

    Attributes
    ----------
    labels : DataFrame indexed by event time with columns:
        ``label`` (+1/-1/0), ``ret`` (realized return to the touch), ``touch_time``
        (when a barrier was hit / vertical reached), ``barrier`` (which barrier:
        'upper'/'lower'/'vertical'), and ``t1`` (the vertical-barrier timestamp,
        i.e. the label's outcome-window end — feed this to PurgedKFold).
    """

    labels: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def t1(self) -> pd.Series:
        """Label-end times indexed by event start (for PurgedKFold)."""
        if self.labels.empty:
            return pd.Series(dtype="datetime64[ns]")
        return self.labels["touch_time"]

    def to_frame(self) -> pd.DataFrame:
        return self.labels


def triple_barrier_labels(
    prices: pd.Series,
    events: Sequence[pd.Timestamp],
    *,
    max_holding: int,
    upper_mult: float = 1.0,
    lower_mult: float = 1.0,
    volatility: pd.Series | None = None,
    vol_span: int = 100,
    side: pd.Series | None = None,
    zero_on_vertical: bool = True,
    min_ret: float = 0.0,
) -> TripleBarrierLabels:
    """Apply the triple-barrier method to ``events`` on ``prices``.

    Parameters
    ----------
    prices : the (adjusted) price series, DatetimeIndex, ascending.
    events : event timestamps (subset of ``prices.index``), e.g. from
        :func:`cusum_events`.
    max_holding : vertical-barrier horizon in bars.
    upper_mult, lower_mult : barrier widths in units of the volatility estimate.
        Set ``upper_mult`` or ``lower_mult`` to ``0`` to disable that horizontal
        barrier (only the vertical applies on that side).
    volatility : optional precomputed per-bar volatility; defaults to an EWMA.
    side : optional ±1 series giving the *primary model's* trade direction per
        event. When given, the barriers are oriented to the side (the "upper"
        barrier becomes the profit-take in the side's favour), and the label
        becomes meta-label-ready: ``1`` if the bet won (profit barrier touched
        first or vertical with side·ret > min_ret), else ``0``.
    zero_on_vertical : if True (and no ``side``), a vertical-barrier touch gets
        label 0; if False it gets ``sign(ret)``.
    min_ret : when ``side`` is set, the minimum side-adjusted return to count a
        vertical touch as a win.

    Returns
    -------
    TripleBarrierLabels
    """
    if max_holding <= 0:
        raise ValueError("max_holding must be a positive integer")
    if not isinstance(prices, pd.Series):
        raise TypeError("prices must be a pandas Series")

    prices = prices.sort_index()
    if volatility is None:
        volatility = ewma_volatility(prices, span=vol_span)
    volatility = volatility.reindex(prices.index).ffill()

    idx = prices.index
    pos_of = {ts: i for i, ts in enumerate(idx)}

    rows = []
    for ev in events:
        if ev not in pos_of:
            continue
        i0 = pos_of[ev]
        i1 = min(i0 + max_holding, len(idx) - 1)
        if i1 <= i0:
            continue
        p0 = float(prices.iloc[i0])
        sigma = float(volatility.iloc[i0])
        if not np.isfinite(sigma) or sigma <= 0 or not np.isfinite(p0) or p0 <= 0:
            continue

        ev_side = 1.0
        if side is not None and ev in side.index:
            ev_side = float(side.loc[ev])
            if ev_side == 0:
                continue

        up = upper_mult * sigma
        dn = lower_mult * sigma

        window = prices.iloc[i0 + 1 : i1 + 1]
        rets = window / p0 - 1.0
        # Orient returns to the side so "upper" is always the profit direction.
        path = ev_side * rets

        touch_time = idx[i1]
        barrier = "vertical"
        for ts, pr in path.items():
            if up > 0 and pr >= up:
                touch_time = ts
                barrier = "upper"
                break
            if dn > 0 and pr <= -dn:
                touch_time = ts
                barrier = "lower"
                break

        realized = float(prices.loc[touch_time] / p0 - 1.0)
        signed_ret = ev_side * realized

        if side is not None:
            # Meta-label: did the primary's bet win?
            if barrier == "upper":
                label = 1
            elif barrier == "lower":
                label = 0
            else:
                label = 1 if signed_ret > min_ret else 0
        else:
            if barrier == "upper":
                label = 1
            elif barrier == "lower":
                label = -1
            else:
                label = 0 if zero_on_vertical else int(np.sign(realized))

        rows.append(
            {
                "event_time": ev,
                "label": int(label),
                "ret": realized,
                "side": ev_side,
                "touch_time": touch_time,
                "barrier": barrier,
            }
        )

    if not rows:
        return TripleBarrierLabels(pd.DataFrame(columns=["label", "ret", "side", "touch_time", "barrier"]))

    df = pd.DataFrame(rows).set_index("event_time").sort_index()
    return TripleBarrierLabels(df)
