"""K-02 tests: statistical manipulation detection over real OHLCV.

These tests target the new ``ManipulationAnalyzer`` (the endpoint is a
thin pass-through). Honest-data rules they enforce:

- Detection fires on injected patterns and refuses to fire on a
  pump-without-reversal (the dump half is what makes it manipulation).
- Every finding carries an ``evidence`` block naming the statistic,
  window, baseline sample size and observed value.
- ``spoofing`` / ``layering`` are honestly reported as 501 - no
  fabricated detection.
- Missing or too-short OHLCV raises, never imputes a default.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from omni.detect import (
    FraudDataUnavailable,
    InsufficientData,
    ManipulationAnalyzer,
    MIN_BARS,
)


# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def rng() -> np.random.Generator:
    return np.random.default_rng(seed=42)


def _flat_market(n: int = 200, rng: np.random.Generator | None = None) -> pd.DataFrame:
    """n bars of low-volatility, normal-volume price action - clean baseline.

    ``high``/``low`` track ``close`` so OHLC is internally coherent (the
    wash-trading detector uses high/low when present).
    """
    rng = rng or np.random.default_rng(0)
    close = 100.0 + np.cumsum(rng.normal(0, 0.10, n))
    half_spread = rng.uniform(0.05, 0.25, n)
    return pd.DataFrame(
        {
            "close": close,
            "volume": rng.uniform(900, 1100, n),
            "high": close + half_spread,
            "low": close - half_spread,
        }
    )


def _with_volume_spike(df: pd.DataFrame, spike_multiple: float = 20.0) -> pd.DataFrame:
    out = df.copy()
    out.iloc[-1, out.columns.get_loc("volume")] *= spike_multiple
    return out


def _with_wash_segment(df: pd.DataFrame, n_bars: int = 12) -> pd.DataFrame:
    """Stamp the last ``n_bars`` with high volume but flat prices."""
    out = df.copy()
    base = float(out["close"].iloc[-n_bars])
    new_close = out["close"].copy()
    new_close.iloc[-n_bars:] = base
    out["close"] = new_close
    out["high"].iloc[-n_bars:] = base + 0.001
    out["low"].iloc[-n_bars:] = base - 0.001
    out.iloc[-n_bars:, out.columns.get_loc("volume")] = 1_000_000.0
    return out


def _with_pump_and_dump(
    df: pd.DataFrame, pump_bars: int = 9, reversal_bars: int = 3
) -> pd.DataFrame:
    """Append a textbook pump (rise + heavy volume) and dump (reversal)."""
    out = df.copy()
    start_idx = -(pump_bars + reversal_bars)
    base = float(out["close"].iloc[start_idx])
    peak = base * 1.40
    pump_path = np.linspace(base, peak, pump_bars)
    dump_path = [peak * 0.95, peak * 0.78, peak * 0.60]
    new_close = out["close"].copy()
    new_close.iloc[start_idx : start_idx + pump_bars] = pump_path
    new_close.iloc[start_idx + pump_bars :] = dump_path
    out["close"] = new_close
    out["high"] = out["close"] + 0.5
    out["low"] = out["close"] - 0.5
    out.iloc[start_idx:, out.columns.get_loc("volume")] = 20_000.0
    return out


# ----------------------------------------------------- analyzer: honest failure


class TestHonestFailurePaths:
    def test_empty_frame_raises_unavailable(self):
        with pytest.raises(FraudDataUnavailable):
            ManipulationAnalyzer().analyze(pd.DataFrame())

    def test_missing_close_raises_unavailable(self):
        df = pd.DataFrame({"volume": [1.0] * MIN_BARS})
        with pytest.raises(FraudDataUnavailable, match="close"):
            ManipulationAnalyzer().analyze(df)

    def test_missing_volume_raises_unavailable(self):
        df = pd.DataFrame({"close": [100.0] * MIN_BARS})
        with pytest.raises(FraudDataUnavailable, match="volume"):
            ManipulationAnalyzer().analyze(df)

    def test_short_history_raises_insufficient(self):
        df = pd.DataFrame(
            {"close": np.linspace(100, 110, 30), "volume": np.linspace(1000, 1100, 30)}
        )
        with pytest.raises(InsufficientData, match="baseline"):
            ManipulationAnalyzer().analyze(df)

    def test_no_imputation_when_rows_have_nan(self):
        # Rows with NaN close/volume must be dropped, not imputed. We seed a
        # frame large enough that dropping NaNs leaves too few rows - the
        # analyzer must surface the honest failure rather than fabricate.
        n = MIN_BARS + 5
        df = pd.DataFrame(
            {"close": [100.0] * n, "volume": [1000.0] * n}
        )
        df.loc[df.index[: n - MIN_BARS + 1], "close"] = np.nan
        with pytest.raises((InsufficientData, FraudDataUnavailable)):
            ManipulationAnalyzer().analyze(df)


# ------------------------------------------------------- analyzer: volume anomaly


class TestVolumeAnomaly:
    def test_detects_volume_spike(self):
        rng = np.random.default_rng(7)
        df = _with_volume_spike(_flat_market(200, rng), spike_multiple=25.0)
        finding = ManipulationAnalyzer().detect_volume_anomaly(df)
        assert finding.pattern == "VOLUME_ANOMALY"
        assert finding.detected is True
        assert finding.confidence >= 0.95
        # Self-auditing evidence is present.
        ev = finding.evidence
        assert ev["baseline_sample_size"] >= 20
        assert ev["z_score"] is not None and ev["z_score"] >= 3.0
        assert ev["percentile_rank"] is not None and ev["percentile_rank"] >= 0.95
        assert ev["observed_raw_volume"] > 0

    def test_does_not_flag_normal_volume(self):
        df = _flat_market(200)
        finding = ManipulationAnalyzer().detect_volume_anomaly(df)
        assert finding.detected is False
        assert finding.confidence < 0.95

    def test_no_hardcoded_threshold_in_evidence(self):
        # The statistic is ranked; no fixed cutoff appears as the basis.
        rng = np.random.default_rng(11)
        df = _with_volume_spike(_flat_market(200, rng), spike_multiple=5.0)
        finding = ManipulationAnalyzer().detect_volume_anomaly(df)
        assert "percentile_rank" in finding.evidence
        assert "baseline_mean_log_volume" in finding.evidence
        assert "baseline_std_log_volume" in finding.evidence


# -------------------------------------------------------- analyzer: wash trading


class TestWashTrading:
    def test_detects_flat_high_volume(self):
        df = _with_wash_segment(_flat_market(200), n_bars=12)
        finding = ManipulationAnalyzer().detect_wash_trading(df)
        assert finding.pattern == "WASH_TRADING"
        assert finding.detected is True
        assert finding.confidence >= 0.99
        ev = finding.evidence
        assert ev["window_bars"] == 12
        assert ev["baseline_sample_size"] >= 5
        assert ev["z_score"] is not None and ev["z_score"] >= 3.0

    def test_does_not_flag_normal_market(self):
        df = _flat_market(200)
        finding = ManipulationAnalyzer().detect_wash_trading(df)
        assert finding.detected is False

    def test_high_volume_with_real_range_is_not_wash(self):
        # Heavy volume that genuinely moves the price is trading, not wash.
        # Volume and range scale together so the volume/range ratio stays
        # within the baseline distribution.
        df = _flat_market(200)
        out = df.copy()
        start = float(out["close"].iloc[-12])
        ramp = np.linspace(start, start * 1.20, 12)
        new_close = out["close"].copy()
        new_close.iloc[-12:] = ramp
        out["close"] = new_close
        out["high"].iloc[-12:] = ramp + 0.05
        out["low"].iloc[-12:] = ramp - 0.05
        out.iloc[-12:, out.columns.get_loc("volume")] = 5_000.0  # ~5x baseline
        finding = ManipulationAnalyzer().detect_wash_trading(out)
        assert finding.detected is False


# ------------------------------------------------------ analyzer: pump and dump


class TestPumpAndDump:
    def test_detects_textbook_pump_and_dump(self):
        df = _with_pump_and_dump(_flat_market(200))
        finding = ManipulationAnalyzer().detect_pump_and_dump(df)
        assert finding.pattern == "PUMP_AND_DUMP"
        assert finding.detected is True
        assert finding.confidence >= 0.50
        ev = finding.evidence
        assert ev["pump_return"] > 0
        assert ev["reversal_return"] < 0
        assert ev["pump_return_percentile"] is not None
        assert ev["pump_return_percentile"] >= 0.95
        assert ev["pump_volume_percentile"] is not None
        assert ev["pump_volume_percentile"] >= 0.90
        assert ev["reversal_to_pump_ratio"] > 0.5

    def test_pump_without_reversal_does_not_fire(self):
        # The dump half is what makes it manipulation. A pure rally must
        # not be flagged - that would be a false accusation.
        df = _flat_market(200)
        out = df.copy()
        out.iloc[-15:, out.columns.get_loc("close")] = np.linspace(
            float(out["close"].iloc[-15]), float(out["close"].iloc[-15]) * 1.50, 15
        )
        out.iloc[-15:, out.columns.get_loc("volume")] = 20_000.0
        finding = ManipulationAnalyzer().detect_pump_and_dump(out)
        assert finding.detected is False

    def test_no_fire_on_quiet_market(self):
        df = _flat_market(200)
        finding = ManipulationAnalyzer().detect_pump_and_dump(df)
        assert finding.detected is False

    def test_confidence_is_conservative_minimum(self):
        # Reported confidence is the weakest of (pump pct, vol pct, ratio),
        # never an average - so it cannot exceed the minimum component.
        df = _with_pump_and_dump(_flat_market(200))
        finding = ManipulationAnalyzer().detect_pump_and_dump(df)
        ev = finding.evidence
        components = [
            ev["pump_return_percentile"],
            ev["pump_volume_percentile"],
            ev["reversal_to_pump_ratio"],
        ]
        components = [c for c in components if c is not None]
        assert finding.confidence <= min(components) + 1e-6


# ------------------------------------------------------- analyzer: composite + 501


class TestCompositeAndUnsupported:
    def test_analyze_returns_findings_and_unsupported(self):
        df = _with_pump_and_dump(_flat_market(200))
        result = ManipulationAnalyzer().analyze(df)
        assert set(result["findings"]) == {
            "VOLUME_ANOMALY",
            "WASH_TRADING",
            "PUMP_AND_DUMP",
        }
        assert "PUMP_AND_DUMP" in result["detected_patterns"]
        assert result["composite_confidence"] >= 0.50

    def test_spoofing_and_layering_reported_as_501(self):
        df = _flat_market(200)
        result = ManipulationAnalyzer().analyze(df)
        for pattern in ("SPOOFING", "LAYERING"):
            entry = result["unsupported"][pattern]
            assert entry["status_code"] == 501
            assert "level-2" in entry["reason"].lower()
            assert entry["pattern"] == pattern

    def test_unsupported_findings_never_claim_detected(self):
        df = _flat_market(200)
        result = ManipulationAnalyzer().analyze(df)
        # Unsupported patterns must not appear in detected_patterns.
        assert "SPOOFING" not in result["detected_patterns"]
        assert "LAYERING" not in result["detected_patterns"]


# ------------------------------------------------------------- analyzer: config


class TestConfiguration:
    def test_rejects_invalid_windows(self):
        with pytest.raises(ValueError):
            ManipulationAnalyzer(baseline=5)  # < 20
        with pytest.raises(ValueError):
            ManipulationAnalyzer(event=1, reversal=1)  # event<2
        with pytest.raises(ValueError):
            ManipulationAnalyzer(event=3, reversal=3)  # event<=reversal

    def test_custom_windows_pass_through_to_findings(self):
        # Smaller valid windows should still work and surface in evidence.
        rng = np.random.default_rng(13)
        df = _flat_market(60, rng)
        a = ManipulationAnalyzer(baseline=25, event=4, reversal=1)
        finding = a.detect_volume_anomaly(df)
        assert finding.evidence["baseline_window_bars"] == 25
