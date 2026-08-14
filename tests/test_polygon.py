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

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.fill.pipeline import run_once
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


def _payload_with_bar(**updates):
    bar = dict(AGGREGATES_PAYLOAD["results"][0])
    bar.update(updates)
    return {"status": "OK", "resultsCount": 1, "results": [bar]}


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

    @pytest.mark.parametrize(
        "timestamp",
        [None, True, "not-a-ts", float("nan"), float("inf"), 0, -1],
    )
    def test_a_malformed_timestamp_is_refused_not_guessed(self, timestamp):
        with pytest.raises(Unavailable, match="timestamp"):
            parse_aggregates(_payload_with_bar(t=timestamp), symbol="AAPL")

    @pytest.mark.parametrize("bar", [None, [], "not-a-bar"])
    def test_a_non_object_bar_is_refused(self, bar):
        with pytest.raises(Unavailable, match="malformed bar"):
            parse_aggregates({"status": "OK", "results": [bar]}, symbol="AAPL")

    @pytest.mark.parametrize("results", [None, {}, "not-a-list"])
    def test_malformed_results_are_refused(self, results):
        with pytest.raises(Unavailable, match="malformed results"):
            parse_aggregates({"status": "OK", "results": results}, symbol="AAPL")

    @pytest.mark.parametrize("field", ["o", "h", "l", "c", "v"])
    def test_every_ohlcv_field_is_required(self, field):
        payload = _payload_with_bar()
        del payload["results"][0][field]
        with pytest.raises(Unavailable, match="finite positive number"):
            parse_aggregates(payload, symbol="AAPL")

    @pytest.mark.parametrize("field", ["o", "h", "l", "c", "v"])
    @pytest.mark.parametrize(
        "bad_value",
        [None, True, "not-a-number", float("nan"), float("inf"), float("-inf"), 0, -1],
    )
    def test_nonnumeric_nonfinite_and_nonpositive_ohlcv_is_refused(self, field, bad_value):
        with pytest.raises(Unavailable, match="finite positive number"):
            parse_aggregates(
                _payload_with_bar(**{field: bad_value}),
                symbol="AAPL",
            )

    @pytest.mark.parametrize(
        "updates",
        [
            {"l": 179.0},
            {"h": 178.2},
            {"l": 178.2, "h": 178.1},
        ],
    )
    def test_inconsistent_ohlc_ranges_are_refused(self, updates):
        with pytest.raises(Unavailable, match="inconsistent"):
            parse_aggregates(_payload_with_bar(**updates), symbol="AAPL")

    def test_one_bad_bar_refuses_the_batch_instead_of_returning_partial_coverage(self):
        payload = dict(AGGREGATES_PAYLOAD)
        payload["results"] = [
            dict(AGGREGATES_PAYLOAD["results"][0]),
            {**AGGREGATES_PAYLOAD["results"][1], "c": None},
        ]
        with pytest.raises(Unavailable, match="close"):
            parse_aggregates(payload, symbol="AAPL")


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

    async def test_a_malformed_bar_is_unfillable_and_writes_no_coverage(self, db):
        await db.pool.execute("TRUNCATE entity, demand CASCADE")
        owner = uuid4()
        entity_id = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name, identifiers) "
            "VALUES ('company', 'AAPL', 'Apple', $1::jsonb) RETURNING id",
            json.dumps({"polygon": "AAPL"}),
        )
        await db.pool.execute(
            "INSERT INTO gap (entity_id, claim_type, gap_class, audience_user_id, score) "
            "VALUES ($1, 'price_snapshot', 'missing', $2, 1.0)",
            entity_id,
            owner,
        )

        async def malformed(_symbol: str) -> dict:
            return _payload_with_bar(c=float("nan"))

        adapter = PolygonAdapter(fetch_fn=malformed)
        registry = Registry()
        registry.add(
            Capability(
                name="polygon.invalid-bar-test",
                description="Polygon invalid bar regression",
                produces=("price_snapshot",),
                entity_kinds=("company",),
                provider_key=adapter.provider_key,
                source=adapter.source,
                touches_byo=True,
                maturity=Maturity.WIRED,
                callability=Callability.YES,
                call=adapter.fetch,
            )
        )

        result = await run_once(db.pool, registry=registry, worker_id="polygon-test")
        assert result is not None
        assert result.outcome == "unfillable"
        assert result.claim_ids == []
        assert result.reason is not None and "finite positive number" in result.reason
        assert await db.pool.fetchval("SELECT count(*) FROM claim") == 0
