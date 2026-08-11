"""Tests for the active-markets fetcher and resolution lookup.

MockTransport throughout — same pattern as test_polymarket_gamma.py. The
active-market parser shares the strictness rules of the resolved-market
parser (refuses non-Yes/No, refuses ambiguous prices) and adds one of its
own: the current `yes_price` must be finite and in [0, 1].
"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from omni.ingest.protocol import Unavailable
from omni.polymarket.active import (
    ActiveMarket,
    fetch_current_resolution,
    list_active_markets,
)

UTC_ = UTC


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _active_payload(**overrides):
    base = {
        "id": "0x1",
        "question": "Will Bitcoin close above $100k on Dec 31?",
        "category": "Crypto",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.65", "0.35"]),
        "clobTokenIds": json.dumps(["tok-y", "tok-n"]),
        "negRisk": False,
        "slug": "btc-100k",
        "volume": "50000.0",
        "endDate": "2024-12-31T23:59:00Z",
    }
    base.update(overrides)
    return base


class TestListActiveMarkets:
    async def test_clean_payload_parsed(self):
        async with _client(lambda r: httpx.Response(200, json=[_active_payload()])) as c:
            markets = await list_active_markets(c, limit=10, fetched_at=datetime(2024, 12, 15, tzinfo=UTC_))
        assert len(markets) == 1
        m = markets[0]
        assert m.condition_id == "0x1"
        assert m.yes_price == pytest.approx(0.65)
        assert m.yes_token_id == "tok-y"
        assert m.fetched_at == datetime(2024, 12, 15, tzinfo=UTC_)

    async def test_non_yes_outcome_refused(self):
        payload = _active_payload(outcomes=json.dumps(["Over", "Under"]))
        async with _client(lambda r: httpx.Response(200, json=[payload])) as c:
            with pytest.raises(Unavailable, match="expected 'Yes'"):
                await list_active_markets(c, limit=10)

    async def test_strict_mode_raises(self):
        payload = _active_payload(outcomes=json.dumps(["Over", "Under"]))
        async with _client(lambda r: httpx.Response(200, json=[payload])) as c:
            with pytest.raises(Unavailable):
                await list_active_markets(c, limit=10, strict=True)

    async def test_lenient_mode_skips(self):
        skipped = []
        good = _active_payload()
        bad = _active_payload(id="0x2", outcomes=json.dumps(["Over", "Under"]))
        async with _client(lambda r: httpx.Response(200, json=[good, bad])) as c:
            markets = await list_active_markets(
                c, limit=10, strict=False,
                on_skip=lambda raw, exc: skipped.append((raw.get("id"), str(exc))),
            )
        assert len(markets) == 1
        assert markets[0].condition_id == "0x1"
        assert len(skipped) == 1
        assert skipped[0][0] == "0x2"

    async def test_out_of_range_price_refused(self):
        payload = _active_payload(outcomePrices=json.dumps(["1.5", "-0.5"]))
        async with _client(lambda r: httpx.Response(200, json=[payload])) as c:
            with pytest.raises(Unavailable, match="out of \\[0, 1\\]"):
                await list_active_markets(c, limit=10)

    async def test_http_error_raises_unavailable(self):
        async with _client(lambda r: httpx.Response(503)) as c:
            with pytest.raises(Unavailable, match="Gamma"):
                await list_active_markets(c, limit=10)

    async def test_category_filter(self):
        payload = [
            _active_payload(category="Crypto"),
            _active_payload(id="0x2", category="Politics"),
        ]
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            markets = await list_active_markets(c, limit=10, categories=["crypto"])
        assert len(markets) == 1
        assert markets[0].category == "Crypto"


class TestFetchCurrentResolution:
    async def test_open_market_returns_none(self):
        # Active market with mid-range price => unresolved.
        payload = _active_payload(outcomePrices=json.dumps(["0.65", "0.35"]), closed=False)
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            resolved_yes, price = await fetch_current_resolution(c, condition_id="0x1")
        assert resolved_yes is None
        assert price == pytest.approx(0.65)

    async def test_resolved_yes_detected(self):
        payload = _active_payload(outcomePrices=json.dumps(["1.0", "0.0"]), closed=True)
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            resolved_yes, _ = await fetch_current_resolution(c, condition_id="0x1")
        assert resolved_yes is True

    async def test_resolved_no_detected(self):
        payload = _active_payload(outcomePrices=json.dumps(["0.0", "1.0"]), closed=True)
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            resolved_yes, _ = await fetch_current_resolution(c, condition_id="0x1")
        assert resolved_yes is False

    async def test_extreme_price_without_close_treated_as_open(self):
        # Price at 1.0 but closed=False => still open, not resolution.
        payload = _active_payload(outcomePrices=json.dumps(["1.0", "0.0"]), closed=False)
        async with _client(lambda r: httpx.Response(200, json=payload)) as c:
            resolved_yes, _ = await fetch_current_resolution(c, condition_id="0x1")
        assert resolved_yes is None

    async def test_http_error_raises(self):
        async with _client(lambda r: httpx.Response(500)) as c:
            with pytest.raises(Unavailable):
                await fetch_current_resolution(c, condition_id="0x1")


class TestActiveMarketValidation:
    def test_clean_constructs(self):
        m = ActiveMarket(
            condition_id="0x1", question="q", category="c",
            yes_token_id="t", no_token_id=None,
            yes_price=0.5, neg_risk=False, slug="", volume=100.0,
            end_date=None, fetched_at=datetime(2024, 1, 1, tzinfo=UTC_),
        )
        assert m.yes_price == 0.5

    def test_naive_fetched_at_refused(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            ActiveMarket(
                condition_id="0x1", question="q", category="c",
                yes_token_id="t", no_token_id=None,
                yes_price=0.5, neg_risk=False, slug="", volume=100.0,
                end_date=None, fetched_at=datetime(2024, 1, 1),  # noqa: DTZ001
            )

    def test_nan_price_refused(self):
        with pytest.raises(ValueError, match="finite"):
            ActiveMarket(
                condition_id="0x1", question="q", category="c",
                yes_token_id="t", no_token_id=None,
                yes_price=float("nan"), neg_risk=False, slug="", volume=100.0,
                end_date=None, fetched_at=datetime(2024, 1, 1, tzinfo=UTC_),
            )
