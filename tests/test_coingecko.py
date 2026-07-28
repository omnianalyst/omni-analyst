"""CoinGecko market-chart adapter.

CoinGecko is the crypto `byo_only` feed: claims it produces are pinned to the
credential owner and never enter shared coverage. As with Polygon, the adapter
only declares `provider_key = "coingecko"` and produces drafts — licence
enforcement is the writer's job — so the tests assert the declaration, the
join, and the parsing, and leave the audience rule to the writer.

The fixture is the shape CoinGecko's `/coins/{id}/market_chart` endpoint
returns: three parallel arrays (`prices`, `market_caps`, `total_volumes`),
each of `[ms_timestamp, value]` pairs. Unlike Polygon's single-letter OHLCV
fields, the three arrays arrive independently and must be joined on timestamp,
not on index — CoinGecko does not guarantee they share length or order.
"""

from datetime import UTC, datetime

import pytest

from omni.ingest.coingecko import CoinGeckoAdapter, parse_market_chart
from omni.ingest.protocol import Unavailable

MARKET_CHART_PAYLOAD = {
    "prices": [
        [1709251200000, 61000.5],
        [1709337600000, 62000.0],
    ],
    "market_caps": [
        [1709251200000, 1200000000000],
        [1709337600000, 1230000000000],
    ],
    "total_volumes": [
        [1709251200000, 30000000000],
        [1709337600000, 31000000000],
    ],
}


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


class TestParsing:
    def test_every_tick_becomes_its_own_draft(self):
        drafts = parse_market_chart(MARKET_CHART_PAYLOAD, asset_id="BTC")
        assert len(drafts) == 2

    def test_the_ticks_timestamp_is_the_event_date(self):
        drafts = parse_market_chart(MARKET_CHART_PAYLOAD, asset_id="BTC")
        first, second = drafts
        assert first.event_date == _at("2024-03-01T00:00:00")
        assert second.event_date == _at("2024-03-02T00:00:00")

    def test_knowledge_date_equals_event_date(self):
        """A crypto tick is knowable the instant it prints: crypto trades
        continuously with no session close, so there is no settlement lag to
        model. Asserted against a literal derived from the tick itself, never
        from `datetime.now()` — and equal to `event_date`, not a fabricated
        close.
        """
        drafts = parse_market_chart(MARKET_CHART_PAYLOAD, asset_id="BTC")
        for d in drafts:
            assert d.knowledge_date == d.event_date
        assert drafts[0].knowledge_date == _at("2024-03-01T00:00:00")

    def test_price_market_cap_and_volume_land_in_value(self):
        drafts = parse_market_chart(MARKET_CHART_PAYLOAD, asset_id="BTC")
        assert drafts[0].value == {
            "price": 61000.5,
            "market_cap": 1200000000000,
            "volume": 30000000000,
        }
        assert drafts[1].value == {
            "price": 62000.0,
            "market_cap": 1230000000000,
            "volume": 31000000000,
        }

    def test_a_throttle_body_raises_unavailable(self):
        """CoinGecko signals throttling over HTTP 200 with a 429 status body;
        that is the source refusing to answer, not an empty result."""
        with pytest.raises(Unavailable, match="429"):
            parse_market_chart(
                {
                    "status": {
                        "error_code": 429,
                        "error_message": "rate limited",
                    }
                },
                asset_id="BTC",
            )

    def test_an_empty_range_returns_no_drafts_rather_than_raising(self):
        """An empty `prices` array is a valid empty window. Different from a
        429."""
        assert parse_market_chart({"prices": []}, asset_id="BTC") == []

    def test_each_tick_is_a_price_snapshot_for_the_asset(self):
        drafts = parse_market_chart(MARKET_CHART_PAYLOAD, asset_id="BTC")
        assert {d.claim_type for d in drafts} == {"price_snapshot"}
        assert {d.key for d in drafts} == {"BTC"}

    def test_the_three_arrays_join_on_timestamp(self):
        """`market_caps` and `total_volumes` are joined to `prices` by
        timestamp, never by array index. Reversing their order must not move a
        value onto the wrong tick."""
        reordered = {
            "prices": [
                [1709251200000, 61000.5],
                [1709337600000, 62000.0],
            ],
            # reversed relative to prices
            "market_caps": [
                [1709337600000, 1230000000000],
                [1709251200000, 1200000000000],
            ],
            "total_volumes": [
                [1709337600000, 31000000000],
                [1709251200000, 30000000000],
            ],
        }
        drafts = parse_market_chart(reordered, asset_id="BTC")
        assert drafts[0].value["market_cap"] == 1200000000000
        assert drafts[0].value["volume"] == 30000000000
        assert drafts[1].value["market_cap"] == 1230000000000
        assert drafts[1].value["volume"] == 31000000000

    def test_mismatched_array_lengths_do_not_invent_data(self):
        """`prices` drives the row count. A missing `market_caps` entry is
        `None` (honestly absent), never zero or a neighbour's value. An extra
        `total_volumes` entry that has no matching price is dropped, not
        fabricated into a row."""
        mismatched = {
            "prices": [
                [1709251200000, 61000.5],
                [1709337600000, 62000.0],
            ],
            # one entry short — second price has no market_cap
            "market_caps": [[1709251200000, 1200000000000]],
            # one entry long — third volume has no matching price
            "total_volumes": [
                [1709251200000, 30000000000],
                [1709337600000, 31000000000],
                [1709424000000, 32000000000],
            ],
        }
        drafts = parse_market_chart(mismatched, asset_id="BTC")
        assert len(drafts) == 2
        assert drafts[0].value["market_cap"] == 1200000000000
        assert drafts[0].value["volume"] == 30000000000
        assert drafts[1].value["market_cap"] is None
        assert drafts[1].value["volume"] == 31000000000

    def test_a_tick_without_a_timestamp_is_skipped_not_guessed(self):
        payload = {
            "prices": [
                [1709251200000, 61000.5],
                ["not-a-ts", 99999.0],
                [None, 88888.0],
            ],
            "market_caps": [],
            "total_volumes": [],
        }
        drafts = parse_market_chart(payload, asset_id="BTC")
        assert len(drafts) == 1
        assert drafts[0].value["market_cap"] is None
        assert drafts[0].value["volume"] is None


class TestAdapter:
    async def test_an_injected_fetcher_needs_no_network_or_key(self):
        async def fake(coin_id: str) -> dict:
            assert coin_id == "bitcoin"
            return MARKET_CHART_PAYLOAD

        drafts = await CoinGeckoAdapter(fetch_fn=fake).fetch("BTC")
        assert len(drafts) == 2

    async def test_the_symbol_is_resolved_to_a_coingecko_id_for_the_url(self):
        """The fetch_fn receives the resolved CoinGecko id, not the ticker —
        the URL path is `/coins/{id}`, and `BTC` is not a valid path segment.
        This also confirms the map resolves `BTC` -> `bitcoin`."""
        seen: list[str] = []

        async def fake(coin_id: str) -> dict:
            seen.append(coin_id)
            return MARKET_CHART_PAYLOAD

        await CoinGeckoAdapter(fetch_fn=fake).fetch("BTC")
        assert seen == ["bitcoin"]

    async def test_an_unmapped_symbol_raises_rather_than_guessing(self):
        """Lowercasing the symbol would not find a mapping and must not be
        tried as an id: CoinGecko ids collide with tickers across unrelated
        coins, so a guess returns confidently wrong prices for another asset."""
        with pytest.raises(Unavailable, match="no CoinGecko id mapped"):
            await CoinGeckoAdapter().fetch("ZZZXYZ")

    async def test_symbol_matching_is_case_insensitive(self):
        async def fake(coin_id: str) -> dict:
            assert coin_id == "ethereum"
            return MARKET_CHART_PAYLOAD

        drafts = await CoinGeckoAdapter(fetch_fn=fake).fetch("ETH")
        assert {d.key for d in drafts} == {"ETH"}

    async def test_a_429_body_propagates_as_unavailable(self):
        async def throttled(coin_id: str) -> dict:
            return {"status": {"error_code": 429, "error_message": "rate limited"}}

        with pytest.raises(Unavailable, match="429"):
            await CoinGeckoAdapter(fetch_fn=throttled).fetch("BTC")

    async def test_a_source_error_propagates_rather_than_returning_nothing(self):
        async def broken(coin_id: str) -> dict:
            raise Unavailable("CoinGecko returned HTTP 429 for bitcoin")

        with pytest.raises(Unavailable, match="429"):
            await CoinGeckoAdapter(fetch_fn=broken).fetch("BTC")

    async def test_an_empty_response_yields_no_drafts(self):
        async def empty(coin_id: str) -> dict:
            return {"prices": []}

        assert await CoinGeckoAdapter(fetch_fn=empty).fetch("BTC") == []

    async def test_knowledge_date_equals_event_date_through_the_adapter(self):
        async def fake(coin_id: str) -> dict:
            return MARKET_CHART_PAYLOAD

        drafts = await CoinGeckoAdapter(fetch_fn=fake).fetch("BTC")
        for d in drafts:
            assert d.knowledge_date == d.event_date

    def test_the_adapter_declares_its_provider_for_licence_lookup(self):
        adapter = CoinGeckoAdapter()
        assert adapter.source == "coingecko"
        assert adapter.provider_key == "coingecko"
