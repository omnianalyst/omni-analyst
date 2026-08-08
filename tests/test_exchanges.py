"""ccxt multi-venue OHLCV adapter.

Every venue ccxt exposes is a `byo_only` source: an exchange's terms forbid
serving its prints on to third parties, so a claim fetched here is pinned to the
credential owner and never enters shared coverage. As with Polygon/CoinGecko the
adapter only declares `provider_key = <venue>` and produces drafts -- licence
enforcement is the writer's job -- so the tests assert the declaration, the
venue distinction, and the parsing, and leave the audience rule to the writer.

The fixture is the shape `ccxt.fetch_ohlcv` actually returns: a list of
``[ts_ms, open, high, low, close, volume]`` rows. Parsing is exercised through
an injected `fetch_fn` (no network, no key), so the parse tests do not require
ccxt to be installed. The ccxt-exception translation tests do reference ccxt's
own exception types; they import it through `pytest.importorskip` so they skip
honestly if ccxt is absent rather than erroring on import.
"""

from datetime import UTC, datetime

import pytest

from omni.ingest.exchanges import CCXTAdapter, parse_ohlcv
from omni.ingest.protocol import Unavailable

# ccxt returns rows of [ts_ms, o, h, l, c, v]. These two timestamps are
# 2024-03-01 and 2024-03-02 00:00:00 UTC -- the same calendar points the
# CoinGecko fixture uses, so the bitemporal assertions share literals.
OHLCV_PAYLOAD = [
    [1709251200000, 178.06, 179.43, 177.41, 178.41, 135648044],
    [1709337600000, 178.50, 181.00, 178.00, 180.20, 120000000],
]


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


class TestParsing:
    def test_one_claim_per_bar_with_all_five_fields_and_venue(self):
        drafts = parse_ohlcv(OHLCV_PAYLOAD, symbol="BTC/USDT", venue="binance")
        assert len(drafts) == 2
        assert drafts[0].value == {
            "open": 178.06,
            "high": 179.43,
            "low": 177.41,
            "close": 178.41,
            "volume": 135648044,
            "venue": "binance",
        }
        assert drafts[1].value["venue"] == "binance"

    def test_event_date_equals_knowledge_date(self):
        """A crypto bar is knowable the moment it closes: crypto trades
        continuously with no settlement lag. Asserted against a literal derived
        from the bar's own timestamp, never from `datetime.now()`, and equal to
        `event_date` -- not a fabricated close."""
        drafts = parse_ohlcv(OHLCV_PAYLOAD, symbol="BTC/USDT", venue="binance")
        assert len(drafts) == 2
        for d in drafts:
            assert d.event_date == d.knowledge_date
        assert drafts[0].event_date == _at("2024-03-01T00:00:00")
        assert drafts[0].knowledge_date == _at("2024-03-01T00:00:00")
        assert drafts[0].knowledge_date != datetime.now(UTC)

    def test_each_bar_is_a_price_snapshot_for_the_symbol(self):
        drafts = parse_ohlcv(OHLCV_PAYLOAD, symbol="BTC/USDT", venue="binance")
        assert {d.claim_type for d in drafts} == {"price_snapshot"}
        assert {d.key for d in drafts} == {"BTC/USDT"}

    def test_a_null_close_is_skipped_neighbours_still_emitted(self):
        payload = [
            [1709251200000, 1.0, 2.0, 0.5, 1.5, 100],
            [1709337600000, 1.0, 2.0, 0.5, None, 100],
            [1709424000000, 1.0, 2.0, 0.5, 1.7, 100],
        ]
        drafts = parse_ohlcv(payload, symbol="BTC/USDT", venue="binance")
        assert len(drafts) == 2
        assert [d.value["close"] for d in drafts] == [1.5, 1.7]

    def test_a_zero_volume_bar_is_emitted_not_dropped(self):
        """A zero-volume bar is a real print (e.g. an illiquid venue), not a
        sentinel for absence. Dropping it would silently erase a day. The
        parser guards on None/NaN/inf, never on == 0."""
        payload = [[1709251200000, 1.0, 2.0, 0.5, 1.5, 0]]
        drafts = parse_ohlcv(payload, symbol="BTC/USDT", venue="binance")
        assert len(drafts) == 1
        assert drafts[0].value["volume"] == 0

    def test_a_row_missing_a_field_is_skipped(self):
        payload = [
            [1709251200000, 1.0, 2.0, 0.5],  # only 4 elements
            [1709337600000, 1.0, 2.0, 0.5, 1.5, 100],
        ]
        drafts = parse_ohlcv(payload, symbol="BTC/USDT", venue="binance")
        assert len(drafts) == 1
        assert drafts[0].value["close"] == 1.5

    def test_a_nan_close_is_skipped(self):
        payload = [
            [1709251200000, 1.0, 2.0, 0.5, float("nan"), 100],
            [1709337600000, 1.0, 2.0, 0.5, 1.5, 100],
        ]
        drafts = parse_ohlcv(payload, symbol="BTC/USDT", venue="binance")
        assert len(drafts) == 1

    def test_a_row_without_a_timestamp_is_skipped(self):
        payload = [
            [None, 1.0, 2.0, 0.5, 1.5, 100],
            ["not-a-ts", 1.0, 2.0, 0.5, 1.5, 100],
            [1709337600000, 1.0, 2.0, 0.5, 1.5, 100],
        ]
        drafts = parse_ohlcv(payload, symbol="BTC/USDT", venue="binance")
        assert len(drafts) == 1

    def test_an_empty_result_returns_no_drafts(self):
        assert parse_ohlcv([], symbol="BTC/USDT", venue="binance") == []
        assert parse_ohlcv(None, symbol="BTC/USDT", venue="binance") == []


class TestAdapter:
    async def test_an_injected_fetcher_needs_no_network_or_key(self):
        async def fake(symbol: str):
            assert symbol == "BTC/USDT"
            return OHLCV_PAYLOAD

        drafts = await CCXTAdapter(venue="binance", fetch_fn=fake).fetch("BTC/USDT")
        assert len(drafts) == 2

    async def test_knowledge_date_equals_event_date_through_the_adapter(self):
        async def fake(symbol: str):
            return OHLCV_PAYLOAD

        drafts = await CCXTAdapter(venue="binance", fetch_fn=fake).fetch("BTC/USDT")
        for d in drafts:
            assert d.knowledge_date == d.event_date

    async def test_the_same_symbol_at_two_venues_is_distinguishable(self):
        """The whole point of a multi-venue feed: the same asset priced at two
        venues must produce claims a consumer can tell apart. Asserted on the
        venue label in the value and on the full value differing while the
        price matches -- the cross-venue producers depend on exactly this."""

        async def fake(symbol: str):
            return OHLCV_PAYLOAD

        binance = await CCXTAdapter(venue="binance", fetch_fn=fake).fetch("BTC/USDT")
        kraken = await CCXTAdapter(venue="kraken", fetch_fn=fake).fetch("BTC/USDT")

        assert binance[0].value["venue"] == "binance"
        assert kraken[0].value["venue"] == "kraken"
        assert binance[0].value["close"] == kraken[0].value["close"]
        assert binance[0].value != kraken[0].value

    async def test_a_venues_claims_only_carry_that_venues_label(self):
        async def fake(symbol: str):
            return OHLCV_PAYLOAD

        drafts = await CCXTAdapter(venue="coinbase", fetch_fn=fake).fetch("ETH/USD")
        assert {d.value["venue"] for d in drafts} == {"coinbase"}

    def test_provider_key_is_the_venue_not_ccxt(self):
        """`provider_key` indexes the credential catalog, which classifies the
        licence per provider. Collapsing every venue onto "ccxt" would give one
        venue's terms to all of them, so it must be the venue."""
        assert CCXTAdapter(venue="binance").provider_key == "binance"
        assert CCXTAdapter(venue="coinbase").provider_key == "coinbase"
        assert CCXTAdapter(venue="kraken").provider_key == "kraken"

    def test_source_is_ccxt(self):
        assert CCXTAdapter(venue="binance").source == "ccxt"


class TestFailureTranslation:
    """Every ccxt exception meaning "the venue would not answer" becomes
    `Unavailable` -- the single source-failure signal the fill pipeline records
    as `unfillable`. These reference ccxt's own exception types, so they import
    ccxt through `pytest.importorskip` and skip honestly if it is absent."""

    @pytest.mark.parametrize(
        "exc_name",
        ["NetworkError", "ExchangeNotAvailable", "RateLimitExceeded", "RequestTimeout"],
    )
    async def test_unavailable_exceptions_translate_to_unavailable(self, exc_name):
        ccxt = pytest.importorskip("ccxt")
        exc_cls = getattr(ccxt, exc_name)

        async def broken(symbol: str):
            raise exc_cls("venue down")

        with pytest.raises(Unavailable, match="binance unavailable"):
            await CCXTAdapter(venue="binance", fetch_fn=broken).fetch("BTC/USDT")

    async def test_a_bad_symbol_raises_unavailable_naming_the_symbol(self):
        """`BadSymbol` is a different fact from "the asset did not trade":
        this venue does not list it. Returning [] would conflate the two."""
        ccxt = pytest.importorskip("ccxt")

        async def bad(symbol: str):
            raise ccxt.BadSymbol("no such market")

        with pytest.raises(Unavailable, match="does not list BTC/FOO"):
            await CCXTAdapter(venue="binance", fetch_fn=bad).fetch("BTC/FOO")

    async def test_a_pre_translated_unavailable_passes_through_unchanged(self):
        async def broken(symbol: str):
            raise Unavailable("binance unavailable: already translated")

        with pytest.raises(Unavailable, match="already translated"):
            await CCXTAdapter(venue="binance", fetch_fn=broken).fetch("BTC/USDT")
