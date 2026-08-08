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

# Epoch milliseconds for 2019-01-01..2019-01-05 UTC, one day apart. Written as
# literals rather than derived from the module under test: the millisecond unit
# and the day step are facts about the exchange API, so a fixture computed from
# the module's own arithmetic would agree with whatever the module believed.
JAN_1 = 1546300800000
JAN_2 = 1546387200000
JAN_3 = 1546473600000
JAN_4 = 1546560000000
JAN_5 = 1546646400000

# Binance serves at most 1000 candles per page. Also a fact about the venue,
# not about this module -- the adapter defines no such constant.
BINANCE_MAX_PAGE = 1000


def _bar(ts_ms: int, close: float) -> list:
    return [ts_ms, close - 1.0, close + 1.0, close - 2.0, close, 100.0]


PAGE_ONE = [_bar(JAN_1, 3800.0), _bar(JAN_2, 3900.0), _bar(JAN_3, 4000.0)]
PAGE_TWO = [_bar(JAN_4, 4100.0), _bar(JAN_5, 4200.0)]


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


class FakeVenue:
    """A recording page server for the injected `fetch_fn` seam.

    `repeat=True` re-serves the last page forever, which is precisely the
    venue behaviour stall detection exists to survive. `max_requests` bounds
    the run: a pager that fails to terminate then fails the test loudly
    instead of hanging it.
    """

    def __init__(self, *pages, repeat: bool = False, max_requests: int = 6) -> None:
        self._pages = list(pages)
        self._repeat = repeat
        self._max = max_requests
        self.calls: list[dict] = []

    async def __call__(self, symbol: str, *, since=None, limit=None):
        self.calls.append({"symbol": symbol, "since": since, "limit": limit})
        if len(self.calls) > self._max:
            raise RuntimeError(
                f"pager issued {len(self.calls)} requests without terminating"
            )
        index = len(self.calls) - 1
        if index < len(self._pages):
            return self._pages[index]
        return self._pages[-1] if self._repeat else []


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
        async def fake(symbol: str, *, since=None, limit=None):
            assert symbol == "BTC/USDT"
            return OHLCV_PAYLOAD

        drafts = await CCXTAdapter(venue="binance", fetch_fn=fake).fetch("BTC/USDT")
        assert len(drafts) == 2

    async def test_knowledge_date_equals_event_date_through_the_adapter(self):
        async def fake(symbol: str, *, since=None, limit=None):
            return OHLCV_PAYLOAD

        drafts = await CCXTAdapter(venue="binance", fetch_fn=fake).fetch("BTC/USDT")
        for d in drafts:
            assert d.knowledge_date == d.event_date

    async def test_the_same_symbol_at_two_venues_is_distinguishable(self):
        """The whole point of a multi-venue feed: the same asset priced at two
        venues must produce claims a consumer can tell apart. Asserted on the
        venue label in the value and on the full value differing while the
        price matches -- the cross-venue producers depend on exactly this."""

        async def fake(symbol: str, *, since=None, limit=None):
            return OHLCV_PAYLOAD

        binance = await CCXTAdapter(venue="binance", fetch_fn=fake).fetch("BTC/USDT")
        kraken = await CCXTAdapter(venue="kraken", fetch_fn=fake).fetch("BTC/USDT")

        assert binance[0].value["venue"] == "binance"
        assert kraken[0].value["venue"] == "kraken"
        assert binance[0].value["close"] == kraken[0].value["close"]
        assert binance[0].value != kraken[0].value

    async def test_a_venues_claims_only_carry_that_venues_label(self):
        async def fake(symbol: str, *, since=None, limit=None):
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

        async def broken(symbol: str, *, since=None, limit=None):
            raise exc_cls("venue down")

        with pytest.raises(Unavailable, match="binance unavailable"):
            await CCXTAdapter(venue="binance", fetch_fn=broken).fetch("BTC/USDT")

    async def test_a_bad_symbol_raises_unavailable_naming_the_symbol(self):
        """`BadSymbol` is a different fact from "the asset did not trade":
        this venue does not list it. Returning [] would conflate the two."""
        ccxt = pytest.importorskip("ccxt")

        async def bad(symbol: str, *, since=None, limit=None):
            raise ccxt.BadSymbol("no such market")

        with pytest.raises(Unavailable, match="does not list BTC/FOO"):
            await CCXTAdapter(venue="binance", fetch_fn=bad).fetch("BTC/FOO")

    async def test_a_pre_translated_unavailable_passes_through_unchanged(self):
        async def broken(symbol: str, *, since=None, limit=None):
            raise Unavailable("binance unavailable: already translated")

        with pytest.raises(Unavailable, match="already translated"):
            await CCXTAdapter(venue="binance", fetch_fn=broken).fetch("BTC/USDT")


class TestDeepHistory:
    """Paging back through years of bars.

    The default call takes whatever page the venue offers (500 bars on
    binance), which is why the store's oldest crypto print was one default
    page old. Asking with `since` is what makes the depth reachable, and every
    assertion below exists to stop that depth arriving with the wrong
    provenance attached.
    """

    async def test_since_is_sent_to_the_venue_as_epoch_milliseconds(self):
        venue = FakeVenue(PAGE_ONE)
        adapter = CCXTAdapter(
            venue="binance", since=_at("2019-01-01T00:00:00"), fetch_fn=venue
        )
        await adapter.fetch("BTC/USDT")
        assert venue.calls[0]["since"] == 1546300800000

    async def test_no_since_asks_for_no_window_and_takes_one_page(self):
        """The live scheduler path is unchanged: one request, no window, no
        page size, exactly what the venue serves by default."""
        venue = FakeVenue(OHLCV_PAYLOAD)
        drafts = await CCXTAdapter(venue="binance", fetch_fn=venue).fetch("BTC/USDT")
        assert len(venue.calls) == 1
        assert venue.calls[0] == {"symbol": "BTC/USDT", "since": None, "limit": None}
        assert len(drafts) == 2

    async def test_the_page_limit_is_sent_on_every_request(self):
        venue = FakeVenue(PAGE_ONE, PAGE_TWO)
        adapter = CCXTAdapter(
            venue="binance",
            since=_at("2019-01-01T00:00:00"),
            page_limit=1000,
            fetch_fn=venue,
        )
        await adapter.fetch("BTC/USDT")
        assert [c["limit"] for c in venue.calls] == [BINANCE_MAX_PAGE] * len(venue.calls)

    async def test_paging_walks_forward_from_just_past_the_newest_bar(self):
        """Each page is requested from one millisecond past the newest
        timestamp already held. Re-requesting *at* that timestamp would re-serve
        the boundary bar on every page; skipping a whole timeframe forward
        would drop the bar that follows it."""
        venue = FakeVenue(PAGE_ONE, PAGE_TWO)
        adapter = CCXTAdapter(
            venue="binance", since=_at("2019-01-01T00:00:00"), fetch_fn=venue
        )
        drafts = await adapter.fetch("BTC/USDT")

        assert [c["since"] for c in venue.calls] == [JAN_1, JAN_3 + 1, JAN_5 + 1]
        assert [d.event_date for d in drafts] == [
            _at("2019-01-01T00:00:00"),
            _at("2019-01-02T00:00:00"),
            _at("2019-01-03T00:00:00"),
            _at("2019-01-04T00:00:00"),
            _at("2019-01-05T00:00:00"),
        ]

    async def test_every_backfilled_bar_is_knowable_only_at_its_own_close(self):
        """The reason deep history is worth having at all.

        A 2019 bar must carry a 2019 `knowledge_date`. If the fetch time leaked
        into it, every backfilled bar would read as known today, a replay at a
        2021 cutoff would see the whole future, and any edge measured on it
        would be an artefact. That failure is silent, so it is asserted
        directly: each pair equals the bar's own timestamp, and the newest of
        them is still years in the past.
        """
        venue = FakeVenue(PAGE_ONE, PAGE_TWO)
        adapter = CCXTAdapter(
            venue="binance", since=_at("2019-01-01T00:00:00"), fetch_fn=venue
        )
        drafts = await adapter.fetch("BTC/USDT")

        assert len(drafts) == 5
        for draft in drafts:
            assert draft.knowledge_date == draft.event_date
        assert [d.knowledge_date for d in drafts] == [
            _at("2019-01-01T00:00:00"),
            _at("2019-01-02T00:00:00"),
            _at("2019-01-03T00:00:00"),
            _at("2019-01-04T00:00:00"),
            _at("2019-01-05T00:00:00"),
        ]
        assert max(d.knowledge_date for d in drafts) < _at("2019-01-06T00:00:00")

    async def test_a_bar_returned_on_two_pages_is_emitted_once(self):
        """Consecutive pages overlap at the boundary on some venues. A bar
        emitted twice is a bar counted twice -- two claims for one print, and a
        volume that double-counts."""
        overlapping = [_bar(JAN_3, 4000.0), _bar(JAN_4, 4100.0), _bar(JAN_5, 4200.0)]
        venue = FakeVenue(PAGE_ONE, overlapping)
        adapter = CCXTAdapter(
            venue="binance", since=_at("2019-01-01T00:00:00"), fetch_fn=venue
        )
        drafts = await adapter.fetch("BTC/USDT")

        event_dates = [d.event_date for d in drafts]
        assert len(event_dates) == len(set(event_dates))
        assert event_dates == [
            _at("2019-01-01T00:00:00"),
            _at("2019-01-02T00:00:00"),
            _at("2019-01-03T00:00:00"),
            _at("2019-01-04T00:00:00"),
            _at("2019-01-05T00:00:00"),
        ]

    async def test_a_venue_that_re_serves_the_same_page_stops_the_walk(self):
        """A repeated page is the shape an unbounded pager dies of: rows keep
        arriving, so it never looks finished, and the request budget burns
        against a rate limit. Terminal, with the reason recorded."""
        venue = FakeVenue(PAGE_ONE, repeat=True, max_requests=4)
        adapter = CCXTAdapter(
            venue="binance", since=_at("2019-01-01T00:00:00"), fetch_fn=venue
        )
        drafts = await adapter.fetch("BTC/USDT")

        assert len(venue.calls) == 2
        assert len(drafts) == 3
        assert "did not advance past 1546473600000" in adapter.last_stop_reason

    async def test_a_page_of_older_bars_stops_the_walk(self):
        """A venue that ignores `since` and hands back an earlier window has
        not advanced either. Same fault, different disguise."""
        older = [_bar(JAN_1 - 86400000, 3700.0), _bar(JAN_1, 3800.0)]
        venue = FakeVenue(PAGE_ONE, older, max_requests=4)
        adapter = CCXTAdapter(
            venue="binance", since=_at("2019-01-01T00:00:00"), fetch_fn=venue
        )
        drafts = await adapter.fetch("BTC/USDT")

        assert len(venue.calls) == 2
        assert [d.event_date for d in drafts] == [
            _at("2019-01-01T00:00:00"),
            _at("2019-01-02T00:00:00"),
            _at("2019-01-03T00:00:00"),
        ]
        assert "did not advance past" in adapter.last_stop_reason

    async def test_a_page_with_no_usable_timestamp_stops_the_walk(self):
        venue = FakeVenue(PAGE_ONE, [[None, 1.0, 2.0, 0.5, 1.5, 100]], max_requests=4)
        adapter = CCXTAdapter(
            venue="binance", since=_at("2019-01-01T00:00:00"), fetch_fn=venue
        )
        drafts = await adapter.fetch("BTC/USDT")

        assert len(venue.calls) == 2
        assert len(drafts) == 3
        assert "no usable timestamp" in adapter.last_stop_reason

    async def test_the_walk_stops_once_the_cursor_passes_the_present(self):
        """There is nothing after now to ask for. Computed from the test's own
        clock so the fixture does not depend on the module's."""
        ahead = int(datetime.now(UTC).timestamp() * 1000) + 3600000
        venue = FakeVenue([_bar(JAN_1, 3800.0), _bar(ahead, 90000.0)], max_requests=4)
        adapter = CCXTAdapter(
            venue="binance", since=_at("2019-01-01T00:00:00"), fetch_fn=venue
        )
        drafts = await adapter.fetch("BTC/USDT")

        assert len(venue.calls) == 1
        assert len(drafts) == 2
        assert "reached the present" in adapter.last_stop_reason

    async def test_an_empty_first_page_yields_no_drafts_and_no_second_request(self):
        """The venue has nothing that far back. That is an answer, not a
        prompt to substitute something."""
        venue = FakeVenue([], max_requests=4)
        adapter = CCXTAdapter(
            venue="binance", since=_at("2019-01-01T00:00:00"), fetch_fn=venue
        )
        drafts = await adapter.fetch("BTC/USDT")

        assert drafts == []
        assert len(venue.calls) == 1

    async def test_a_failure_partway_through_the_walk_raises_unavailable(self):
        """A backfill that dies on page two returns nothing rather than a
        silently truncated history. The writer is idempotent, so the honest
        refusal costs a re-run and nothing else."""
        ccxt = pytest.importorskip("ccxt")
        calls: list[int | None] = []

        async def flaky(symbol: str, *, since=None, limit=None):
            calls.append(since)
            if len(calls) == 1:
                return PAGE_ONE
            raise ccxt.RateLimitExceeded("too many requests")

        adapter = CCXTAdapter(
            venue="binance", since=_at("2019-01-01T00:00:00"), fetch_fn=flaky
        )
        with pytest.raises(Unavailable, match="binance unavailable"):
            await adapter.fetch("BTC/USDT")
        assert len(calls) == 2

    async def test_the_symbol_is_carried_through_every_page(self):
        venue = FakeVenue(PAGE_ONE, PAGE_TWO)
        adapter = CCXTAdapter(
            venue="kraken", since=_at("2019-01-01T00:00:00"), fetch_fn=venue
        )
        drafts = await adapter.fetch("ETH/USD")

        assert {c["symbol"] for c in venue.calls} == {"ETH/USD"}
        assert {d.key for d in drafts} == {"ETH/USD"}
        assert {d.value["venue"] for d in drafts} == {"kraken"}

    def test_a_naive_since_is_refused(self):
        """A naive datetime is read as local time, which would silently shift
        the requested window by the operator's UTC offset."""
        with pytest.raises(ValueError, match="timezone-aware"):
            CCXTAdapter(venue="binance", since=datetime(2019, 1, 1))  # noqa: DTZ001

    def test_the_live_path_enables_ccxts_own_rate_limiter(self):
        """Deep paging is many requests against a rate-limited venue. ccxt
        knows each endpoint's weight; a hand-rolled sleep here would not."""
        options = CCXTAdapter(
            venue="binance", api_key="k", api_secret="s"
        )._exchange_options()
        assert options["enableRateLimit"] is True
        assert options["apiKey"] == "k"
        assert options["secret"] == "s"
