"""Market manipulation detection service (K-02).

Statistical detectors over real OHLCV data: volume anomaly, wash trading,
pump-and-dump. Spoofing/layering are honestly reported as unavailable - no
level-2 feed is wired.
"""

from omni.detect.manipulation import (
    DEFAULT_BASELINE,
    DEFAULT_EVENT,
    DEFAULT_REVERSAL,
    MIN_BARS,
    Finding,
    FraudDataUnavailable,
    InsufficientData,
    ManipulationAnalyzer,
)

__all__ = [
    "DEFAULT_BASELINE",
    "DEFAULT_EVENT",
    "DEFAULT_REVERSAL",
    "MIN_BARS",
    "Finding",
    "FraudDataUnavailable",
    "InsufficientData",
    "ManipulationAnalyzer",
]
