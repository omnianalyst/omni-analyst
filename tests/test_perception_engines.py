"""Tests for the sentiment/price divergence engine.

_divergence is the flagship cross-domain capability; these tests assert values,
not shapes, on the pure-pandas detection path. herding.py and fomo.py were
deleted as defective orphans (float-zero fabrication, chosen-not-calibrated
thresholds, fillna-as-signal) -- their non-discriminating tests went with them.
"""
import numpy as np
import pandas as pd


def _div_series(bullish: bool, n: int = 200):
    """Construct aligned single-asset sentiment/price DataFrames.

    bullish=True  -> sentiment rises while price falls.
    bullish=False -> sentiment falls while price rises.
    """
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    if bullish:
        s = np.linspace(0, 1, n)
        p = 100 - np.linspace(0, 50, n)
    else:
        s = np.linspace(1, 0, n)
        p = 50 + np.linspace(0, 50, n)
    return (
        pd.DataFrame({"ASSET": s}, index=idx),
        pd.DataFrame({"ASSET": p}, index=idx),
    )


def test_divergence_bullish_when_sentiment_rises_and_price_falls():
    from omni.perception.dynamics import SentimentDynamicsModel

    sentiment, price = _div_series(bullish=True)
    result = SentimentDynamicsModel()._analyze_sentiment_divergence(sentiment, price)

    assert result["divergence_count"] == 1
    assert result["bullish_count"] == 1
    assert result["bearish_count"] == 0
    assert result["active_divergences"][0]["asset"] == "ASSET"

    active = result["active_divergences"][0]
    assert active["type"] == "bullish"
    assert active["sentiment_trend"] == "up"
    assert active["price_trend"] == "down"
    assert active["days_persisted"] >= 1
    assert active["divergence_score"] > 0


def test_divergence_bearish_when_sentiment_falls_and_price_rises():
    from omni.perception.dynamics import SentimentDynamicsModel

    sentiment, price = _div_series(bullish=False)
    result = SentimentDynamicsModel()._analyze_sentiment_divergence(sentiment, price)

    assert result["divergence_count"] == 1
    assert result["bearish_count"] == 1
    assert result["bullish_count"] == 0

    active = result["active_divergences"][0]
    assert active["type"] == "bearish"
    assert active["sentiment_trend"] == "down"
    assert active["price_trend"] == "up"
    assert active["divergence_score"] < 0


def test_divergence_none_when_sentiment_and_price_move_together():
    from omni.perception.dynamics import SentimentDynamicsModel

    n = 200
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    sentiment = pd.DataFrame({"ASSET": np.linspace(0, 1, n)}, index=idx)
    price = pd.DataFrame({"ASSET": 100 + np.linspace(0, 50, n)}, index=idx)

    result = SentimentDynamicsModel()._analyze_sentiment_divergence(sentiment, price)

    assert result["divergence_count"] == 0
    assert result["bullish_count"] == 0
    assert result["bearish_count"] == 0
    assert result["active_divergences"] == []


def test_divergence_requires_threshold_crossing():
    """Tiny opposite drift that never exceeds the rolling-std threshold is not flagged."""
    from omni.perception.dynamics import SentimentDynamicsModel

    n = 200
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    rng = np.random.default_rng(1)
    # Sentiment and price both noisy around a flat trend; no sustained divergence.
    sentiment = pd.DataFrame({"ASSET": rng.normal(0, 0.01, n)}, index=idx)
    price = pd.DataFrame({"ASSET": 100 + rng.normal(0, 0.1, n).cumsum() * 0}, index=idx)

    result = SentimentDynamicsModel()._analyze_sentiment_divergence(sentiment, price)

    assert result["divergence_count"] == 0


def test_divergence_abstains_on_constant_series_not_fabricate():
    """A near-constant series yields rolling-std ~1e-17; the guard must refuse
    rather than fabricate a full-confidence divergence from noise."""
    from omni.perception.dynamics import SentimentDynamicsModel

    n = 60
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    # 0.05 is not exactly representable -> std ~1e-17, the failure input.
    sentiment = pd.DataFrame({"ASSET": np.full(n, 0.05)}, index=idx)
    price = pd.DataFrame({"ASSET": np.full(n, 100.0)}, index=idx)

    result = SentimentDynamicsModel()._analyze_sentiment_divergence(sentiment, price)

    assert result["divergence_count"] == 0
    assert result["active_divergences"] == []
