"""Polygon.io aggregates adapter.

Polygon is the first `byo_only` source: claims it produces are pinned to the
credential owner and never enter shared coverage. The adapter itself does not
make that decision — it only declares `provider_key = "polygon"` and produces
drafts — so the tests assert that declaration and the parsing, and leave
licence enforcement to the writer.

The fixture is the shape Polygon's `/v2/aggs` endpoint actually returns: bars
carry millisecond `t` (the open of the window), single-letter OHLCV fields, and
a top-level `status`/`resultsCount` pair where an `ERROR` status arrives over
HTTP 200.
"""

from datetime import UTC, datetime

import pytest

from omni.ingest.polygon import PolygonAdapter, parse_aggregates
from omni.ingest.protocol import Unavailable

AGGREGATES_PAYLOAD = {
    "ticker": "AAPL",
    "status": "OK",
    "adjusted": True,
    "queryCount": 2,
    "request_id": "abc123",
    "resultsCount": 2,
    "results": [
        {
            "v": 135648044,
            "vw": 178.3418,
            "o": 178.06,
            "c": 178.41,
            "h": 179.43,
            "l": 177.41,
            "t": 1709251200000,
            "n": 735889,
        },
        {
            "v": 120000000,
            "vw": 180.0,
            "o": 178.5,
            "c": 180.2,
            "h": 181.0,
            "l": 178.0,
            "t": 1709510400000,
            "n": 600000,
        },
    ],
}


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


class TestParsing:
    def test_every_bar_becomes_its_own_draft(self):
        drafts = parse_aggregates(AGGREGATES_PAYLOAD, symbol="AAPL")
        assert len(drafts) == 2

    def test_the_bars_timestamp_is_the_event_date(self):
        drafts = parse_aggregates(AGGREGATES_PAYLOAD, symbol="AAPL")
        first, second = drafts
        assert first.event_date == _at("2024-03-01T00:00:00")
        assert second.event_date == _at("2024-03-04T00:00:00")

    def test_knowledge_date_is_the_bars_close_not_wall_clock(self):
        """A daily bar is knowable at its session close, not at fetch time.

        Asserted against fixed literals derived from the bar's own date, so the
        test still holds whenever it runs — `knowledge_date` is a function of
        the bar, never of `datetime.now()`.
        """
        drafts = parse_aggregates(AGGREGATES_PAYLOAD, symbol="AAPL")
        first, second = drafts
        assert first.knowledge_date == _at("2024-03-02T00:00:00")
        assert second.knowledge_date == _at("2024-03-05T00:00:00")
        assert first.knowledge_date > first.event_date
        assert first.knowledge_date != datetime.now(UTC)

    def test_ohlcv_lands_in_value(self):
        drafts = parse_aggregates(AGGREGATES_PAYLOAD, symbol="AAPL")
        assert drafts[0].value == {
            "open": 178.06,
            "high": 179.43,
            "low": 177.41,
            "close": 178.41,
            "volume": 135648044,
        }

    def test_a_polygon_error_at_http_200_raises_unavailable(self):
        """Polygon returns status ERROR with HTTP 200; that is the source being
        unable to answer, not an empty result."""
        with pytest.raises(Unavailable, match="ERROR"):
            parse_aggregates(
                {"status": "ERROR", "error": "Invalid ticker"},
                symbol="AAPL",
            )

    def test_an_empty_range_returns_no_drafts_rather_than_raising(self):
        """resultsCount 0 is a valid empty window (e.g. a holiday). Different
        from ERROR."""
        empty = {"status": "OK", "resultsCount": 0, "results": []}
        assert parse_aggregates(empty, symbol="AAPL") == []

    def test_each_bar_is_a_price_snapshot_for_the_symbol(self):
        drafts = parse_aggregates(AGGREGATES_PAYLOAD, symbol="AAPL")
        assert {d.claim_type for d in drafts} == {"price_snapshot"}
        assert {d.key for d in drafts} == {"AAPL"}

    def test_currency_becomes_the_unit(self):
        drafts = parse_aggregates(
            AGGREGATES_PAYLOAD, symbol="AAPL", currency="USD"
        )
        assert drafts[0].unit == "USD"

    def test_a_bar_without_a_timestamp_is_skipped_not_guessed(self):
        payload = {
            "status": "OK",
            "results": [
                {"o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10},
                {"o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "t": "not-a-ts"},
                {
                    "o": 1,
                    "h": 2,
                    "l": 0.5,
                    "c": 1.5,
                    "v": 10,
                    "t": 1709251200000,
                },
            ],
        }
        drafts = parse_aggregates(payload, symbol="AAPL")
        assert len(drafts) == 1


class TestAdapter:
    async def test_an_injected_fetcher_needs_no_network_or_key(self):
        async def fake(symbol: str) -> dict:
            assert symbol == "AAPL"
            return AGGREGATES_PAYLOAD

        drafts = await PolygonAdapter(fetch_fn=fake).fetch("AAPL")
        assert len(drafts) == 2

    async def test_no_key_and_no_fetcher_is_unavailable_not_empty(self):
        """Honest failure. An empty list would read as 'Polygon has no data'."""
        with pytest.raises(Unavailable, match="no Polygon API key"):
            await PolygonAdapter().fetch("AAPL")

    async def test_a_source_error_propagates_rather_than_returning_nothing(self):
        async def broken(symbol: str) -> dict:
            raise Unavailable("Polygon returned HTTP 429 for AAPL")

        with pytest.raises(Unavailable, match="429"):
            await PolygonAdapter(fetch_fn=broken).fetch("AAPL")

    async def test_a_polygon_error_payload_propagates_as_unavailable(self):
        async def errors(symbol: str) -> dict:
            return {"status": "ERROR", "error": "Invalid ticker"}

        with pytest.raises(Unavailable, match="ERROR"):
            await PolygonAdapter(fetch_fn=errors).fetch("AAPL")

    async def test_an_empty_response_yields_no_drafts(self):
        async def empty(symbol: str) -> dict:
            return {"status": "OK", "resultsCount": 0, "results": []}

        assert await PolygonAdapter(fetch_fn=empty).fetch("AAPL") == []

    def test_the_adapter_declares_its_provider_for_licence_lookup(self):
        adapter = PolygonAdapter()
        assert adapter.source == "polygon"
        assert adapter.provider_key == "polygon"
