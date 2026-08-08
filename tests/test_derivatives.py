"""Derivatives adapter: funding rate, open interest, liquidations.

Fixtures are recorded excerpts of the real Binance response shapes:

* funding -- `GET /fapi/v1/fundingRate`: a list of settled intervals, each a
  signed decimal ``fundingRate`` string at a ``fundingTime`` settlement stamp.
  The shape and the sign convention (positive = longs pay shorts) are the ones
  documented at https://binance-docs.github.io/apidocs/futures/en/ and stated
  by the work order; they were not researched over the network.
* open interest -- `GET /futures/data/openInterestHist`: contract quantity
  (``sumOpenInterest``) and notional (``sumOpenInterestValue``) per snapshot.
* liquidations -- `GET /fapi/v1/forceOrders`: one forced order per event with
  ``side``, ``origQty`` and ``time``.

All tests run against recorded payloads through pure parse functions and an
injected ``fetch_fn`` -- no network. The load-bearing assertions are: the
funding sign is preserved unchanged, ``knowledge_date == event_date`` for every
claim, and a zero rate is emitted rather than dropped as falsy.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from omni.ingest.derivatives import (
    DerivativesAdapter,
    parse_funding,
    parse_liquidations,
    parse_open_interest,
)
from omni.ingest.protocol import Unavailable

# 1698768000000 ms = 2023-10-31T12:00:00Z. Settlements are 8h (28800000 ms)
# apart, matching Binance's funding cadence.
FUNDING_TS = [1698768000000, 1698796800000, 1698825600000, 1698854400000]

# The real `GET /fapi/v1/fundingRate` shape, from the work order's documented
# fixture. Five intervals: a positive rate, a negative rate, a zero rate, and
# two malformed entries (missing timestamp, unparseable rate) that must be
# skipped rather than emitted with a substituted value.
FUNDING_PAYLOAD = [
    {
        "symbol": "BTCUSDT",
        "fundingTime": FUNDING_TS[0],
        "fundingRate": "0.00010000",
        "markPrice": "34500.00000000",
    },
    {
        "symbol": "BTCUSDT",
        "fundingTime": FUNDING_TS[1],
        "fundingRate": "-0.00005000",
        "markPrice": "34400.00000000",
    },
    {
        "symbol": "BTCUSDT",
        "fundingTime": FUNDING_TS[2],
        "fundingRate": "0.00000000",
        "markPrice": "34450.00000000",
    },
    # missing settlement time -> no event_date -> skipped
    {
        "symbol": "BTCUSDT",
        "fundingTime": None,
        "fundingRate": "0.0001",
        "markPrice": "34600.00000000",
    },
    # unparseable rate -> skipped, never substituted with zero
    {
        "symbol": "BTCUSDT",
        "fundingTime": FUNDING_TS[3],
        "fundingRate": "not-a-number",
        "markPrice": "34700.00000000",
    },
]

# `GET /futures/data/openInterestHist` shape. Two valid snapshots -- one with a
# notional, one where the venue omitted it -- and one malformed (no contracts).
OI_PAYLOAD = [
    {
        "symbol": "BTCUSDT",
        "sumOpenInterest": "106999.091",
        "sumOpenInterestValue": "3689345789.12",
        "timestamp": FUNDING_TS[0],
    },
    {
        "symbol": "BTCUSDT",
        "sumOpenInterest": "107500.000",
        "sumOpenInterestValue": None,
        "timestamp": FUNDING_TS[1],
    },
    # missing contracts -> skipped
    {
        "symbol": "BTCUSDT",
        "sumOpenInterest": None,
        "sumOpenInterestValue": "100.0",
        "timestamp": FUNDING_TS[2],
    },
]

# `GET /fapi/v1/forceOrders` shape. Two valid liquidations (a SELL closing a
# long, a BUY closing a short) and one missing its size.
LIQ_PAYLOAD = [
    {
        "orderId": 901,
        "symbol": "BTCUSDT",
        "side": "SELL",
        "price": "34500.00",
        "origQty": "1.50000",
        "time": FUNDING_TS[0],
    },
    {
        "orderId": 902,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "price": "34600.00",
        "origQty": "0.80000",
        "time": FUNDING_TS[1],
    },
    # missing size -> skipped
    {
        "orderId": 903,
        "symbol": "BTCUSDT",
        "side": "SELL",
        "price": "34400.00",
        "origQty": None,
        "time": FUNDING_TS[2],
    },
]


def _at_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class TestParseFunding:
    def test_one_claim_per_settlement(self):
        drafts = parse_funding(FUNDING_PAYLOAD, symbol="BTCUSDT", venue="binance")
        assert len(drafts) == 3
        assert {d.event_date for d in drafts} == {
            _at_ms(FUNDING_TS[0]),
            _at_ms(FUNDING_TS[1]),
            _at_ms(FUNDING_TS[2]),
        }

    def test_malformed_entries_are_skipped_not_substituted(self):
        drafts = parse_funding(FUNDING_PAYLOAD, symbol="BTCUSDT", venue="binance")
        # The missing-timestamp and unparseable-rate entries are gone, and no
        # claim landed at the 4th settlement stamp.
        assert len(drafts) == 3
        assert all(d.event_date != _at_ms(FUNDING_TS[3]) for d in drafts)
        assert all(d.value["rate"] != "0" for d in drafts)

    def test_knowledge_date_equals_event_date_for_every_settlement(self):
        drafts = parse_funding(FUNDING_PAYLOAD, symbol="BTCUSDT", venue="binance")
        for d in drafts:
            assert d.knowledge_date == d.event_date
        assert drafts[0].knowledge_date == _at_ms(FUNDING_TS[0])

    def test_a_positive_rate_stays_positive(self):
        drafts = parse_funding(FUNDING_PAYLOAD, symbol="BTCUSDT", venue="binance")
        positive = next(
            d for d in drafts if d.event_date == _at_ms(FUNDING_TS[0])
        )
        # positive = longs pay shorts, the venue's own sign, preserved.
        assert Decimal(positive.value["rate"]) > Decimal(0)

    def test_a_negative_rate_stays_negative(self):
        drafts = parse_funding(FUNDING_PAYLOAD, symbol="BTCUSDT", venue="binance")
        negative = next(
            d for d in drafts if d.event_date == _at_ms(FUNDING_TS[1])
        )
        assert Decimal(negative.value["rate"]) < Decimal(0)

    def test_a_zero_rate_is_emitted_not_dropped(self):
        drafts = parse_funding(FUNDING_PAYLOAD, symbol="BTCUSDT", venue="binance")
        zero = next(
            d for d in drafts if d.event_date == _at_ms(FUNDING_TS[2])
        )
        # Zero is a real, common funding observation. It must produce a claim,
        # not be filtered out as falsy or as a float-equals-zero edge case.
        assert Decimal(zero.value["rate"]) == Decimal(0)

    def test_the_rate_is_decimal_faithful_not_float_rounded(self):
        # str(Decimal("0.00010000")) round-trips the venue's exact scale, so a
        # backtest summing hundreds of these never accumulates float error.
        drafts = parse_funding(FUNDING_PAYLOAD, symbol="BTCUSDT", venue="binance")
        assert drafts[0].value["rate"] == "0.00010000"

    def test_the_claim_type_and_key(self):
        drafts = parse_funding(FUNDING_PAYLOAD, symbol="BTCUSDT", venue="binance")
        assert all(d.claim_type == "funding_rate" for d in drafts)
        assert all(d.key == "binance:BTCUSDT" for d in drafts)
        assert all(d.unit == "rate" for d in drafts)

    def test_an_empty_valid_window_returns_empty_not_unavailable(self):
        assert parse_funding([], symbol="BTCUSDT", venue="binance") == []

    def test_a_throttle_body_raises_unavailable(self):
        with pytest.raises(Unavailable, match="rate-limited"):
            parse_funding(
                {"code": -1003, "msg": "request weight"},
                symbol="BTCUSDT",
                venue="binance",
            )

    def test_mark_price_is_not_treated_as_a_funding_observation(self):
        drafts = parse_funding(FUNDING_PAYLOAD, symbol="BTCUSDT", venue="binance")
        for d in drafts:
            assert "markPrice" not in d.value


class TestParseOpenInterest:
    def test_one_claim_per_snapshot(self):
        drafts = parse_open_interest(
            OI_PAYLOAD, symbol="BTCUSDT", venue="binance"
        )
        assert len(drafts) == 2

    def test_knowledge_date_equals_event_date(self):
        drafts = parse_open_interest(
            OI_PAYLOAD, symbol="BTCUSDT", venue="binance"
        )
        for d in drafts:
            assert d.knowledge_date == d.event_date
        assert drafts[0].event_date == _at_ms(FUNDING_TS[0])

    def test_contracts_and_notional_are_separate_fields_neither_substituted(self):
        drafts = parse_open_interest(
            OI_PAYLOAD, symbol="BTCUSDT", venue="binance"
        )
        first = drafts[0]
        # Both present on the first snapshot, as distinct fields carrying
        # distinct quantities -- a contract count and a dollar notional.
        assert "contracts" in first.value
        assert "notional" in first.value
        assert first.value["contracts"] != first.value["notional"]
        assert Decimal(first.value["contracts"]) == Decimal("106999.091")
        assert Decimal(first.value["notional"]) == Decimal("3689345789.12")

    def test_a_missing_notional_is_absent_not_guessed_from_contracts(self):
        drafts = parse_open_interest(
            OI_PAYLOAD, symbol="BTCUSDT", venue="binance"
        )
        second = next(
            d for d in drafts if d.event_date == _at_ms(FUNDING_TS[1])
        )
        assert "notional" not in second.value
        # contracts still present; nothing was substituted into the notional slot.
        assert Decimal(second.value["contracts"]) == Decimal("107500.000")

    def test_a_snapshot_missing_contracts_is_skipped(self):
        drafts = parse_open_interest(
            OI_PAYLOAD, symbol="BTCUSDT", venue="binance"
        )
        assert all(d.event_date != _at_ms(FUNDING_TS[2]) for d in drafts)
        assert len(drafts) == 2

    def test_the_claim_type_and_key(self):
        drafts = parse_open_interest(
            OI_PAYLOAD, symbol="BTCUSDT", venue="binance"
        )
        assert all(d.claim_type == "open_interest" for d in drafts)
        assert all(d.key == "binance:BTCUSDT" for d in drafts)

    def test_an_empty_window_returns_empty_not_unavailable(self):
        assert parse_open_interest([], symbol="BTCUSDT", venue="binance") == []

    def test_a_throttle_body_raises_unavailable(self):
        with pytest.raises(Unavailable, match="rate-limited"):
            parse_open_interest(
                {"code": -1015, "msg": "too many requests"},
                symbol="BTCUSDT",
                venue="binance",
            )


class TestParseLiquidations:
    def test_one_claim_per_event_with_side_and_size(self):
        drafts = parse_liquidations(LIQ_PAYLOAD, symbol="BTCUSDT", venue="binance")
        assert len(drafts) == 2
        by_side = {d.value["side"]: d for d in drafts}
        assert Decimal(by_side["SELL"].value["size"]) == Decimal("1.50000")
        assert Decimal(by_side["BUY"].value["size"]) == Decimal("0.80000")

    def test_knowledge_date_equals_event_date(self):
        drafts = parse_liquidations(LIQ_PAYLOAD, symbol="BTCUSDT", venue="binance")
        for d in drafts:
            assert d.knowledge_date == d.event_date

    def test_the_side_is_preserved_as_the_venue_publishes_it(self):
        drafts = parse_liquidations(LIQ_PAYLOAD, symbol="BTCUSDT", venue="binance")
        assert {d.value["side"] for d in drafts} == {"BUY", "SELL"}

    def test_an_event_missing_size_is_skipped_not_zeroed(self):
        drafts = parse_liquidations(LIQ_PAYLOAD, symbol="BTCUSDT", venue="binance")
        assert all(d.event_date != _at_ms(FUNDING_TS[2]) for d in drafts)
        assert len(drafts) == 2

    def test_each_event_has_a_distinct_key(self):
        drafts = parse_liquidations(LIQ_PAYLOAD, symbol="BTCUSDT", venue="binance")
        # Two distinct liquidations must not collapse onto one key.
        assert len({d.key for d in drafts}) == 2

    def test_the_claim_type(self):
        drafts = parse_liquidations(LIQ_PAYLOAD, symbol="BTCUSDT", venue="binance")
        assert all(d.claim_type == "liquidation_event" for d in drafts)

    def test_an_empty_window_returns_empty_not_unavailable(self):
        assert parse_liquidations([], symbol="BTCUSDT", venue="binance") == []

    def test_a_throttle_body_raises_unavailable(self):
        with pytest.raises(Unavailable, match="rate-limited"):
            parse_liquidations(
                {"code": -1003, "msg": "request weight"},
                symbol="BTCUSDT",
                venue="binance",
            )


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def get(self, url: str, params=None, headers=None) -> _FakeResponse:
        return self._response


class TestAdapterRouting:
    async def test_funding_route_uses_an_injected_payload(self):
        async def fake(key: str):
            assert key == "funding:BTCUSDT"
            return FUNDING_PAYLOAD

        drafts = await DerivativesAdapter(fetch_fn=fake).fetch("funding:BTCUSDT")
        assert len(drafts) == 3
        assert all(d.claim_type == "funding_rate" for d in drafts)

    async def test_oi_route_uses_an_injected_payload(self):
        async def fake(key: str):
            assert key == "oi:BTCUSDT"
            return OI_PAYLOAD

        drafts = await DerivativesAdapter(fetch_fn=fake).fetch("oi:BTCUSDT")
        assert len(drafts) == 2
        assert all(d.claim_type == "open_interest" for d in drafts)

    async def test_liq_route_uses_an_injected_payload(self):
        async def fake(key: str):
            assert key == "liq:BTCUSDT"
            return LIQ_PAYLOAD

        drafts = await DerivativesAdapter(fetch_fn=fake).fetch("liq:BTCUSDT")
        assert len(drafts) == 2
        assert all(d.claim_type == "liquidation_event" for d in drafts)

    async def test_an_empty_valid_window_returns_empty_through_the_adapter(self):
        async def empty(key: str):
            return []

        for route in ("funding:BTCUSDT", "oi:BTCUSDT", "liq:BTCUSDT"):
            assert await DerivativesAdapter(fetch_fn=empty).fetch(route) == []

    async def test_an_unknown_kind_raises_unavailable(self):
        async def fake(key: str):
            return []

        with pytest.raises(Unavailable, match="unknown derivatives kind"):
            await DerivativesAdapter(fetch_fn=fake).fetch("basis:BTCUSDT")

    async def test_a_key_without_a_kind_separator_raises_unavailable(self):
        with pytest.raises(Unavailable, match="must be"):
            await DerivativesAdapter(fetch_fn=None).fetch("BTCUSDT")

    async def test_a_fetch_failure_propagates_as_unavailable(self):
        async def broken(key: str):
            raise Unavailable("binance returned HTTP 429 for funding")

        with pytest.raises(Unavailable, match="429"):
            await DerivativesAdapter(fetch_fn=broken).fetch("funding:BTCUSDT")

    async def test_a_throttle_body_propagates_through_the_adapter(self):
        async def throttled(key: str):
            return {"code": -1003, "msg": "request weight"}

        with pytest.raises(Unavailable, match="rate-limited"):
            await DerivativesAdapter(fetch_fn=throttled).fetch("funding:BTCUSDT")

    async def test_http_429_from_the_default_fetcher_is_unavailable_not_empty(self):
        # The default fetcher (no fetch_fn) classifies an HTTP 429 status as a
        # refusal to answer. Driven through an injected session so no network.
        adapter = DerivativesAdapter(session=_FakeSession(_FakeResponse(429, {})))
        with pytest.raises(Unavailable, match="429"):
            await adapter.fetch("funding:BTCUSDT")

    async def test_a_non_200_status_from_the_default_fetcher_is_unavailable(self):
        adapter = DerivativesAdapter(
            session=_FakeSession(_FakeResponse(500, {"code": -1}))
        )
        with pytest.raises(Unavailable, match="500"):
            await adapter.fetch("oi:BTCUSDT")


class TestAdapterDeclaration:
    def test_the_adapter_declares_binance_for_licence_lookup(self):
        adapter = DerivativesAdapter()
        assert adapter.source == "derivatives"
        assert adapter.provider_key == "binance"

    async def test_default_venue_is_binance(self):
        async def fake(key: str):
            return FUNDING_PAYLOAD

        drafts = await DerivativesAdapter(fetch_fn=fake).fetch("funding:BTCUSDT")
        # The venue lands in the claim value, confirming the default routed to
        # binance rather than an unset/empty venue.
        assert all(d.value["venue"] == "binance" for d in drafts)
