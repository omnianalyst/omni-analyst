"""Statistical market-manipulation detection over real OHLCV data.

Three detectors that are defensible from price and volume alone, per
``_census/work-orders/K-02.md``:

- ``volume_anomaly``  - latest bar's volume as a percentile / z-score of the
  symbol's own trailing distribution.
- ``wash_trading``    - recent volume with disproportionately small price
  range, ranked against the trailing distribution of the same ratio.
- ``pump_and_dump``   - recent sharp rise on anomalous volume followed by a
  partial reversal, each component measured against the symbol's own
  trailing distribution.

Spoofing and layering need order-book depth (level-2). This system has no
level-2 feed wired, so they are NOT detected here - callers receive an
honest "unsupported" finding naming the missing input (reported with
``status_code: 501`` in the response) rather than a fabricated score.

Design rules enforced here (read these before changing anything):

- **No fixed thresholds.** Every score is a percentile rank or z-score of
  the statistic against the symbol's *own* trailing distribution. A
  detection at the 99th percentile means "this bar's volume exceeded 99%
  of the symbol's own recent history", not "volume crossed a hardcoded
  line".
- **Every finding is self-auditing.** The ``evidence`` dict names the
  statistic, the window, the baseline sample size, the observed value and
  the baseline statistics. A manipulation flag with no evidence is an
  accusation; this structure makes the accusation checkable.
- **Confidence is conservative.** Where multiple signals must combine
  (pump-and-dump), the reported confidence is the *weakest* of them, not
  an average.
- **No fabrication on missing data.** If OHLCV is missing required
  columns or is too short to build a baseline, we raise - never impute,
  never substitute a default.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class FraudDataUnavailable(RuntimeError):
    """Real OHLCV data is missing or malformed - cannot compute any detection."""


class InsufficientData(ValueError):
    """Data is present but too short to build a trailing baseline."""


DEFAULT_BASELINE = 60
DEFAULT_EVENT = 12
DEFAULT_REVERSAL = 3
MIN_BARS = DEFAULT_BASELINE + DEFAULT_EVENT


@dataclass
class Finding:
    """One detector's result. Fields chosen so the payload is self-auditing.

    ``confidence`` is in ``[0.0, 1.0]`` and is always derived from a
    percentile rank or ratio of the symbol's own trailing distribution -
    never a hardcoded score.
    """

    pattern: str
    detected: bool
    confidence: float
    evidence: Dict[str, Any] = field(default_factory=dict)
    note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _percentile_rank(value: float, sample: Sequence[float]) -> float:
    """Fraction of ``sample`` strictly below ``value`` (0..1), or ``nan``.

    Non-parametric: makes no distributional assumption about returns or
    volume. This is the workhorse that replaces fixed thresholds.
    """
    arr = np.asarray(sample, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return float("nan")
    return float(np.mean(arr < value))


def _zscore(value: float, sample: Sequence[float]) -> float:
    arr = np.asarray(sample, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return float("nan")
    mu = float(np.mean(arr))
    sd = float(np.std(arr, ddof=1))
    if sd == 0:
        return 0.0
    return float((value - mu) / sd)


def _round_or_none(x: float) -> Optional[float]:
    if x is None or math.isnan(x):
        return None
    return round(float(x), 4)


class ManipulationAnalyzer:
    """Stateless manipulation detection over a real OHLCV frame.

    The class holds only window configuration; all heavy lifting is in the
    methods, which take a DataFrame so callers (and tests) can feed
    controlled data.
    """

    def __init__(
        self,
        baseline: int = DEFAULT_BASELINE,
        event: int = DEFAULT_EVENT,
        reversal: int = DEFAULT_REVERSAL,
    ) -> None:
        if baseline < 20 or event < 2 or reversal < 1 or event <= reversal:
            raise ValueError(
                "Require baseline>=20, event>=2, reversal>=1, event>reversal"
            )
        self.baseline = baseline
        self.event = event
        self.reversal = reversal

    # ------------------------------------------------------------------ prep

    def _prepare(self, ohlcv: pd.DataFrame) -> pd.DataFrame:
        """Coerce the data fetcher's price list into a clean OHLCV frame.

        Raises ``FraudDataUnavailable`` if the required columns can't be
        obtained - never substitutes a default. Rows missing ``close`` or
        ``volume`` are dropped; we never impute.
        """
        if ohlcv is None or len(ohlcv) == 0:
            raise FraudDataUnavailable(
                "No OHLCV rows returned by the data layer. Manipulation "
                "detection needs real price and volume bars."
            )

        df = ohlcv.copy()
        df = df.replace([np.inf, -np.inf], np.nan)

        if "close" not in df.columns:
            raise FraudDataUnavailable(
                "OHLCV response is missing a 'close' column; cannot compute "
                "any price-based detection."
            )
        if "volume" not in df.columns:
            raise FraudDataUnavailable(
                "OHLCV response is missing a 'volume' column; manipulation "
                "detection from price+volume cannot proceed without volume."
            )

        df = df.sort_index()
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df = df.dropna(subset=["close", "volume"])
        df = df[df["close"] > 0]
        df = df[df["volume"] >= 0]

        min_bars = self.baseline + self.event
        if len(df) < min_bars:
            raise InsufficientData(
                f"Need at least {min_bars} clean OHLCV bars to build a "
                f"{self.baseline}-bar trailing baseline plus a "
                f"{self.event}-bar event window; got {len(df)}."
            )
        return df

    # ----------------------------------------------------------- detectors

    def detect_volume_anomaly(self, ohlcv: pd.DataFrame) -> Finding:
        """Latest bar's log(volume) vs the symbol's own trailing distribution."""
        df = self._prepare(ohlcv)
        return self._volume_anomaly(df)

    def _volume_anomaly(self, df: pd.DataFrame) -> Finding:
        log_vol = np.log(df["volume"].clip(lower=1.0))
        latest = float(log_vol.iloc[-1])
        prior = log_vol.iloc[:-1].to_numpy()
        prior = prior[np.isfinite(prior)]

        used = prior.size
        if used > self.baseline:
            prior = prior[-self.baseline:]
            used = self.baseline

        pct = _percentile_rank(latest, prior)
        z = _zscore(latest, prior)
        confidence = 0.0 if math.isnan(pct) else pct
        detected = confidence >= 0.95 and (not math.isnan(z)) and z >= 3.0

        return Finding(
            pattern="VOLUME_ANOMALY",
            detected=detected,
            confidence=round(confidence, 4),
            evidence={
                "statistic": "log(volume)",
                "observed_log_volume": round(latest, 4),
                "observed_raw_volume": float(df["volume"].iloc[-1]),
                "baseline_mean_log_volume": _round_or_none(
                    float(np.mean(prior)) if prior.size else None
                ),
                "baseline_std_log_volume": _round_or_none(
                    float(np.std(prior, ddof=1)) if prior.size > 1 else None
                ),
                "baseline_sample_size": int(used),
                "baseline_window_bars": self.baseline,
                "z_score": _round_or_none(z),
                "percentile_rank": _round_or_none(pct),
            },
            note=(
                "Latest bar's log(volume) ranked as a percentile of the "
                "symbol's own trailing distribution. Detection requires "
                ">=95th percentile AND z-score >= 3.0 (both, conservatively)."
            ),
        )

    def detect_wash_trading(self, ohlcv: pd.DataFrame) -> Finding:
        """Volume-to-range ratio for the recent window vs trailing distribution.

        High volume with a tiny price range is the wash signature - and we
        can only rank it against the symbol's own history of this ratio.
        Most meaningful on crypto pairs; without order-flow data this is a
        necessary-conditions signal, not identification of self-trading.
        """
        df = self._prepare(ohlcv)
        return self._wash_trading(df)

    def _wash_trading(self, df: pd.DataFrame) -> Finding:
        event = self.event
        recent_ratio, ratios = self._volume_range_ratios(df, event)

        if ratios.size < 5:
            raise InsufficientData(
                "Not enough trailing event-sized windows to rank the "
                "volume/range ratio; need a longer price history."
            )

        pct = _percentile_rank(recent_ratio, ratios)
        z = _zscore(recent_ratio, ratios)
        confidence = 0.0 if math.isnan(pct) else pct
        detected = confidence >= 0.99 and (not math.isnan(z)) and z >= 3.0

        return Finding(
            pattern="WASH_TRADING",
            detected=detected,
            confidence=round(confidence, 4),
            evidence={
                "statistic": "window_volume / window_range_pct",
                "window_bars": event,
                "observed": round(recent_ratio, 4),
                "baseline_sample_size": int(ratios.size),
                "baseline_median": round(float(np.median(ratios)), 4),
                "baseline_p95": round(float(np.quantile(ratios, 0.95)), 4),
                "percentile_rank": _round_or_none(pct),
                "z_score": _round_or_none(z),
            },
            note=(
                "High volume with disproportionately small price range, "
                "ranked against the symbol's own trailing windows. A "
                "necessary-conditions signal absent order-flow data."
            ),
        )

    def _volume_range_ratios(
        self, df: pd.DataFrame, window: int
    ) -> tuple[float, np.ndarray]:
        """Return (latest_window_ratio, array_of_baseline_window_ratios).

        Uses ``high``/``low`` when available (most providers supply them);
        falls back to ``close`` extrema otherwise. Never fabricates a range.
        """
        has_hl = "high" in df.columns and "low" in df.columns

        def block_ratio(block: pd.DataFrame) -> float:
            vol = float(block["volume"].sum())
            if has_hl:
                lo = float(pd.to_numeric(block["low"], errors="coerce").min())
                hi = float(pd.to_numeric(block["high"], errors="coerce").max())
            else:
                lo = float(block["close"].min())
                hi = float(block["close"].max())
            mid = float(block["close"].mean())
            if not np.isfinite(mid) or mid <= 0:
                return float("nan")
            range_pct = (hi - lo) / mid
            if not np.isfinite(range_pct) or range_pct <= 0:
                # Genuine flat market (range == 0). Use a tiny epsilon so
                # the ratio is large but finite - we are not inventing a
                # range, only avoiding a divide-by-zero.
                range_pct = 1e-9
            return vol / range_pct

        recent_ratio = block_ratio(df.iloc[-window:])

        ratios = []
        n = len(df)
        for start in range(0, n - window + 1):
            block = df.iloc[start : start + window]
            r = block_ratio(block)
            if np.isfinite(r):
                ratios.append(r)

        # The most recent window is the last one emitted; exclude it from
        # the baseline so we never compare a value to itself.
        if ratios:
            ratios = ratios[:-1]
        return recent_ratio, np.asarray(ratios, dtype=float)

    def detect_pump_and_dump(self, ohlcv: pd.DataFrame) -> Finding:
        """Recent sharp rise on heavy volume followed by partial reversal.

        Three independent signals, each ranked against the symbol's own
        trailing distribution; reported confidence is the *weakest* of the
        three (conservative). A pump-without-reversal does NOT fire - the
        dump half is what makes it manipulation rather than a rally.
        """
        df = self._prepare(ohlcv)
        return self._pump_and_dump(df)

    def _pump_and_dump(self, df: pd.DataFrame) -> Finding:
        event = self.event
        rev = self.reversal
        pump_len = event - rev
        if pump_len < 2:
            raise ValueError("event - reversal must be >= 2 bars for the pump phase")

        # The ``event`` bars split into a pump phase (first ``pump_len`` bars)
        # and a reversal phase (last ``rev`` bars). Pump return is measured
        # ONLY over the pump phase so the dump does not cancel the signal.
        closes = df["close"].to_numpy()
        pump_start = float(closes[-event])
        peak = float(closes[-rev - 1])
        bottom = float(closes[-1])
        if pump_start <= 0 or peak <= 0:
            raise FraudDataUnavailable("Invalid zero/negative close in pump window.")
        pump_return = peak / pump_start - 1.0
        reversal = bottom / peak - 1.0 if peak > 0 else 0.0

        # Baseline: same pump-phase return measured across every prior
        # event-sized window. Each window's pump return is close at the
        # last pump-phase bar divided by close at the first pump-phase bar.
        baseline_returns = []
        for start in range(0, len(closes) - event):
            e0 = float(closes[start])
            e1 = float(closes[start + pump_len - 1])
            if e0 > 0:
                baseline_returns.append(e1 / e0 - 1.0)
        baseline_returns = np.asarray(baseline_returns, dtype=float)

        if baseline_returns.size < 5:
            raise InsufficientData(
                "Not enough trailing event-sized windows to rank the pump."
            )

        ret_pct = _percentile_rank(pump_return, baseline_returns)
        ret_z = _zscore(pump_return, baseline_returns)

        # Volume over the pump phase only, vs trailing single-bar log-volume.
        log_vol = np.log(df["volume"].clip(lower=1.0))
        pump_log_vol_mean = float(log_vol.iloc[-event:-rev].mean())
        prior_log_vol = log_vol.iloc[:-event].to_numpy()
        prior_log_vol = prior_log_vol[np.isfinite(prior_log_vol)]
        vol_pct = _percentile_rank(pump_log_vol_mean, prior_log_vol)

        has_pump = (not math.isnan(ret_pct)) and ret_pct >= 0.95 and pump_return > 0
        has_volume = (not math.isnan(vol_pct)) and vol_pct >= 0.90
        has_reversal = reversal < 0

        confidence = 0.0
        reversal_ratio = 0.0
        if has_pump and has_volume and has_reversal and abs(pump_return) > 0:
            reversal_ratio = abs(reversal) / abs(pump_return)
            # Weakest of the three (conservative): pump percentile, volume
            # percentile, and how much of the pump was given back.
            confidence = min(ret_pct, vol_pct, reversal_ratio)

        detected = confidence >= 0.50

        return Finding(
            pattern="PUMP_AND_DUMP",
            detected=detected,
            confidence=round(confidence, 4),
            evidence={
                "statistic": "pump-phase return + pump-phase volume + reversal ratio",
                "pump_phase_bars": pump_len,
                "reversal_phase_bars": rev,
                "event_window_bars": event,
                "pump_return": round(pump_return, 4),
                "pump_return_percentile": _round_or_none(ret_pct),
                "pump_return_z": _round_or_none(ret_z),
                "pump_volume_percentile": _round_or_none(vol_pct),
                "reversal_return": round(reversal, 4),
                "reversal_to_pump_ratio": round(reversal_ratio, 4),
                "baseline_event_windows": int(baseline_returns.size),
                "baseline_pump_return_p95": round(
                    float(np.quantile(baseline_returns, 0.95)), 4
                ),
            },
            note=(
                "Recent sharp rise on heavy volume followed by a partial "
                "reversal, each ranked against the symbol's own trailing "
                "distribution. Pump and reversal are measured over separate "
                "sub-windows so the dump does not cancel the pump signal. "
                "Reported confidence is the weakest of the three (pump "
                "percentile, volume percentile, reversal ratio). A pump "
                "with no reversal does not fire - the dump is what makes "
                "it manipulation."
            ),
        )

    # ----------------------------------------------------------- composite

    def analyze(self, ohlcv: pd.DataFrame) -> Dict[str, Any]:
        """Run every supported detector plus the named-unsupported ones.

        Spoofing and layering cannot be run (no level-2 feed). They are
        reported under ``unsupported`` with ``status_code: 501`` and a
        reason naming the missing input, per ``_census/FIXING.md``.
        """
        df = self._prepare(ohlcv)
        findings = {
            "VOLUME_ANOMALY": self._volume_anomaly(df).to_dict(),
            "WASH_TRADING": self._wash_trading(df).to_dict(),
            "PUMP_AND_DUMP": self._pump_and_dump(df).to_dict(),
        }
        unsupported = {
            "SPOOFING": self._unsupported_level2("SPOOFING"),
            "LAYERING": self._unsupported_level2("LAYERING"),
        }
        detected = [k for k, v in findings.items() if v["detected"]]
        # Conservative composite: the strongest detector's confidence.
        composite_confidence = max(
            (v["confidence"] for v in findings.values()), default=0.0
        )
        return {
            "findings": findings,
            "unsupported": unsupported,
            "detected_patterns": detected,
            "composite_confidence": round(composite_confidence, 4),
        }

    @staticmethod
    def _unsupported_level2(pattern: str) -> Dict[str, Any]:
        return {
            "pattern": pattern,
            "status_code": 501,
            "reason": (
                f"{pattern} detection requires order-book depth (level-2). "
                "No level-2 feed is wired into this system, so this pattern "
                "is not detected. Do not interpret the absence of a flag "
                "as absence of manipulation."
            ),
        }
