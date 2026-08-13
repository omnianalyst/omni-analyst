"""Tests for the carry-book capacity probe.

Everything under test is pure over a synthetic book of the shape ccxt returns,
so these are exact-value assertions rather than range checks. The tests that
carry weight are the ones a plausible wrong implementation fails: reading the
touch instead of walking the levels, pricing two legs instead of four, filling
past the visible depth, and interpolating the ladder instead of bisecting the
book.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ccxt.base.errors import ExchangeNotAvailable

from omni.venue.protocol import MarketType, Side
from ops.carry_capacity import (
    BUDGET_BPS_PER_ROUND_TRIP,
    EDGE_BPS_PER_YEAR,
    REBALANCES_PER_YEAR,
    capacity,
    leg_cost,
    pair_round_trip,
    run_samples,
    sample_once,
    summarize,
    touch,
    walk,
)

_D = Decimal


def _flat_book(price: str, size: str, half_spread: str = "0") -> dict:
    bid = _D(price) - _D(half_spread)
    ask = _D(price) + _D(half_spread)
    return {
        "bids": [[str(bid), size]],
        "asks": [[str(ask), size]],
    }


def _pair(notional: str, spot: dict, perp: dict) -> dict:
    return pair_round_trip(
        spot,
        perp,
        notional=_D(notional),
        spot_taker_bps=_D("7"),
        perp_taker_bps=_D("4.5"),
    )


def test_budget_is_the_edge_divided_by_the_rebalance_count():
    assert REBALANCES_PER_YEAR == _D("365.25") / _D(42)
    assert EDGE_BPS_PER_YEAR == _D(780)
    assert BUDGET_BPS_PER_ROUND_TRIP == _D(780) / (_D("365.25") / _D(42))
    assert _D("89.69") < BUDGET_BPS_PER_ROUND_TRIP < _D("89.71")


def test_touch_refuses_one_sided_and_crossed_books():
    assert touch({"bids": [], "asks": [["100", "1"]]}) is None
    assert touch({"bids": [["100", "1"]], "asks": []}) is None
    assert touch({"bids": [["101", "1"]], "asks": [["100", "1"]]}) is None
    assert touch(None) is None
    bid, ask, mid = touch({"bids": [["99", "1"]], "asks": [["101", "1"]]})
    assert (bid, ask, mid) == (_D(99), _D(101), _D(100))


def test_touch_ignores_non_numeric_and_non_finite_levels():
    book = {"bids": [["nan", "1"], ["99", "1"]], "asks": [[None, "1"], ["101", "1"]]}
    assert touch(book) == (_D(99), _D(101), _D(100))


def test_walk_returns_vwap_across_consumed_levels():
    levels = [(_D(100), _D(1)), (_D(110), _D(1))]
    vwap, filled = walk(levels, _D("1.5"))
    assert filled == _D("1.5")
    assert vwap == (_D(100) * _D(1) + _D(110) * _D("0.5")) / _D("1.5")


def test_walk_refuses_rather_than_pricing_a_partial_fill():
    levels = [(_D(100), _D(1))]
    assert walk(levels, _D("1.0001")) is None
    assert walk(levels, _D(0)) is None


def test_thin_book_costs_more_than_a_deep_book_at_the_same_touch():
    # Identical best bid and ask. Only a function that walks the levels can tell
    # these apart; one that reads the touch reports the same slippage for both.
    deep = {"bids": [["99.99", "1000"]], "asks": [["100.01", "1000"]]}
    thin = {"bids": [["99.99", "1000"]], "asks": [["100.01", "0.01"], ["101.00", "1000"]]}

    assert touch(deep) == touch(thin)
    deep_leg = leg_cost(deep, side=Side.BUY, notional=_D(1000), taker_fee_bps=_D(7))
    thin_leg = leg_cost(thin, side=Side.BUY, notional=_D(1000), taker_fee_bps=_D(7))
    assert thin_leg["slippage_bps"] > deep_leg["slippage_bps"] * 10
    assert thin_leg["cost_bps"] - thin_leg["slippage_bps"] == _D(7)


def test_leg_slippage_is_signed_the_same_way_for_a_buy_and_a_sell():
    book = {
        "bids": [["100", "0.5"], ["98", "100"]],
        "asks": [["100", "0.5"], ["102", "100"]],
    }
    buy = leg_cost(book, side=Side.BUY, notional=_D(100), taker_fee_bps=_D(0))
    sell = leg_cost(book, side=Side.SELL, notional=_D(100), taker_fee_bps=_D(0))
    # mid is 100; the buy pays above it and the sell receives below it, and both
    # are costs, so both slippages must come out positive.
    assert buy["vwap"] > _D(100)
    assert sell["vwap"] < _D(100)
    assert buy["slippage_bps"] > 0
    assert sell["slippage_bps"] > 0
    assert buy["slippage_bps"] == sell["slippage_bps"]


def test_leg_beyond_visible_depth_reports_the_wall_and_no_cost():
    book = {"bids": [["99.99", "1"]], "asks": [["100.01", "1"]]}
    leg = leg_cost(book, side=Side.BUY, notional=_D(1_000_000), taker_fee_bps=_D(7))
    assert leg["error"] == "unfillable_at_visible_depth"
    assert "cost_bps" not in leg
    assert leg["wall_notional"] == _D(1) * _D(100)


def test_round_trip_charges_four_legs_not_two():
    # A book with zero spread and unlimited depth has no slippage at all, so the
    # round trip is exactly the four fees. Two spot legs at 7 and two perp legs
    # at 4.5 is 23; a two-leg implementation would report 11.5.
    spot = _flat_book("100", "1000000")
    perp = _flat_book("100", "1000000")
    rt = _pair("10000", spot, perp)
    assert rt["slippage_bps"] == _D(0)
    assert rt["fee_bps_published"] == _D(23)
    assert rt["round_trip_bps_published"] == _D(23)
    assert rt["fee_bps_flat"] == _D(28)
    assert rt["round_trip_bps_flat"] == _D(28)
    assert set(rt["legs"]) == {
        "open_spot_buy",
        "open_perp_sell",
        "close_spot_sell",
        "close_perp_buy",
    }


def test_round_trip_sums_spot_and_perp_slippage_separately():
    # Spot is wide, perp is tight. Each book is hit twice, so the round trip must
    # be 2x the spot half-spread plus 2x the perp half-spread plus fees. An
    # implementation that priced one book twice would miss this.
    spot = _flat_book("100", "1000000", half_spread="0.10")  # 10 bps half-spread
    perp = _flat_book("100", "1000000", half_spread="0.01")  # 1 bp half-spread
    rt = _pair("10000", spot, perp)
    assert rt["slippage_bps"] == _D(2) * _D(10) + _D(2) * _D(1)
    assert rt["round_trip_bps_published"] == _D(22) + _D(23)


def test_round_trip_annualises_by_the_rebalance_count():
    spot = _flat_book("100", "1000000")
    perp = _flat_book("100", "1000000")
    rt = _pair("10000", spot, perp)
    assert rt["annual_cost_bps_published"] == _D(23) * REBALANCES_PER_YEAR
    assert rt["net_edge_bps_published"] == _D(780) - _D(23) * REBALANCES_PER_YEAR
    assert rt["net_edge_bps_flat"] == _D(780) - _D(28) * REBALANCES_PER_YEAR


def test_round_trip_refuses_when_any_single_leg_is_unfillable():
    # Deep on both sides of spot and on the perp bid, thin only on the perp ask,
    # which is the close leg. Three of four legs fill. The pair must still refuse
    # rather than report three legs' worth of cost as a round trip.
    spot = _flat_book("100", "1000000")
    perp = {"bids": [["99.99", "1000000"]], "asks": [["100.01", "0.0001"]]}
    rt = _pair("10000", spot, perp)
    assert rt["error"] == "unfillable"
    assert list(rt["unfillable_legs"]) == ["close_perp_buy"]
    assert "round_trip_bps_published" not in rt
    assert "slippage_bps" not in rt


def test_round_trip_rises_with_size_on_a_laddered_book():
    levels_up = [[str(100 + i), "1"] for i in range(50)]
    levels_down = [[str(100 - i), "1"] for i in range(50)]
    book = {"bids": levels_down, "asks": levels_up}
    small = _pair("100", book, book)
    large = _pair("2000", book, book)
    assert large["round_trip_bps_published"] > small["round_trip_bps_published"]


def _laddered_book(step: str = "0.01", size: str = "1", levels: int = 400) -> dict:
    mid = _D(100)
    bids = [[str(mid - _D(step) * (i + 1)), size] for i in range(levels)]
    asks = [[str(mid + _D(step) * (i + 1)), size] for i in range(levels)]
    return {"bids": bids, "asks": asks}


def test_capacity_finds_the_size_where_the_round_trip_reaches_the_budget():
    book = _laddered_book()
    cap = capacity(
        book, book, spot_taker_bps=_D("7"), perp_taker_bps=_D("4.5"), model="published"
    )
    assert cap["status"] == "measured"

    at_capacity = _pair(str(cap["notional"]), book, book)
    assert abs(at_capacity["round_trip_bps_published"] - BUDGET_BPS_PER_ROUND_TRIP) < _D(
        "0.01"
    )

    # The crossing is a real boundary: a size 10% larger must be over budget and
    # a size 10% smaller must be under it.
    over = _pair(str(cap["notional"] * _D("1.1")), book, book)
    under = _pair(str(cap["notional"] * _D("0.9")), book, book)
    assert over["round_trip_bps_published"] > BUDGET_BPS_PER_ROUND_TRIP
    assert under["round_trip_bps_published"] < BUDGET_BPS_PER_ROUND_TRIP


def test_capacity_under_the_flat_fee_model_is_smaller_than_under_published():
    book = _laddered_book()
    published = capacity(
        book, book, spot_taker_bps=_D("7"), perp_taker_bps=_D("4.5"), model="published"
    )
    flat = capacity(
        book, book, spot_taker_bps=_D("7"), perp_taker_bps=_D("4.5"), model="flat"
    )
    assert published["status"] == flat["status"] == "measured"
    # 28 bps of fee leaves less slippage budget than 23, so less size fits.
    assert flat["notional"] < published["notional"]


def test_capacity_reports_a_lower_bound_when_the_book_runs_out_first():
    # Zero spread, finite depth: cost never reaches the budget, so the answer is
    # the wall and it must be labelled as a bound rather than as capacity.
    book = _flat_book("100", "50")
    cap = capacity(
        book, book, spot_taker_bps=_D("7"), perp_taker_bps=_D("4.5"), model="published"
    )
    assert cap["status"] == "beyond_visible_depth"
    assert cap["notional"] == _D(50) * _D(100) * _D("0.999")
    assert cap["cost_at_wall_bps"] == _D(23)


def test_capacity_refuses_when_the_venue_minimum_is_already_over_budget():
    # A 60 bps half-spread costs 240 bps of slippage over four legs at any size.
    wide = _flat_book("100", "1000000", half_spread="0.6")
    cap = capacity(
        wide, wide, spot_taker_bps=_D("7"), perp_taker_bps=_D("4.5"), model="published"
    )
    assert cap["status"] == "below_floor"
    assert cap["notional"] is None
    assert cap["cost_at_floor_bps"] == _D(240) + _D(23)
    assert cap["budget_bps"] == BUDGET_BPS_PER_ROUND_TRIP
    # Widening the spread cannot rescue it, and narrowing it must.
    narrow = _flat_book("100", "1000000", half_spread="0.1")
    rescued = capacity(
        narrow, narrow, spot_taker_bps=_D("7"), perp_taker_bps=_D("4.5"), model="published"
    )
    assert rescued["status"] != "below_floor"


def test_capacity_refuses_on_an_empty_book():
    empty = {"bids": [], "asks": []}
    cap = capacity(
        empty, empty, spot_taker_bps=_D("7"), perp_taker_bps=_D("4.5"), model="published"
    )
    assert cap["status"] == "below_floor"
    assert cap["notional"] is None


def _record(asset: str, costs: list[str], capacity_notional: str | None) -> dict:
    ladder = [{"round_trip_bps_published": _D(c)} for c in costs]
    return {
        "asset": asset,
        "spot_spread_bps": _D("1"),
        "perp_spread_bps": _D("2"),
        "ladder": ladder,
        "capacity_published": {
            "status": "measured" if capacity_notional else "below_floor",
            "notional": _D(capacity_notional) if capacity_notional else None,
        },
    }


def test_summarize_spreads_costs_across_snapshots_and_annualises_them():
    six = ["10", "12", "20", "30", "60", "120"]
    other = ["14", "16", "24", "34", "64", "124"]
    summary = summarize(
        [_record("SOL", six, "9000"), _record("SOL", other, "7000")], "published"
    )
    block = summary["SOL"]
    assert block["snapshots"] == 2
    assert block["ladder"]["$500"]["round_trip_bps"]["min"] == _D(10)
    assert block["ladder"]["$500"]["round_trip_bps"]["max"] == _D(14)
    assert block["ladder"]["$100,000"]["annual_cost_bps"]["min"] == _D(120) * REBALANCES_PER_YEAR
    assert block["ladder"]["$100,000"]["net_edge_bps"]["max"] == _D(780) - _D(
        120
    ) * REBALANCES_PER_YEAR
    assert block["capacity_notional"]["median"] == _D(8000)
    assert summary["_model"]["budget_bps_per_round_trip"] == BUDGET_BPS_PER_ROUND_TRIP


def test_summarize_counts_unfillable_rungs_without_averaging_them_away():
    good = _record("PURR", ["10", "12", "20", "30", "60", "120"], "500")
    bad = _record("PURR", ["10", "12", "20", "30", "60", "120"], None)
    bad["ladder"][4] = {"error": "unfillable"}
    bad["ladder"][5] = {"error": "unfillable"}
    summary = summarize([good, bad], "published")
    block = summary["PURR"]
    assert block["ladder"]["$50,000"]["unfillable"] == 1
    assert block["ladder"]["$50,000"]["round_trip_bps"]["n"] == 1
    assert block["capacity_notional"]["n"] == 1
    assert set(block["capacity_status"]) == {"measured", "below_floor"}


def test_summarize_skips_records_that_never_measured_a_pair():
    summary = summarize(
        [
            {"asset": "WLD", "error": "no_pair"},
            _record("BTC", ["10", "12", "20", "30", "60", "120"], "50000"),
        ],
        "published",
    )
    assert summary["_skipped"]["count"] == 1
    assert summary["_skipped"]["assets"] == ["WLD"]
    assert "WLD" not in summary
    assert summary["BTC"]["snapshots"] == 1


class _FakeExchange:
    def __init__(self, book: dict, fail_on: set[str] | None = None):
        self.book = book
        self.fail_on = fail_on or set()
        self.calls: list[str] = []

    async def fetch_order_book(self, symbol, limit=None, params=None):
        self.calls.append(symbol)
        if symbol in self.fail_on:
            raise ExchangeNotAvailable(f"hyperliquid POST /info for {symbol}")
        return self.book


class _FakeVenue:
    name = "hyperliquid"

    def __init__(self, exchange: _FakeExchange):
        self._exchange = exchange

    def symbol_for(self, asset, market_type):
        return (
            f"{asset}/USDC"
            if market_type is MarketType.SPOT
            else f"{asset}/USDC:USDC"
        )

    def _market(self, symbol):
        return {"taker": 0.0007 if ":" not in symbol else 0.00045}


def test_sample_once_records_a_dropped_request_and_keeps_going():
    # One asset's book request fails. The other asset must still be measured,
    # and the failure must be a named record rather than a missing row -- an
    # absent asset and an unavailable asset are different facts.
    exchange = _FakeExchange(
        _flat_book("100", "1000000"), fail_on={"SOL/USDC:USDC"}
    )
    records = asyncio.run(sample_once(_FakeVenue(exchange), ["BTC", "SOL", "HYPE"]))

    assert [r["asset"] for r in records] == ["BTC", "SOL", "HYPE"]
    failed = records[1]
    assert failed["error"] == "venue_unavailable"
    assert "ExchangeNotAvailable" in failed["reason"]
    assert "ladder" not in failed
    for ok in (records[0], records[2]):
        assert "error" not in ok
        assert ok["ladder"][0]["round_trip_bps_published"] == _D(23)


def test_run_samples_hands_each_round_over_before_taking_the_next():
    # The first run of this probe wrote once at the end and lost 24 minutes of
    # snapshots to one dropped request. Rounds must be handed to the writer as
    # they complete, so a later failure cannot erase an earlier measurement.
    exchange = _FakeExchange(_flat_book("100", "1000000"))
    venue = _FakeVenue(exchange)
    flushed: list[list[dict]] = []
    seen_when_flushed: list[int] = []

    def on_round(records):
        flushed.append(records)
        seen_when_flushed.append(len(exchange.calls))

    out = asyncio.run(
        run_samples(venue, ["BTC", "ETH"], samples=3, interval=0, on_round=on_round)
    )

    assert len(flushed) == 3
    assert [len(r) for r in flushed] == [2, 2, 2]
    assert len(out) == 6
    # Each flush happened after its own round's four fetches and before the
    # next round's, which is what a single end-of-run write cannot satisfy.
    assert seen_when_flushed == [4, 8, 12]
