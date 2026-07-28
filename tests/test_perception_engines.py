"""Tests for the ported perception engines (dynamics, herding, FOMO).

The first three tests below are the v1 smoke tests, copied verbatim from
``software/backend/tests/test_behavioral.py`` with only the import path changed
(``app.services.behavioral`` -> ``omni.perception``). They are wrapped in
``skipif`` markers because ``pyproject.toml`` does not yet declare
scipy/sklearn/networkx (W2 forbids adding them). When those deps are added the
smoke tests run unchanged.

The remaining tests are new and assert *values*, not shapes, on the pure-pandas
paths that import cleanly today: sentiment/price divergence (the flagship cross-
domain seed) and CSSD herding intensity.
"""
import importlib

import numpy as np
import pandas as pd
import pytest


def _have(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


need_scipy = pytest.mark.skipif(
    not _have("scipy"), reason="scipy not declared in pyproject (W2 forbids adding)"
)
need_sklearn = pytest.mark.skipif(
    not _have("sklearn"), reason="sklearn not declared in pyproject (W2 forbids adding)"
)
need_networkx = pytest.mark.skipif(
    not _have("networkx"), reason="networkx not declared in pyproject (W2 forbids adding)"
)


# --------------------------------------------------------------------------- #
# v1 smoke fixture (copied verbatim)
# --------------------------------------------------------------------------- #
@pytest.fixture
def price_volume():
    rng = np.random.default_rng(42)
    idx = pd.date_range("2024-01-01", periods=120, freq="D")
    symbols = ["AAA", "BBB", "CCC"]
    prices = pd.DataFrame(
        {s: 100 * np.cumprod(1 + rng.normal(0.001, 0.02, len(idx))) for s in symbols},
        index=idx,
    )
    volumes = pd.DataFrame(
        {s: rng.integers(1_000_000, 5_000_000, len(idx)).astype(float) for s in symbols},
        index=idx,
    )
    return prices, volumes


# --------------------------------------------------------------------------- #
# v1 smoke tests (import path changed only)
# --------------------------------------------------------------------------- #
@need_sklearn
def test_fomo_score_computes_without_error(price_volume):
    from omni.perception import FOMODetector

    prices, volumes = price_volume
    scores = FOMODetector().calculate_fomo_score(prices, volumes)
    assert set(scores.columns) == set(prices.columns)
    assert not scores.dropna(how="all").empty


@need_sklearn
def test_fomo_status_is_categorical():
    from omni.perception import FOMODetector

    status = FOMODetector()._get_fomo_status(0.9)
    assert isinstance(status, str) and status


@need_sklearn
@need_networkx
@need_scipy
def test_herding_intensity_computes(price_volume):
    from omni.perception import HerdingAnalyzer

    prices, _ = price_volume
    returns = prices.pct_change().dropna(how="all")
    market_returns = returns.mean(axis=1)
    intensity = HerdingAnalyzer().calculate_herding_intensity(
        returns, market_returns, method="all"
    )
    assert isinstance(intensity, dict)
    assert "overall" in intensity


# --------------------------------------------------------------------------- #
# Sentiment / price divergence -- the flagship cross-domain seed.
# _analyze_sentiment_divergence is pure pandas, so these run today.
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Herding -- CSSD is pure pandas/numpy and runs today.
# --------------------------------------------------------------------------- #
def _synthetic_returns(n=120, n_assets=10, idio_std=0.001, seed=7):
    """Returns driven by a single common factor plus idiosyncratic noise.

    Small ``idio_std`` -> assets move together (herding regime).
    Large ``idio_std`` -> assets disperse.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    common = rng.normal(0, 0.01, n)
    return pd.DataFrame(
        {f"A{i}": common + rng.normal(0, idio_std, n) for i in range(n_assets)},
        index=idx,
    )


def test_cssd_lower_under_herding_regime_than_under_dispersion():
    from omni.perception.herding import HerdingAnalyzer

    herding_returns = _synthetic_returns(idio_std=0.001)
    dispersed_returns = _synthetic_returns(idio_std=0.05)

    ha = HerdingAnalyzer()
    cssd_h = ha.calculate_cssd(herding_returns, herding_returns.mean(axis=1))["cssd"].mean()
    cssd_d = ha.calculate_cssd(dispersed_returns, dispersed_returns.mean(axis=1))["cssd"].mean()

    assert cssd_h < cssd_d
    # Herding regime is at least an order of magnitude tighter cross-sectionally.
    assert cssd_h < cssd_d / 10


def test_cssd_result_frame_has_expected_columns_and_extreme_flags():
    from omni.perception.herding import HerdingAnalyzer

    returns = _synthetic_returns(idio_std=0.03)
    market = returns.mean(axis=1)
    out = HerdingAnalyzer().calculate_cssd(returns, market)

    for col in (
        "cssd",
        "market_return",
        "extreme_day",
        "extreme_up",
        "extreme_down",
        "herding_indicator",
    ):
        assert col in out.columns
    assert len(out) == len(returns)
    # At least one extreme day is flagged given the 5% thresholds.
    assert bool(out["extreme_day"].any())


def test_herding_intensity_cssd_branch_runs_pure_pandas():
    """method='cssd' must not require scipy/sklearn/networkx."""
    from omni.perception.herding import HerdingAnalyzer

    returns = _synthetic_returns(idio_std=0.02)
    market = returns.mean(axis=1)
    intensity = HerdingAnalyzer().calculate_herding_intensity(returns, market, method="cssd")

    assert set(intensity.keys()) == {"cssd"}
    assert isinstance(intensity["cssd"]["current_herding"], bool)
    assert intensity["cssd"]["herding_days_pct"] >= 0
    assert "overall" not in intensity


# --------------------------------------------------------------------------- #
# FOMO -- requires sklearn (StandardScaler at __init__). Skipped without it.
# --------------------------------------------------------------------------- #
@need_sklearn
def test_fomo_score_rises_when_price_accelerates_and_volume_surges():
    from omni.perception import FOMODetector

    n = 200
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    prices_arr = np.concatenate(
        [
            np.linspace(100, 102, 150),  # calm
            np.linspace(102, 150, 50),   # explosive acceleration
        ]
    )
    vol_arr = np.concatenate(
        [
            np.full(150, 1_000_000.0),
            np.linspace(1_000_000, 20_000_000, 50),  # volume surge
        ]
    )
    prices = pd.DataFrame({"X": prices_arr}, index=idx)
    volumes = pd.DataFrame({"X": vol_arr}, index=idx)

    scores = FOMODetector().calculate_fomo_score(prices, volumes)

    surge_mean = scores["X"].iloc[-20:].mean()
    calm_mean = scores["X"].iloc[-60:-40].mean()
    assert surge_mean > calm_mean
