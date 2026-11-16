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


def test_lead_lag_recovers_a_planted_lag():
    """Returns are yesterday-but-two's sentiment plus noise: sentiment leads by 3.

    `Series.corr` aligns on the index, so the previous slices-then-corr
    implementation discarded the shift and returned (nearly) the lag-0
    correlation at every lag -- it could not have found this.
    """
    from omni.perception.dynamics import SentimentDynamicsModel

    rng = np.random.default_rng(7)
    n = 400
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    s = rng.normal(0, 1, n)
    r = np.empty(n)
    r[:3] = rng.normal(0, 1, 3)
    r[3:] = s[:-3] + rng.normal(0, 0.3, n - 3)

    result = SentimentDynamicsModel()._analyze_lead_lag(
        pd.DataFrame({"ASSET": s}, index=idx),
        pd.DataFrame({"ASSET": r}, index=idx),
        max_lag=10,
    )
    asset = result["by_asset"]["ASSET"]
    assert asset["optimal_lag"] == -3
    assert asset["sentiment_leads"]
    assert asset["max_correlation"] > 0.9
    by_lag = {c["lag"]: c["correlation"] for c in asset["all_correlations"]}
    # The other lags are genuinely different numbers, and small.
    assert abs(by_lag[0]) < 0.2
    assert abs(by_lag[3]) < 0.2


def test_lead_lag_reports_an_unmeasurable_lag_as_none_not_zero():
    from omni.perception.dynamics import SentimentDynamicsModel

    idx = pd.date_range("2024-01-01", periods=50, freq="D")
    flat = pd.DataFrame({"ASSET": np.full(50, 0.05)}, index=idx)
    moving = pd.DataFrame({"ASSET": np.linspace(0, 1, 50)}, index=idx)

    result = SentimentDynamicsModel()._analyze_lead_lag(flat, moving, max_lag=3)
    assert result == {"by_asset": {}, "summary": None}


def test_divergence_type_and_score_come_from_the_flagged_day():
    """A divergence flagged mid-window keeps its own day's sign and size, even
    after the series have come back together by the last row."""
    from omni.perception.dynamics import SentimentDynamicsModel

    sentiment, price = _div_series(bullish=True)
    result = SentimentDynamicsModel()._analyze_sentiment_divergence(sentiment, price)
    active = result["active_divergences"][0]
    assert active["flagged_at"] in sentiment.index
    assert active["type"] == "bullish"
    assert active["flagged_score"] > 0
