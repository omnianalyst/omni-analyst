"""Measured rankings used by the Discover market scanner."""

import json
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
    _mix_history,
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
    """Two years; A doubles mid-year in year 1, B doubles mid-year in year 2,
    and levels carry across the new year (A stays at its year-1 level).
    Equal weight with annual rebalancing must show +50% in BOTH years. A
    drift-weighted mix would show +50% then +33% (year 1's winner dominates
    year 2's start) -- the exact distinction this test pins. Moves are
    intra-year: a move landing on new year's is the new year's return, a
    different behaviour than the rebalancing pinned here."""
    index = pd.date_range("2022-01-03", "2023-12-29", freq="B")
    years = sorted(set(index.year))
    a: list[float] = []
    b: list[float] = []
    level_a, level_b = 100.0, 100.0
    for position, year in enumerate(years):
        days = index[index.year == year]
        mid = len(days) // 2
        if position == 0:  # A doubles mid-year
            a.extend([level_a] * mid + [level_a * 2] * (len(days) - mid))
            b.extend([level_b] * len(days))
            level_a *= 2
        else:  # B doubles mid-year, A holds its level
            a.extend([level_a] * len(days))
            b.extend([level_b] * mid + [level_b * 2] * (len(days) - mid))
            level_b *= 2
    return pd.DataFrame({"AAA": a, "BBB": b}, index=index)


def test_portfolio_history_is_exactly_annually_rebalanced() -> None:
    history = _portfolio_history(_two_sleeve_prices(), ["AAA", "BBB"])

    assert history is not None
    # Both years: one sleeve doubles, one is flat, equal weight -> +50%.
    # Drift-weighting would compound year 1's winner into a +33% year 2.
    assert history["complete_years"] == 2
    assert history["median_year"] == 50.0
    assert history["worst_year"]["return"] == 50.0
    assert history["best_year"]["return"] == 50.0
    assert history["up_years"] == 100.0
    assert history["window_start"] == "2022-01"


def test_portfolio_history_refuses_a_partial_first_year() -> None:
    """A series starting mid-year must drop that stub, so every reported
    calendar year is whole -- a half-year annualised as a year is a number
    pretending to be comparable. The stub sits directly before the real
    frame: a synthetic year-long jump between the two would be a price gap,
    which the cadence check correctly refuses."""
    frame = _two_sleeve_prices()
    stub_index = pd.date_range(
        frame.index[0] - pd.offsets.BDay(20), periods=20, freq="B"
    )
    stub = pd.DataFrame({"AAA": [100.0] * 20, "BBB": [100.0] * 20}, index=stub_index)
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


def test_a_weighted_mix_blends_by_weight_not_equally() -> None:
    """75% of a flat asset and 25% of one that halves must land at -12.5% for
    the year -- the weight is the whole point of a custom mix."""
    index = pd.date_range("2022-01-03", "2023-12-29", freq="B")
    half = len(index) // 2
    flat_then_double = [100.0] * half + [200.0] * (len(index) - half)
    halved = [100.0] * half + [50.0] * (len(index) - half)
    frame = pd.DataFrame({"AAA": flat_then_double, "BBB": halved}, index=index)

    history = _mix_history(frame, [("AAA", 3.0), ("BBB", 1.0)])

    assert history is not None
    # Year two: AAA +100% * 0.75 + BBB -50% * 0.25 = +62.5%.
    assert history["best_year"]["return"] == pytest.approx(62.5, abs=0.1)


def test_mix_weights_are_normalised_so_scale_does_not_matter() -> None:
    index = pd.date_range("2022-01-03", "2023-12-29", freq="B")
    half = len(index) // 2
    a = [100.0] * half + [200.0] * (len(index) - half)
    b = [100.0] * half + [50.0] * (len(index) - half)
    frame = pd.DataFrame({"AAA": a, "BBB": b}, index=index)

    by_ints = _mix_history(frame, [("AAA", 3), ("BBB", 1)])
    by_fractions = _mix_history(frame, [("AAA", 0.75), ("BBB", 0.25)])

    assert by_ints == by_fractions


def test_window_symbols_pin_two_mixes_to_the_same_window() -> None:
    """The comparison contract: a custom mix of two long-history assets alone
    would start in 2022, but with the policy symbols deciding the window
    alongside, both mixes must report identical windows."""
    long_index = pd.date_range("2021-01-04", "2023-12-29", freq="B")
    slow = [100.0 + i * 0.1 for i in range(len(long_index))]
    frame = pd.DataFrame(
        {"AAA": slow, "BBB": slow, "POL": slow}, index=long_index
    )

    custom = _mix_history(frame, [("AAA", 1.0), ("BBB", 1.0)], window_symbols=["POL"])
    policy = _mix_history(frame, [("POL", 1.0)], window_symbols=["AAA", "BBB"])

    assert custom is not None and policy is not None
    assert custom["window_start"] == policy["window_start"]
    assert custom["window_end"] == policy["window_end"]
    assert custom["complete_years"] == policy["complete_years"]


def test_mix_history_refuses_a_nonpositive_weight() -> None:
    index = pd.date_range("2022-01-03", "2023-12-29", freq="B")
    frame = pd.DataFrame({"AAA": [100.0] * len(index)}, index=index)

    assert _mix_history(frame, [("AAA", 0.0)]) is None


def test_company_rows_parse_the_ohlcv_snapshot_shape() -> None:
    """Company price claims store the full OHLCV object; the close is the
    line the panel must read. The first cut read a bare-number shape and
    silently produced an empty company panel."""
    from omni.api.scanner import _company_rows_to_series

    rows = [
        {"symbol": "NVDA", "value": json.dumps({"low": 1, "high": 2, "open": 1.5, "close": 1.75, "volume": 100}),
         "event_date": datetime(2024, 1, 2, tzinfo=UTC)},
        {"symbol": "NVDA", "value": json.dumps({"low": 1, "high": 2, "open": 1.5, "close": 1.8, "volume": 100}),
         "event_date": datetime(2024, 1, 3, tzinfo=UTC)},
    ]

    series = _company_rows_to_series(rows)

    assert set(series) == {"NVDA"}
    assert list(series["NVDA"].values()) == [1.75, 1.8]


def test_a_holey_series_refuses_rather_than_splices() -> None:
    """A month of missing closes in one held symbol removed those dates for
    every series -- the mix path jumped the gap and any crash inside it
    vanished. A gap past 15 calendar days in a held symbol refuses the
    history; the baseline is the symbol's own cadence, so weekends and a
    market-closed week never count as holes."""
    index = pd.date_range("2022-01-03", "2023-12-29", freq="B")
    values = pd.Series(100.0, index=index)
    holey = values.copy()
    holey.iloc[100:130] = float("nan")  # 30 business days, ~42 calendar
    frame = pd.DataFrame({"AAA": holey, "BBB": values})

    assert _mix_history(frame, [("AAA", 1.0), ("BBB", 1.0)]) is None


def test_a_long_weekend_of_gaps_is_not_a_hole() -> None:
    """Holidays and provider off-days leave short gaps; a market-closed week
    (thanksgiving stretch, exchange outage) must not refuse the history."""
    index = pd.date_range("2022-01-03", "2023-12-29", freq="B")
    values = pd.Series(100.0, index=index)
    sparse = values.copy()
    sparse.iloc[100:105] = float("nan")  # 5 business days missing
    frame = pd.DataFrame({"AAA": sparse, "BBB": values})

    assert _mix_history(frame, [("AAA", 1.0), ("BBB", 1.0)]) is not None


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


class TestReliabilityScore:
    """Median-of-components with an evidence gate -- the two failure modes of
    a weighted average that motivated it (observed live 2026-08-22): a name
    carrying growth 96.6 / stability 0.9 to a mid "balanced" rank, and a
    half-measured name reaching #2 on its present components alone."""

    def _entry(self, symbol, **scores):
        return {
            "symbol": symbol,
            "max_drawdown": scores.get("max_drawdown", -50.0),
            "underwater_pct": scores.get("underwater_pct", 50.0),
            "downside_deviation": scores.get("downside_deviation", 20.0),
            "scores": {
                "durable_growth": scores.get("durable_growth"),
                "consistency": scores.get("consistency"),
                "diversification": scores.get("diversification"),
            },
        }

    def test_a_bottom_quartile_dimension_disqualifies_not_debits(self):
        # The APP shape: brilliant growth AND consistency, catastrophic
        # downside. The balanced average read this mid-pack; a median alone
        # still reads it respectable (two strong components govern the
        # middle). The rule "best overall" needs: failing any dimension in
        # the bottom quartile disqualifies outright -- a strong dimension
        # can never buy back a collapsed one.
        entries = [
            self._entry("BLOWOUT", durable_growth=96.6, consistency=76.7,
                        diversification=48.4, max_drawdown=-91.9,
                        underwater_pct=99.0, downside_deviation=77.5),
            self._entry("STEADY", durable_growth=70.0, consistency=72.0,
                        diversification=75.0, max_drawdown=-15.0,
                        underwater_pct=20.0, downside_deviation=8.0),
            # fillers so percentiles spread
            self._entry("F1", durable_growth=50.0, consistency=50.0,
                        diversification=50.0),
            self._entry("F2", durable_growth=30.0, consistency=30.0,
                        diversification=30.0),
        ]
        from omni.api.scanner import _qualitative_scores

        _qualitative_scores(entries)
        by = {e["symbol"]: e["scores"] for e in entries}
        assert by["STEADY"]["reliability"] is not None
        assert by["BLOWOUT"]["reliability"] is None, (
            "a bottom-quartile dimension must disqualify from best overall"
        )
        assert by["BLOWOUT"]["downside"] < by["STEADY"]["downside"]  # the crash is priced into the downside component

    def test_missing_component_gates_out_not_fills_in(self):
        # The EA shape: long-term components unmeasurable on short history.
        entries = [
            self._entry("HALF", durable_growth=None, consistency=None,
                        diversification=80.0),
            self._entry("FULL", durable_growth=60.0, consistency=60.0,
                        diversification=60.0),
        ]
        from omni.api.scanner import _qualitative_scores

        _qualitative_scores(entries)
        assert entries[0]["scores"]["reliability"] is None
        assert entries[0]["scores"]["evidence_complete"] is False
        assert entries[1]["scores"]["reliability"] is not None

    def test_reliability_ranking_skips_incomplete_records(self):
        from omni.api.scanner import _payload

        full = self._entry("FULL", durable_growth=60.0, consistency=60.0,
                           diversification=60.0)
        full.update({"asset_class": "stocks", "name": "Full", "area": "x"})
        half = self._entry("HALF", durable_growth=None, consistency=None,
                           diversification=95.0)
        half.update({"asset_class": "stocks", "name": "Half", "area": "x"})
        # The pipeline sets balanced before _payload; the test mirrors it.
        full["scores"]["balanced"] = 60.0
        half["scores"]["balanced"] = 65.0
        from omni.api.scanner import _qualitative_scores

        _qualitative_scores([full, half])
        payload = _payload([], [], {}, [full, half])
        ranked = payload["category_rankings"]["stocks"]
        # Balanced still ranks both (it reweights); reliability ranks only FULL.
        assert {a["symbol"] for a in payload["reliability_rankings"]["stocks"]} == {"FULL"}
        assert "HALF" in {a["symbol"] for a in ranked}


class TestQualityScore:
    """The candidate list: best stocks right now to choose from.

    Quality ranks the three ASSET dimensions and refuses to charge market
    correlation against a single name -- the NVDA case (2026-08-23): the
    universe's best growth franchise, excluded from reliability because it
    IS ~8% of the index it correlates with. Diversification gates
    reliability (portfolio building blocks); it never gates quality (the
    candidate set a veteran chooses FROM, then allocates).
    """

    def _entry(self, symbol, **kw):
        return {
            "symbol": symbol,
            "max_drawdown": kw.get("max_drawdown", -50.0),
            "underwater_pct": kw.get("underwater_pct", 50.0),
            "downside_deviation": kw.get("downside_deviation", 20.0),
            "scores": {
                "durable_growth": kw.get("durable_growth"),
                "consistency": kw.get("consistency"),
                "diversification": kw.get("diversification", 50.0),
            },
        }

    def test_market_correlation_never_gates_quality(self):
        from omni.api.scanner import _qualitative_scores

        # Six names so quartiles are meaningful: MAG7's downside must be
        # mid-pack (a real but survivable drawdown), not the category's worst.
        entries = [
            self._entry("MAG7", durable_growth=99.0, consistency=84.0,
                        diversification=4.0, max_drawdown=-55.0,
                        underwater_pct=60.0, downside_deviation=30.0),
            self._entry("STEADY", durable_growth=70.0, consistency=72.0,
                        diversification=90.0, max_drawdown=-15.0,
                        underwater_pct=20.0, downside_deviation=8.0),
            self._entry("F1", durable_growth=50.0, consistency=50.0),
            self._entry("F2", durable_growth=30.0, consistency=30.0),
            self._entry("WORSE1", durable_growth=40.0, consistency=40.0,
                        diversification=40.0, max_drawdown=-80.0,
                        underwater_pct=85.0, downside_deviation=55.0),
            self._entry("WORSE2", durable_growth=45.0, consistency=35.0,
                        diversification=45.0, max_drawdown=-85.0,
                        underwater_pct=90.0, downside_deviation=60.0),
        ]
        _qualitative_scores(entries)
        by = {e["symbol"]: e["scores"] for e in entries}
        # Quality ranks MAG7 despite div=4 (correlation is not the stock's
        # fault); reliability still refuses it.
        assert by["MAG7"]["quality"] is not None
        assert by["MAG7"]["reliability"] is None
        assert by["MAG7"]["quality"] > by["STEADY"]["quality"]
        assert by["STEADY"]["reliability"] is not None

    def test_quality_still_gates_on_evidence_and_floor(self):
        from omni.api.scanner import _qualitative_scores

        entries = [
            self._entry("HALF", durable_growth=None, consistency=80.0),
            self._entry("CRASH", durable_growth=90.0, consistency=85.0,
                        max_drawdown=-92.0, underwater_pct=95.0,
                        downside_deviation=70.0),
            self._entry("F1", durable_growth=50.0, consistency=50.0),
            self._entry("F2", durable_growth=30.0, consistency=30.0),
        ]
        _qualitative_scores(entries)
        by = {e["symbol"]: e["scores"] for e in entries}
        assert by["HALF"]["quality"] is None          # unmeasured dimension
        assert by["CRASH"]["quality"] is None         # bottom-quartile downside
