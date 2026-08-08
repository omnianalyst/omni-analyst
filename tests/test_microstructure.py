"""Microstructure adapter: order book snapshot and trade tape.

Fixtures are recorded excerpts of the real OKX V5 response shapes, captured
from the official OKX API documentation
(https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-order-book
and -get-trades) -- not researched over the network against a live endpoint:

* order book -- ``GET /api/v5/market/books``: ``{"code","msg","data":[{"asks":
  [[px, sz, "0", n], ...], "bids": [[px, sz, "0", n], ...], "ts", "seqId"}]}``.
  bids descend by price, asks ascend. ``ts`` is a millisecond string.
* trades -- ``GET /api/v5/market/trades``: ``{"code","msg","data":[{"instId",
  "side","px","sz","tradeId","ts"}]}``. ``side`` is the taker side ("buy"/"sell").

All tests run against recorded payloads through pure parse functions and an
injected ``fetch_fn`` -- no network. The load-bearing assertions are: the spread
is measured against the mid (hand-derived below), a crossed book is refused, a
locked book is emitted, and ``knowledge_date == event_date`` for every claim.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from omni.ingest.microstructure import (
    MicrostructureAdapter,
    parse_book,
    parse_tape,
)
from omni.ingest.protocol import Unavailable

# 1700000000000 ms = 2023-11-14T22:13:20Z, the instant the onchain fixtures use.
BOOK_TS = 1700000000000
BOOK_TS_PLUS1 = 1700000001000
BOOK_TS_PLUS2 = 1700000002000
SNAP_AT = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def _at_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


# Real OKX `/api/v5/market/books` shape. Three levels a side, centred on a
# 100.00 mid so the spread arithmetic is clean. OKX packs each level as
# [price, size, deprecated-"0", order-count]; only price and size are read.
BOOK_PAYLOAD = {
    "code": "0",
    "msg": "",
    "data": [
        {
            "asks": [
                ["100.05", "1.5", "0", "3"],
                ["100.10", "4.0", "0", "1"],
                ["100.15", "2.5", "0", "2"],
            ],
            "bids": [
                ["99.95", "2.0", "0", "4"],
                ["99.90", "3.0", "0", "1"],
                ["99.85", "5.0", "0", "3"],
            ],
            "ts": str(BOOK_TS),
            "seqId": 3235851742,
        }
    ],
}

# A crossed book: best_bid (100.05) > best_ask (99.90). OKX documents this can
# arrive during the pre-open period; here it is bad data and must be refused.
CROSSED_BOOK = {
    "code": "0",
    "msg": "",
    "data": [
        {
            "asks": [["99.90", "1.0", "0", "1"]],
            "bids": [["100.05", "2.0", "0", "1"]],
            "ts": str(BOOK_TS),
        }
    ],
}

# A locked market: best_bid == best_ask == 100.00, spread zero. A real, if
# unusual, observation -- must emit a claim, not raise.
LOCKED_BOOK = {
    "code": "0",
    "msg": "",
    "data": [
        {
            "asks": [["100.00", "1.0", "0", "1"]],
            "bids": [["100.00", "2.0", "0", "2"]],
            "ts": str(BOOK_TS),
        }
    ],
}

NO_BIDS_BOOK = {
    "code": "0",
    "msg": "",
    "data": [
        {
            "asks": [["100.05", "1.5", "0", "1"]],
            "bids": [],
            "ts": str(BOOK_TS),
        }
    ],
}

NO_ASKS_BOOK = {
    "code": "0",
    "msg": "",
    "data": [
        {
            "asks": [],
            "bids": [["99.95", "2.0", "0", "1"]],
            "ts": str(BOOK_TS),
        }
    ],
}

# A book with no `ts`. An unstamped snapshot cannot be placed in time; it must
# raise rather than be stamped now().
NO_TS_BOOK = {
    "code": "0",
    "msg": "",
    "data": [
        {
            "asks": [["100.05", "1.5", "0", "1"]],
            "bids": [["99.95", "2.0", "0", "1"]],
        }
    ],
}

# Real OKX `/api/v5/market/trades` shape. Two valid trades (a buy, a sell) at
# distinct instants, plus one missing its price that must be skipped.
TRADES_PAYLOAD = {
    "code": "0",
    "msg": "",
    "data": [
        {
            "instId": "BTC-USDT",
            "side": "buy",
            "sz": "0.50000",
            "px": "100.05",
            "tradeId": "242720720",
            "ts": str(BOOK_TS),
        },
        {
            "instId": "BTC-USDT",
            "side": "sell",
            "sz": "0.80000",
            "px": "99.95",
            "tradeId": "242720719",
            "ts": str(BOOK_TS_PLUS1),
        },
        # missing price -> skipped, never zero-filled
        {
            "instId": "BTC-USDT",
            "side": "buy",
            "sz": "0.30000",
            "px": None,
            "tradeId": "242720718",
            "ts": str(BOOK_TS_PLUS2),
        },
    ],
}


class TestParseBook:
    def test_one_claim_with_every_field(self):
        drafts = parse_book(BOOK_PAYLOAD, symbol="BTC-USDT", venue="okx")
        assert len(drafts) == 1
        claim = drafts[0]
        assert claim.claim_type == "orderbook_snapshot"
        assert claim.key == "okx:BTC-USDT"
        for field in (
            "best_bid",
            "best_ask",
            "mid",
            "spread_absolute",
            "spread_bps",
            "bid_depth_n",
            "ask_depth_n",
            "symbol",
            "venue",
        ):
            assert field in claim.value, f"missing {field}"

    def test_spread_bps_is_computed_against_the_mid(self):
        # Hand-derived, independently of the implementation: the spread in bps
        # is (ask - bid) / mid * 10000, with the mid -- not the bid or ask -- as
        # the reference. costs.py charges the half-spread against the mid, so a
        # spread stated against the wrong reference biases every cost estimate.
        best_bid = Decimal("99.95")
        best_ask = Decimal("100.05")
        expected_mid = (best_bid + best_ask) / Decimal(2)  # 100.00
        expected_spread_abs = best_ask - best_bid  # 0.10
        expected_spread_bps = expected_spread_abs / expected_mid * Decimal(10000)  # 10.0

        claim = parse_book(BOOK_PAYLOAD, symbol="BTC-USDT", venue="okx")[0]
        assert Decimal(claim.value["mid"]) == expected_mid
        assert Decimal(claim.value["spread_absolute"]) == expected_spread_abs
        assert Decimal(claim.value["spread_bps"]) == expected_spread_bps
        # The discriminating check: against the bid it would be 10.0050..., not 10.
        assert Decimal(claim.value["spread_bps"]) != (
            expected_spread_abs / best_bid * Decimal(10000)
        )

    def test_a_crossed_book_raises_unavailable(self):
        with pytest.raises(Unavailable, match="crossed book"):
            parse_book(CROSSED_BOOK, symbol="BTC-USDT", venue="okx")

    def test_a_crossed_book_names_the_symbol_and_both_prices(self):
        with pytest.raises(Unavailable) as exc:
            parse_book(CROSSED_BOOK, symbol="BTC-USDT", venue="okx")
        msg = str(exc.value)
        assert "BTC-USDT" in msg
        assert "100.05" in msg
        assert "99.90" in msg

    def test_an_empty_bid_side_raises_rather_than_zero_depth(self):
        with pytest.raises(Unavailable, match="no bids"):
            parse_book(NO_BIDS_BOOK, symbol="BTC-USDT", venue="okx")

    def test_an_empty_ask_side_raises_rather_than_zero_depth(self):
        with pytest.raises(Unavailable, match="no asks"):
            parse_book(NO_ASKS_BOOK, symbol="BTC-USDT", venue="okx")

    def test_a_thin_book_reports_the_depth_it_has_without_padding(self):
        # The fixture has 3 levels a side; depth defaults to 20. The cumulative
        # size is the sum of the levels that exist (bid 2+3+5=10, ask
        # 1.5+4+2.5=8), not padded out to 20 zero-sized levels.
        claim = parse_book(BOOK_PAYLOAD, symbol="BTC-USDT", venue="okx")[0]
        assert Decimal(claim.value["bid_depth_n"]) == Decimal("10.0")
        assert Decimal(claim.value["ask_depth_n"]) == Decimal("8.0")

    def test_depth_truncates_to_the_top_n_levels(self):
        # Same book, but depth=2: only the two best levels a side count
        # (bid 2+3=5, ask 1.5+4=5.5). Proves the depth parameter is applied.
        claim = parse_book(BOOK_PAYLOAD, symbol="BTC-USDT", venue="okx", depth=2)[0]
        assert Decimal(claim.value["bid_depth_n"]) == Decimal("5.0")
        assert Decimal(claim.value["ask_depth_n"]) == Decimal("5.5")

    def test_a_zero_spread_locked_book_emits_a_claim(self):
        # best_bid == best_ask is a locked market -- a real observation. It must
        # produce a claim with a zero spread, not raise as if crossed.
        claim = parse_book(LOCKED_BOOK, symbol="BTC-USDT", venue="okx")[0]
        assert Decimal(claim.value["spread_absolute"]) == Decimal(0)
        assert Decimal(claim.value["spread_bps"]) == Decimal(0)
        assert Decimal(claim.value["mid"]) == Decimal("100.00")

    def test_a_book_with_no_timestamp_raises_rather_than_using_now(self):
        with pytest.raises(Unavailable, match="no timestamp"):
            parse_book(NO_TS_BOOK, symbol="BTC-USDT", venue="okx")

    def test_knowledge_date_equals_event_date(self):
        claim = parse_book(BOOK_PAYLOAD, symbol="BTC-USDT", venue="okx")[0]
        assert claim.knowledge_date == claim.event_date
        assert claim.event_date == SNAP_AT

    def test_an_okx_error_code_raises_unavailable(self):
        err = {"code": "50011", "msg": "rate limit reached", "data": []}
        with pytest.raises(Unavailable, match="error code"):
            parse_book(err, symbol="BTC-USDT", venue="okx")


class TestParseTape:
    def test_one_claim_per_trade_with_price_size_side_and_timestamp(self):
        drafts = parse_tape(TRADES_PAYLOAD, symbol="BTC-USDT", venue="okx")
        # Two valid trades; the priceless one is skipped.
        assert len(drafts) == 2
        for d in drafts:
            assert d.claim_type == "trade_tape"
            for field in ("price", "size", "side", "symbol", "venue"):
                assert field in d.value

    def test_sides_are_preserved_as_the_venue_publishes_them(self):
        drafts = parse_tape(TRADES_PAYLOAD, symbol="BTC-USDT", venue="okx")
        by_side = {d.value["side"]: d for d in drafts}
        assert set(by_side) == {"buy", "sell"}
        # The buy printed at the ask, the sell at the bid.
        assert Decimal(by_side["buy"].value["price"]) == Decimal("100.05")
        assert Decimal(by_side["sell"].value["price"]) == Decimal("99.95")
        assert Decimal(by_side["buy"].value["size"]) == Decimal("0.50000")
        assert Decimal(by_side["sell"].value["size"]) == Decimal("0.80000")

    def test_a_trade_missing_price_is_skipped_not_zeroed(self):
        drafts = parse_tape(TRADES_PAYLOAD, symbol="BTC-USDT", venue="okx")
        assert all(d.value["price"] != "0" for d in drafts)
        assert len(drafts) == 2

    def test_knowledge_date_equals_event_date_for_every_trade(self):
        drafts = parse_tape(TRADES_PAYLOAD, symbol="BTC-USDT", venue="okx")
        for d in drafts:
            assert d.knowledge_date == d.event_date
        assert drafts[0].event_date == _at_ms(BOOK_TS)
        assert drafts[1].event_date == _at_ms(BOOK_TS_PLUS1)

    def test_each_trade_has_a_distinct_key(self):
        drafts = parse_tape(TRADES_PAYLOAD, symbol="BTC-USDT", venue="okx")
        assert len({d.key for d in drafts}) == 2

    def test_an_empty_window_returns_empty_not_unavailable(self):
        empty = {"code": "0", "msg": "", "data": []}
        assert parse_tape(empty, symbol="BTC-USDT", venue="okx") == []


class TestAdapterRouting:
    async def test_book_route_uses_an_injected_payload(self):
        async def fake(key: str):
            assert key == "book:BTC-USDT"
            return BOOK_PAYLOAD

        drafts = await MicrostructureAdapter(venue="okx", fetch_fn=fake).fetch("book:BTC-USDT")
        assert len(drafts) == 1
        assert all(d.claim_type == "orderbook_snapshot" for d in drafts)

    async def test_tape_route_uses_an_injected_payload(self):
        async def fake(key: str):
            assert key == "tape:BTC-USDT"
            return TRADES_PAYLOAD

        drafts = await MicrostructureAdapter(venue="okx", fetch_fn=fake).fetch("tape:BTC-USDT")
        assert len(drafts) == 2
        assert all(d.claim_type == "trade_tape" for d in drafts)

    async def test_depth_is_passed_through_to_parse_book(self):
        async def fake(key: str):
            return BOOK_PAYLOAD

        drafts = await MicrostructureAdapter(venue="okx", fetch_fn=fake, depth=2).fetch(
            "book:BTC-USDT"
        )
        claim = drafts[0]
        assert Decimal(claim.value["bid_depth_n"]) == Decimal("5.0")

    async def test_a_crossed_book_propagates_through_the_adapter(self):
        async def fake(key: str):
            return CROSSED_BOOK

        with pytest.raises(Unavailable, match="crossed book"):
            await MicrostructureAdapter(venue="okx", fetch_fn=fake).fetch("book:BTC-USDT")

    async def test_an_unknown_kind_raises_unavailable(self):
        async def fake(key: str):
            return BOOK_PAYLOAD

        with pytest.raises(Unavailable, match="unknown microstructure kind"):
            await MicrostructureAdapter(venue="okx", fetch_fn=fake).fetch("depth:BTC-USDT")

    async def test_a_key_without_a_kind_separator_raises_unavailable(self):
        with pytest.raises(Unavailable, match="must be"):
            await MicrostructureAdapter(venue="okx", fetch_fn=None).fetch("BTC-USDT")

    async def test_a_fetch_failure_propagates_as_unavailable(self):
        async def broken(key: str):
            raise Unavailable("okx returned HTTP 429 for books")

        with pytest.raises(Unavailable, match="429"):
            await MicrostructureAdapter(venue="okx", fetch_fn=broken).fetch("book:BTC-USDT")


class TestAdapterDeclaration:
    def test_provider_key_is_the_venue_for_licence_lookup(self):
        # Per the P12 rule: the licence class is per venue, so provider_key is
        # the venue, not the literal "microstructure". Asserted for two venues.
        assert MicrostructureAdapter(venue="okx").provider_key == "okx"
        assert MicrostructureAdapter(venue="binance").provider_key == "binance"

    def test_source_is_microstructure(self):
        assert MicrostructureAdapter(venue="okx").source == "microstructure"

    async def test_the_venue_lands_in_the_claim_value(self):
        async def fake(key: str):
            return BOOK_PAYLOAD

        drafts = await MicrostructureAdapter(venue="okx", fetch_fn=fake).fetch("book:BTC-USDT")
        assert all(d.value["venue"] == "okx" for d in drafts)
