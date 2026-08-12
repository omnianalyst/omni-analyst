"""Measured rankings used by the Discover market scanner."""

import math
from datetime import UTC, datetime, timedelta

import pandas as pd

from omni.api.scanner import (
    ASSETS,
    SECTOR_RETURN_WINDOW,
    _compute_metrics,
    _correlation_to_market,
    _market_behavior,
    _overall_leaders,
    _payload,
    _risk_tier,
    _sector_leader_payload,
    _tier_census,
)


def test_crypto_universe_includes_monero() -> None:
    symbols = {asset["symbol"] for asset in ASSETS["Debasement"]}

    assert "XMR" in symbols


def test_broad_universe_covers_every_sector_and_major_missing_sleeves() -> None:
    assets = [asset for bucket in ASSETS.values() for asset in bucket]
    symbols = {asset["symbol"] for asset in assets}
    sector_symbols = {asset["symbol"] for asset in assets if asset["area"] == "Sector"}

    assert len(sector_symbols) == 11
    assert {"VT", "VO", "VNQ", "BNDX", "LQD", "HYG", "SGOV"} <= symbols


def test_partial_sector_coverage_withholds_overall_company_ranking() -> None:
    sectors = [{
        "name": "Technology",
        "symbol": "XLK",
        "coverage": 1,
        "leaders": [{"symbol": "AAA", "return_window": 12.0}],
    }]
    payload = _payload([], sectors, {"complete": False})

    assert payload["overall_leaders"] == []


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


def test_metrics_discard_non_finite_and_non_positive_feed_values() -> None:
    index = pd.date_range("2020-01-02", periods=500, freq="B")
    values = pd.Series(range(100, 600), index=index, dtype=float)
    values.iloc[20] = float("inf")
    values.iloc[30] = float("-inf")
    values.iloc[40] = 0

    metrics = _compute_metrics(values)

    assert all(
        value is None or not isinstance(value, float) or math.isfinite(value)
        for key, value in metrics.items()
        if key != "returns"
    )


def test_a_cash_equivalent_reports_no_sharpe_rather_than_a_huge_one() -> None:
    """The guard must catch NEARLY constant, not only exactly constant.

    Measured on the live feed 2026-08-12, SGOV had 0.205% annualised
    volatility and a genuine 4.2% annualised return, which the old
    `ann_vol > 0` guard turned into a reported Sharpe of 20.6. No strategy
    achieves that; it is a real numerator divided by a denominator that rounds
    to nothing.
    """
    index = pd.date_range("2024-01-01", periods=500, freq="D", tz=UTC)
    # ~4%/yr drift with the negligible daily wobble of a T-bill fund.
    drift = [100.0 * (1.0 + 0.04 / 365) ** day for day in range(500)]
    prices = pd.Series(drift, index=index)

    metrics = _compute_metrics(prices)

    assert metrics["volatility"] is not None
    assert metrics["volatility"] < 1.0
    assert metrics["sharpe"] is None, (
        f"a {metrics['volatility']}% volatility asset reported a Sharpe of "
        f"{metrics['sharpe']}; below the floor there is no risk to adjust for"
    )


def test_a_normally_volatile_asset_still_reports_a_sharpe() -> None:
    """The floor must not silence the assets it exists to protect."""
    index = pd.date_range("2024-01-01", periods=500, freq="D", tz=UTC)
    rng = __import__("numpy").random.default_rng(7)
    steps = rng.normal(0.0006, 0.012, size=500).cumsum()
    prices = pd.Series(100.0 * __import__("numpy").exp(steps), index=index)

    metrics = _compute_metrics(prices)

    assert metrics["volatility"] > 1.0
    assert metrics["sharpe"] is not None
    assert abs(metrics["sharpe"]) < 10, "a plausible Sharpe, not a divide-by-noise artefact"


def test_tier_census_reports_an_unreached_tier_as_zero_not_absent() -> None:
    """An omitted tier reads as a filter the caller applied.

    The stocks category holds only diversified funds and cannot reach `high`
    at all -- the most volatile measured 27.1% against a 30% cut. Showing
    `high: 0` says that honestly; omitting the key would imply the universe was
    narrowed.
    """
    census = _tier_census(
        [{"risk_tier": "low"}, {"risk_tier": "medium"}, {"risk_tier": "medium"}]
    )

    assert census["high"] == 0
    assert set(census) == {"low", "medium", "high", "unrated"}


def test_payload_publishes_a_risk_census_for_every_category() -> None:
    census = _payload([], [], {})["risk_census"]

    assert set(census) == {"stocks", "defensive", "crypto"}
    for category in census.values():
        assert category["high"] == 0
