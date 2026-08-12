"""Measured rankings used by the Discover market scanner."""

from datetime import UTC, datetime, timedelta

import pandas as pd

from omni.api.scanner import (
    SECTOR_RETURN_WINDOW,
    _compute_metrics,
    _correlation_to_market,
    _market_behavior,
    _overall_leaders,
    _risk_tier,
    _sector_leader_payload,
)


def _history(symbol: str, name: str, start: float, finish: float) -> list[dict]:
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    step = (finish - start) / SECTOR_RETURN_WINDOW
    return [
        {
            "sector_symbol": "XLK",
            "sector_name": "Information Technology",
            "symbol": symbol,
            "name": name,
            "event_date": origin + timedelta(days=index),
            "value": {"close": start + step * index},
        }
        for index in range(SECTOR_RETURN_WINDOW + 1)
    ]


def test_sector_leaders_rank_returns_and_limit_the_display() -> None:
    rows = [
        *_history("FLAT", "Flat Corp", 100, 100),
        *_history("MID", "Middle Corp", 100, 110),
        *_history("TOP", "Top Corp", 100, 130),
        *_history("LOW", "Lower Corp", 100, 105),
    ]

    sectors = _sector_leader_payload(rows)

    assert len(sectors) == 1
    assert sectors[0]["coverage"] == 4
    assert [leader["symbol"] for leader in sectors[0]["leaders"]] == [
        "TOP", "MID", "LOW", "FLAT"
    ]
    assert sectors[0]["leaders"][0]["return_window"] == 30.0


def test_sector_leaders_exclude_incomplete_or_invalid_histories() -> None:
    incomplete = _history("SHORT", "Short History", 100, 110)[:-1]
    invalid = _history("BAD", "Bad Data", 100, 110)
    for row in invalid:
        row["value"] = {"close": None}

    assert _sector_leader_payload([*incomplete, *invalid]) == []


def test_risk_tiers_are_derived_from_observed_volatility() -> None:
    assert _risk_tier(4.0) == "low"
    assert _risk_tier(18.0) == "medium"
    assert _risk_tier(42.0) == "high"
    assert _risk_tier(None) == "unrated"


def test_market_behavior_uses_measured_correlation_bands() -> None:
    assert _market_behavior(0.8) == "risk_on"
    assert _market_behavior(0.1) == "diversifier"
    assert _market_behavior(-0.3) == "counterweight"
    assert _market_behavior(None) == "unrated"


def test_market_correlation_uses_aligned_daily_returns() -> None:
    market = pd.Series([100 + index for index in range(40)], dtype=float)
    same_direction = market * 2

    assert _correlation_to_market(same_direction, market) == 1.0
    assert _correlation_to_market(same_direction.iloc[:20], market.iloc[:20]) is None


def test_overall_leaders_rank_across_sector_boundaries() -> None:
    sectors = [
        {
            "name": "Technology",
            "symbol": "XLK",
            "leaders": [{"symbol": "AAA", "return_window": 12.0}],
        },
        {
            "name": "Energy",
            "symbol": "XLE",
            "leaders": [{"symbol": "BBB", "return_window": 20.0}],
        },
    ]

    overall = _overall_leaders(sectors)

    assert [company["symbol"] for company in overall] == ["BBB", "AAA"]
    assert overall[0]["sector"] == "Energy"


def test_long_horizon_metrics_use_cagr_and_complete_calendar_years() -> None:
    index = pd.date_range("2015-01-02", "2026-08-10", freq="B")
    prices = pd.Series(100 * (1.10 ** ((index - index[0]).days / 365.2425)), index=index)

    metrics = _compute_metrics(prices)

    assert metrics["cagr_5y"] == 10.0
    assert metrics["cagr_10y"] == 10.0
    assert metrics["median_annual_return"] is not None
    assert metrics["complete_years"] == 10


def test_short_history_does_not_claim_five_or_ten_year_performance() -> None:
    index = pd.date_range("2024-01-02", periods=400, freq="B")
    metrics = _compute_metrics(pd.Series(range(100, 500), index=index), "crypto")

    assert metrics["cagr_5y"] is None
    assert metrics["cagr_10y"] is None
