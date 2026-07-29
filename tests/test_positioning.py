"""Positioning perception adapter -- Polygon options snapshot.

`perception_positioning` folds a listed options chain into put/call ratios and
an IV skew. The fixture is the shape Polygon's ``/v3/snapshot/options/{ticker}``
endpoint returns: each result carries ``details.contract_type``, ``open_interest``,
``day.volume``, ``implied_volatility`` and a millisecond ``last_quote.last_updated``;
a top-level ``status`` where ``ERROR`` arrives over HTTP 200.

Polygon is the closest catalog provider that actually serves options data. Short
interest (FMP has the endpoint, no adapter) and ETF flows (no catalog provider)
are named gaps -- see the module docstring and the report. No endpoint is
invented; the licence class is asserted at the bottom because that is the rule
most likely to be broken by accident.
"""

from datetime import UTC, datetime

import pytest

from omni.credentials.catalog import redistribution_for
from omni.ingest.positioning import PositioningAdapter, parse_options_snapshot
from omni.ingest.protocol import Unavailable


def _contract(
    ctype: str,
    strike: float,
    expiration: str,
    *,
    oi: int,
    volume: int,
    iv: float | None,
    last_updated_ms: int | None,
    underlying: str = "AAPL",
) -> dict:
    contract = {
        "break_even_price": strike + 2.5,
        "day": {
            "change": 0.5,
            "change_percent": 1.2,
            "close": 3.1,
            "high": 3.4,
            "low": 2.9,
            "open": 2.95,
            "previous_close": 2.6,
            "volume": volume,
            "vwap": 3.05,
        },
        "details": {
            "contract_type": ctype,
            "exercise_style": "american",
            "expiration_date": expiration,
            "shares_per_contract": 100,
            "strike_price": strike,
        },
        "greeks": {"delta": 0.4, "gamma": 0.02, "theta": -0.1, "vega": 0.15},
        "implied_volatility": iv,
        "open_interest": oi,
        "underlying_ticker": underlying,
    }
    if last_updated_ms is not None:
        contract["last_quote"] = {
            "ask": 3.2,
            "ask_size": 100,
            "bid": 3.0,
            "bid_size": 90,
            "last_updated": last_updated_ms,
            "midpoint": 3.1,
            "timeframe": "REAL-TIME",
        }
    return contract


# Timestamps are real ms epochs:
#   1711926000000 -> 2024-03-31T23:00:00Z
#   1711929600000 -> 2024-04-01T00:00:00Z
#   1711933200000 -> 2024-04-01T01:00:00Z  (freshest -> event_date)
SNAPSHOT_PAYLOAD = {
    "status": "OK",
    "count": 4,
    "request_id": "req1",
    "results": [
        _contract(
            "call", 170.0, "2024-04-19", oi=1000, volume=100, iv=0.28, last_updated_ms=1711929600000
        ),
        _contract(
            "call", 175.0, "2024-04-19", oi=2000, volume=200, iv=0.30, last_updated_ms=1711926000000
        ),
        _contract(
            "put", 170.0, "2024-04-19", oi=1500, volume=50, iv=0.40, last_updated_ms=1711933200000
        ),
        _contract(
            "put", 165.0, "2024-04-19", oi=500, volume=10, iv=0.42, last_updated_ms=1711929600000
        ),
    ],
}


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


class TestParsing:
    def test_one_summary_draft_per_snapshot(self):
        drafts = parse_options_snapshot(SNAPSHOT_PAYLOAD, symbol="AAPL")
        assert len(drafts) == 1

    def test_put_call_ratios_fold_the_whole_chain(self):
        # puts: oi 1500+500=2000, vol 50+10=60 ; calls: oi 3000, vol 300
        (draft,) = parse_options_snapshot(SNAPSHOT_PAYLOAD, symbol="AAPL")
        assert draft.value["put_call_oi_ratio"] == pytest.approx(2000 / 3000)
        assert draft.value["put_call_volume_ratio"] == pytest.approx(60 / 300)

    def test_iv_skew_is_put_mean_minus_call_mean(self):
        (draft,) = parse_options_snapshot(SNAPSHOT_PAYLOAD, symbol="AAPL")
        assert draft.value["call_iv_mean"] == pytest.approx(0.29)
        assert draft.value["put_iv_mean"] == pytest.approx(0.41)
        assert draft.value["iv_skew"] == pytest.approx(0.41 - 0.29)
        assert draft.value["iv_skew"] > 0  # puts pricier than calls = bearish skew

    def test_event_date_is_the_freshest_quote_timestamp(self):
        (draft,) = parse_options_snapshot(SNAPSHOT_PAYLOAD, symbol="AAPL")
        assert draft.event_date == _at("2024-04-01T01:00:00")

    def test_knowledge_date_is_the_event_date_not_wall_clock(self):
        """A real-time quote is knowable at the instant it prints, so the bound
        is the quote time itself -- never ``datetime.now()``."""
        (draft,) = parse_options_snapshot(SNAPSHOT_PAYLOAD, symbol="AAPL")
        assert draft.knowledge_date == draft.event_date
        assert draft.knowledge_date == _at("2024-04-01T01:00:00")
        assert draft.knowledge_date != datetime.now(UTC)

    def test_confidence_is_full_when_every_signal_computed(self):
        (draft,) = parse_options_snapshot(SNAPSHOT_PAYLOAD, symbol="AAPL")
        assert draft.confidence == pytest.approx(1.0)

    def test_claim_type_and_key_target_the_entity(self):
        (draft,) = parse_options_snapshot(SNAPSHOT_PAYLOAD, symbol="AAPL")
        assert draft.claim_type == "perception_positioning"
        assert draft.key == "AAPL"

    def test_contracts_observed_counts_every_result(self):
        (draft,) = parse_options_snapshot(SNAPSHOT_PAYLOAD, symbol="AAPL")
        assert draft.value["contracts_observed"] == 4

    def test_expirations_are_recorded_in_evidence(self):
        (draft,) = parse_options_snapshot(SNAPSHOT_PAYLOAD, symbol="AAPL")
        assert draft.evidence["underlying"] == "AAPL"
        assert draft.evidence["expirations"] == ["2024-04-19"]
        assert draft.evidence["source_endpoint"] == "v3/snapshot/options"

    def test_a_polygon_error_at_http_200_raises_unavailable(self):
        with pytest.raises(Unavailable, match="ERROR"):
            parse_options_snapshot(
                {"status": "ERROR", "error": "Invalid ticker"},
                symbol="AAPL",
            )

    def test_an_empty_chain_returns_no_drafts_rather_than_raising(self):
        empty = {"status": "OK", "count": 0, "results": []}
        assert parse_options_snapshot(empty, symbol="AAPL") == []

    def test_no_quote_timestamp_returns_nothing_not_a_guessed_date(self):
        """Without a quote timestamp the bitemporal guarantee cannot be made.
        Substituting ``now()`` would let a backtest read a state whose time is
        unknown -- so nothing is written."""
        stripped = {
            "status": "OK",
            "results": [
                {k: v for k, v in c.items() if k != "last_quote"}
                for c in SNAPSHOT_PAYLOAD["results"]
            ],
        }
        assert parse_options_snapshot(stripped, symbol="AAPL") == []

    def test_a_chain_that_reports_nothing_returns_nothing(self):
        """Every contract exists but has zero OI, zero volume and no IV. The
        ratios are all uncomputable, so no claim is written rather than one
        whose value is entirely null."""
        inert = {
            "status": "OK",
            "results": [
                _contract(
                    "call",
                    170.0,
                    "2024-04-19",
                    oi=0,
                    volume=0,
                    iv=None,
                    last_updated_ms=1711929600000,
                ),
                _contract(
                    "put",
                    170.0,
                    "2024-04-19",
                    oi=0,
                    volume=0,
                    iv=None,
                    last_updated_ms=1711929600000,
                ),
            ],
        }
        assert parse_options_snapshot(inert, symbol="AAPL") == []

    def test_missing_iv_lowers_confidence_without_discarding_the_claim(self):
        no_iv = {
            "status": "OK",
            "results": [
                _contract(
                    "call",
                    170.0,
                    "2024-04-19",
                    oi=1000,
                    volume=100,
                    iv=None,
                    last_updated_ms=1711929600000,
                ),
                _contract(
                    "put",
                    170.0,
                    "2024-04-19",
                    oi=500,
                    volume=50,
                    iv=None,
                    last_updated_ms=1711929600000,
                ),
            ],
        }
        (draft,) = parse_options_snapshot(no_iv, symbol="AAPL")
        assert draft.value["iv_skew"] is None
        # OI and volume ratios computable, IV not -> 2 of 3 signals present.
        assert draft.confidence == pytest.approx(2 / 3)

    def test_a_contract_without_a_type_is_observed_but_not_tallied(self):
        """Polygon occasionally returns contracts whose details lack a type; they
        must not leak into either the call or put totals."""
        untyped = {
            "details": {
                "expiration_date": "2024-04-19",
                "strike_price": 180.0,
                "exercise_style": "american",
            },
            "open_interest": 99999,
            "day": {"volume": 99999},
            "implied_volatility": 0.99,
            "last_quote": {"last_updated": 1711929600000},
        }
        payload = {"status": "OK", "results": SNAPSHOT_PAYLOAD["results"] + [untyped]}
        (draft,) = parse_options_snapshot(payload, symbol="AAPL")
        assert draft.value["contracts_observed"] == 5
        # Ratios unchanged from the typed four.
        assert draft.value["put_call_oi_ratio"] == pytest.approx(2000 / 3000)


class TestAdapter:
    async def test_an_injected_fetcher_needs_no_network_or_key(self):
        async def fake(underlying: str) -> dict:
            assert underlying == "AAPL"
            return SNAPSHOT_PAYLOAD

        drafts = await PositioningAdapter(fetch_fn=fake).fetch("AAPL")
        assert len(drafts) == 1
        assert drafts[0].claim_type == "perception_positioning"

    async def test_no_key_and_no_fetcher_is_unavailable_not_empty(self):
        with pytest.raises(Unavailable, match="no Polygon API key"):
            await PositioningAdapter().fetch("AAPL")

    async def test_a_source_error_propagates_as_unavailable(self):
        async def errors(underlying: str) -> dict:
            return {"status": "ERROR", "error": "Unknown asset"}

        with pytest.raises(Unavailable, match="ERROR"):
            await PositioningAdapter(fetch_fn=errors).fetch("AAPL")

    async def test_an_empty_response_yields_no_drafts(self):
        async def empty(underlying: str) -> dict:
            return {"status": "OK", "count": 0, "results": []}

        assert await PositioningAdapter(fetch_fn=empty).fetch("AAPL") == []

    async def test_a_broken_fetcher_propagates_not_swallows(self):
        async def broken(underlying: str) -> dict:
            raise Unavailable("Polygon returned HTTP 429 for options snapshot AAPL")

        with pytest.raises(Unavailable, match="429"):
            await PositioningAdapter(fetch_fn=broken).fetch("AAPL")

    def test_the_adapter_declares_its_provider_for_licence_lookup(self):
        adapter = PositioningAdapter()
        assert adapter.source == "polygon"
        assert adapter.provider_key == "polygon"


class TestLicence:
    """The adapter points at Polygon, whose catalog entry is ``byo_only``: a
    claim fetched with a user's key is visible to that user alone and never
    enters shared coverage. Asserting it here guards the redistribution rule the
    adapter relies on the writer to enforce."""

    def test_polygon_positioning_is_byo_only_without_a_licence(self):
        assert redistribution_for("polygon") == "byo_only"

    def test_an_operator_licence_promotes_polygon_to_allowed(self):
        assert redistribution_for("polygon", licensed=("polygon",)) == "allowed"

    def test_the_adapter_provider_key_is_the_one_the_catalog_vets(self):
        from omni.ingest.positioning import PROVIDER_KEY

        assert PROVIDER_KEY == "polygon"
        # The lookup must not raise -- an unknown key would mean the adapter
        # produces claims the writer cannot classify, silently bypassing the rule.
        assert redistribution_for(PROVIDER_KEY) == "byo_only"
