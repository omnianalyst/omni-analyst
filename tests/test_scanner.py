"""Measured rankings used by the Discover market scanner."""

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from omni.api.scanner import (
    ASSETS,
    INCOME_AND_COST,
    REPRESENTATIVE_ASSETS,
    SECTOR_RETURN_WINDOW,
    _compute_metrics,
    _correlation_to_market,
    _drop_broken_seed_prefix,
    _feed_defect_reasons,
    _market_behavior,
    _overall_leaders,
    _payload,
    _portfolio_history,
    _representative,
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


def test_every_regime_bucket_designates_a_representative_that_exists_in_it() -> None:
    """The portfolio's picks are policy, so each bucket must actually contain
    its designated sleeve -- a representative pointing outside its bucket is a
    mis-edited registry, not a judgment call."""
    for bucket_name, designated in REPRESENTATIVE_ASSETS.items():
        symbols = {asset["symbol"] for asset in ASSETS[bucket_name]}
        assert designated["symbol"] in symbols, (
            f"{bucket_name} designates {designated['symbol']}, which is not in the bucket"
        )
        assert designated["reason"], f"{bucket_name} must record why its sleeve is policy"


def test_the_representative_is_returned_only_when_it_survived_ranking() -> None:
    """A refused or unranked representative must yield None so the caller
    falls back to measurement -- never a name the data cannot stand behind."""
    assert _representative("Growth", {"VTI", "SPY"}) is not None
    assert _representative("Growth", {"SPY"}) is None
    assert _representative("NotABucket", {"VTI"}) is None


def _two_sleeve_prices() -> pd.DataFrame:
    """2022 and 2023, each sleeve doubling in one and halving in the other,
    offset between the years. Equal weight with annual rebalancing must end
    each year flat and show two zero calendar years -- drift-weighting would
    instead compound (2022 halves the winner of 2023's sleeve), which is the
    exact distinction this test pins."""
    index = pd.date_range("2022-01-03", "2023-12-29", freq="B")
    half = len(index) // 2
    a = [100.0] * half + [200.0] * (len(index) - half)  # A doubles in year two
    b = [50.0] * half + [25.0] * (len(index) - half)  # B halves in year two
    return pd.DataFrame({"AAA": a, "BBB": b}, index=index)


def test_portfolio_history_is_exactly_annually_rebalanced() -> None:
    history = _portfolio_history(_two_sleeve_prices(), ["AAA", "BBB"])

    assert history is not None
    # Each year: one sleeve x2, one sleeve /2, equal weight -> exactly flat.
    assert history["complete_years"] == 2
    assert history["median_year"] == 0.0
    assert history["worst_year"]["return"] == 0.0
    assert history["best_year"]["return"] == 0.0
    # Flat path -> no drawdown, no volatility.
    assert history["worst_drawdown"] == 0.0
    assert history["volatility"] == 0.0
    assert history["window_start"] == "2022-01"


def test_portfolio_history_refuses_a_partial_first_year() -> None:
    """A series starting mid-year must drop that stub, so every reported
    calendar year is whole -- a half-year annualised as a year is a number
    pretending to be comparable."""
    frame = _two_sleeve_prices()
    stub = frame.iloc[:20].copy()
    stub.index = stub.index - pd.Timedelta(days=365)
    extended = pd.concat([stub, frame])

    history = _portfolio_history(extended, ["AAA", "BBB"])

    assert history is not None
    assert history["window_start"] == "2022-01"
    assert history["complete_years"] == 2


def test_portfolio_history_is_none_when_a_sleeve_is_missing() -> None:
    """One refused feed means no mix history at all -- not a three-sleeve
    approximation wearing the four-sleeve portfolio's name."""
    frame = _two_sleeve_prices()

    assert _portfolio_history(frame, ["AAA", "MISSING"]) is None


def test_a_drawdown_crossing_years_is_measured_through_the_trough() -> None:
    """Two bad years in a row: the drawdown must span the decline (not reset
    each January), which is what holding through it actually costs you. The
    decline is intra-year, like a real market."""
    index = pd.date_range("2022-01-03", "2023-12-29", freq="B")
    half = len(index) // 2
    # Year 1: flat at 100, then declines linearly to 50 across year 2.
    second = [100.0 - 50.0 * (i + 1) / (len(index) - half) for i in range(len(index) - half)]
    values = [100.0] * half + second
    frame = pd.DataFrame({"AAA": values, "BBB": values}, index=index)

    history = _portfolio_history(frame, ["AAA", "BBB"])

    assert history is not None
    assert history["worst_year"]["return"] == pytest.approx(-50.0, abs=0.2)
    assert history["worst_drawdown"] == pytest.approx(-50.0, abs=0.2)


def test_every_broad_etf_carries_income_and_cost() -> None:
    """The static table must cover every non-crypto asset in the universe --
    a missing entry renders as 'unknown' and an unknown cost is the one thing
    this product refuses to guess."""
    broad = {
        asset["symbol"]
        for assets in ASSETS.values()
        for asset in assets
        if asset["asset_class"] != "crypto"
    }

    missing = broad - set(INCOME_AND_COST)
    assert not missing, f"assets without income/cost figures: {sorted(missing)}"
    for figures in INCOME_AND_COST.values():
        assert figures["expense_ratio_pct"] >= 0
        assert figures["yield_pct"] >= 0


def test_a_broken_seed_print_is_dropped_not_priced() -> None:
    """The AAVE defect, reconstructed. Yahoo seeded AAVE-USD at $0.52 against a
    real first close of $53 -- a single 100x day that reported as 4,207%
    annualised volatility (the true figure was ~109%). The seed must be
    truncated away, and the honest vol of the remaining series is what shows.
    """
    index = pd.date_range("2024-01-01", periods=501, freq="D", tz=UTC)
    rng = __import__("numpy").random.default_rng(11)
    steps = rng.normal(0.0005, 0.02, size=500).cumsum()
    real = pd.Series(100.0 * __import__("numpy").exp(steps), index=index[1:])
    seeded = pd.concat([pd.Series([1.0], index=index[:1]), real])

    dropped = _drop_broken_seed_prefix(seeded)

    assert dropped.index[0] == real.index[0], "the seed print is gone"
    assert len(dropped) == 500
    metrics = _compute_metrics(dropped, "crypto")
    assert metrics["volatility"] < 60, (
        f"{metrics['volatility']}% vol -- the seed survived and priced a 100x day"
    )
    # Discrimination: the untruncated series prices the seed as an impossible
    # market, which is the defect these assertions exist to catch.
    poisoned = _compute_metrics(seeded, "crypto")
    assert poisoned["volatility"] > 500


def test_a_real_crash_day_is_not_treated_as_a_broken_seed() -> None:
    """The floor must not eat genuine history. Crypto majors have printed
    -80% days; a drop inside the 10x multiple stays in the series, and only a
    jump past it truncates."""
    index = pd.date_range("2024-01-01", periods=500, freq="D", tz=UTC)
    values = [100.0] * 250 + [20.0] + [21.0] * 249  # -80% single day
    prices = pd.Series(values, index=index)

    dropped = _drop_broken_seed_prefix(prices)

    assert len(dropped) == 500, "a real crash day is kept"
    assert dropped.index[0] == index[0]


def test_a_series_of_mostly_broken_prints_truncates_to_honest_emptiness() -> None:
    """Two seeds in a row (0.01 -> 1 -> 100): the loop keeps cutting until the
    series is continuous. What survives is measured; nothing is invented."""
    index = pd.date_range("2024-01-01", periods=6, freq="D", tz=UTC)
    prices = pd.Series([0.01, 1.0, 100.0, 102.0, 99.0, 101.0], index=index)

    dropped = _drop_broken_seed_prefix(prices)

    assert list(dropped.values) == [100.0, 102.0, 99.0, 101.0]


def _flip_series(flip_count: int) -> pd.Series:
    """A feed alternating between a real ~$3 scale and a broken ~$0.5 scale --
    the measured TON-USD shape. Each flip prints a move past 3x."""
    index = pd.date_range("2024-01-01", periods=200, freq="D", tz=UTC)
    values: list[float] = []
    level = 3.0
    for day in range(200):
        level *= 1.0 + (0.01 if day % 2 == 0 else -0.008)
        if day in [50, 80, 110, 140, 170, 190][:flip_count]:
            values.append(0.5)
        else:
            values.append(level)
    return pd.Series(values, index=index)


def test_a_feed_flipping_between_scales_is_refused_not_priced() -> None:
    """TON-USD measured 2026-08-14: six daily moves beyond 3x from a feed
    oscillating between a wrong and a right price scale. A cluster of
    impossible moves is a feed defect; the asset must be refused, not ranked
    at the garbage volatility the series implies."""
    reasons = _feed_defect_reasons(_flip_series(6), census_price=None)

    assert reasons, "a scale-flipping feed produced no defect reason"
    assert "6 daily moves" in reasons[0]


def test_one_real_mania_print_is_not_a_feed_defect() -> None:
    """DOGE really printed +355% on 2021-04-16 -- the single worst legitimate
    day in the ranked universe. One such day must not refuse the feed; the
    check exists to remove broken feeds, not history."""
    series = _flip_series(1)

    assert _feed_defect_reasons(series, census_price=None) == []


def test_a_wrong_scale_tail_disagrees_with_the_live_census_price() -> None:
    """TON's second shape: a smooth, internally consistent tail at the wrong
    price level ($0.005 against a live $3). No move check can catch a
    consistent series; the census cross-check is what does."""
    index = pd.date_range("2024-01-01", periods=200, freq="D", tz=UTC)
    tail = pd.Series([0.005 * (1 + 0.001 * day) for day in range(200)], index=index)

    reasons = _feed_defect_reasons(tail, census_price=3.0)

    assert len(reasons) == 1
    assert "$0.005" in reasons[0] and "$3" in reasons[0]


def test_a_tail_matching_the_census_price_is_kept() -> None:
    index = pd.date_range("2024-01-01", periods=200, freq="D", tz=UTC)
    tail = pd.Series([3.0 * (1 + 0.001 * day) for day in range(200)], index=index)

    assert _feed_defect_reasons(tail, census_price=3.2) == []


def test_without_a_census_price_the_price_check_degrades_to_not_running() -> None:
    """The registry-fallback census carries no live price. The move check
    still refuses a flipping feed; the price check must degrade honestly
    rather than trust or condemn the tail it cannot see."""
    assert len(_feed_defect_reasons(_flip_series(6), census_price=None)) == 1
    tail = pd.Series(
        [0.005 * (1 + 0.001 * day) for day in range(200)],
        index=pd.date_range("2024-01-01", periods=200, freq="D", tz=UTC),
    )
    assert _feed_defect_reasons(tail, census_price=None) == []
