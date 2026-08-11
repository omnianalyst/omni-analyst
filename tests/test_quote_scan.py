"""Tests for the quote-scan measurement.

`measure_leg` and `summarize` are pure, so these are exact-value assertions
against synthetic books -- the same shape ccxt returns. The depth test is the
discriminating one: a book with the same spread but thinner top-of-book must
cost more, which only a function that walks the levels (rather than reading the
touch) can produce.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omni.venue.protocol import MarketType, Side
from ops.quote_scan import measure_leg, summarize

_D = Decimal
_NOW = datetime.now(UTC)


def _buy_rec(book: dict, **overrides) -> dict:
    defaults = {
        "asset": "BTC",
        "market_type": MarketType.SPOT,
        "side": Side.BUY,
        "notional": _D("50"),
        "maker_fee_bps": _D("4"),
        "taker_fee_bps": _D("7"),
        "symbol": "BTC/USDC",
        "venue_name": "hyperliquid",
        "as_of": _NOW,
    }
    defaults.update(overrides)
    return measure_leg(book, **defaults)


def test_buy_fills_at_top_of_book_with_exact_costs():
    book = {"bids": [[100.00, 100.0]], "asks": [[100.10, 100.0]]}
    r = _buy_rec(book)
    assert "error" not in r

    mid = (_D(str(100.00)) + _D(str(100.10))) / 2
    spread_bps = (_D(str(100.10)) - _D(str(100.00))) / mid * _D(10_000)
    slippage_bps = (_D(str(100.10)) - mid) / mid * _D(10_000)

    assert r["mid"] == mid
    assert r["spread_bps"] == spread_bps
    assert r["taker_vwap"] == _D(str(100.10))
    assert r["slippage_bps"] == slippage_bps
    assert r["taker_cost_bps"] == slippage_bps + _D("7")
    assert r["maker_cost_bps"] == _D("4")
    assert r["gross_saving_bps"] == slippage_bps + _D("7") - _D("4")


def test_thin_top_of_book_walks_deeper_and_costs_more():
    # Same spread (best bid/ask identical), but the thin book cannot fill at the
    # top level and walks into a worse price. A reader of the touch alone would
    # report identical slippage for both -- this is what proves the walk ran.
    deep = {"bids": [[100.00, 100.0]], "asks": [[100.10, 100.0]]}
    thin = {"bids": [[100.00, 100.0]], "asks": [[100.10, 0.01], [100.50, 100.0]]}

    r_deep = _buy_rec(deep)
    r_thin = _buy_rec(thin)

    assert r_deep["spread_bps"] == r_thin["spread_bps"]
    assert r_thin["taker_vwap"] > _D(str(100.10))
    assert r_thin["slippage_bps"] > r_deep["slippage_bps"]
    assert r_thin["taker_cost_bps"] > r_deep["taker_cost_bps"]


def test_empty_book_records_no_two_sided_market():
    for book in ({"bids": [], "asks": []}, {"bids": [[100.0, 1.0]], "asks": []}, None):
        r = _buy_rec(book)
        assert r["error"] == "no_two_sided_market"
        assert r["taker_cost_bps"] is None
        assert r["gross_saving_bps"] is None


def test_crossed_book_records_no_two_sided_market():
    r = _buy_rec({"bids": [[101.0, 1.0]], "asks": [[100.0, 1.0]]})
    assert r["error"] == "no_two_sided_market"


def test_size_beyond_visible_depth_is_unfillable():
    book = {"bids": [[100.00, 0.0001]], "asks": [[100.10, 0.0001]]}
    r = _buy_rec(book)
    assert r["error"] == "unfillable"
    assert r["spread_bps"] is not None
    assert r["taker_cost_bps"] is None


def test_sell_leg_walks_into_bid_depth():
    book = {"bids": [[100.00, 0.01], [99.50, 100.0]], "asks": [[100.10, 100.0]]}
    r = _buy_rec(book, market_type=MarketType.PERPETUAL, side=Side.SELL)
    assert "error" not in r
    assert r["taker_vwap"] < _D(str(100.00))
    assert r["slippage_bps"] > _D(0)


def _full_record(asset: str, mt: str, **vals) -> dict:
    base = {
        "asset": asset,
        "market_type": mt,
        "spread_bps": _D("2"),
        "slippage_bps": _D("1"),
        "taker_cost_bps": _D("8"),
        "maker_cost_bps": _D("4"),
        "gross_saving_bps": _D("4"),
        "bid_depth_notional": _D("1000"),
        "ask_depth_notional": _D("1000"),
    }
    base.update(vals)
    return base


def test_summarize_groups_by_leg_and_counts_errors_separately():
    records = [
        _full_record("BTC", "spot", spread_bps=_D("2"), gross_saving_bps=_D("4")),
        _full_record("BTC", "spot", spread_bps=_D("6"), gross_saving_bps=_D("10")),
        _full_record("ETH", "perpetual"),
        {"asset": "SOL", "market_type": "spot", "error": "no_two_sided_market"},
    ]
    s = summarize(records)
    assert s["BTC spot"]["spread_bps"]["n"] == 2
    assert s["BTC spot"]["spread_bps"]["min"] == _D("2")
    assert s["BTC spot"]["spread_bps"]["max"] == _D("6")
    assert s["BTC spot"]["gross_saving_bps"]["n"] == 2
    assert s["ETH perpetual"]["taker_cost_bps"]["n"] == 1
    assert "SOL spot" not in s
    assert s["_errors"]["count"] == 1
    assert s["_errors"]["total"] == 4
