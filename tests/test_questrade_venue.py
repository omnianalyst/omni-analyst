"""Tests for the Questrade venue adapter.

The adapter's HTTP layer is injectable, so every test controls the API
responses. No network access needed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from omni.venue.protocol import MarketType, OrderKind, Side, TradeIntent, VenueUnavailable
from omni.venue.questrade_venue import QuestradeVenue


class FakeQuestrade:
    """Records calls and returns scripted responses."""

    def __init__(self):
        self.calls: list[tuple] = []
        self._responses: dict[str, list[dict]] = {}
        self._symbol_ids: dict[str, int] = {"VTI": 12345, "SPY": 67890}

    def script(self, pattern: str, *responses: dict) -> None:
        self._responses[pattern] = list(responses)

    async def __call__(self, method: str, url: str, **kwargs) -> dict:
        self.calls.append((method, url, kwargs))
        for pattern, responses in self._responses.items():
            if pattern in url and responses:
                return responses.pop(0)
        # Default: token refresh
        if "oauth2/token" in url:
            return {
                "access_token": "test-token",
                "api_server": "https://api-test.questrade.com",
                "expires_in": 1800,
                "refresh_token": "new-refresh",
            }
        if "/accounts" in url and "balances" not in url and "positions" not in url and "orders" not in url:
            return {"accounts": [{"number": 111, "type": "Margin"}]}
        if "symbols/search" in url:
            ticker = url.split("prefix=")[-1]
            sid = self._symbol_ids.get(ticker)
            if sid is None:
                return {"symbols": []}
            return {"symbols": [{"symbol": ticker, "symbolId": sid}]}
        if "quotes" in url:
            return {"quotes": [{"bidPrice": 380.00, "askPrice": 380.10}]}
        if "balances" in url:
            return {"perCurrencyBalances": [{"currency": "CAD", "cash": 10000.00}]}
        if "positions" in url:
            return {"positions": []}
        return {}


def _intent(symbol="VTI", side=Side.BUY, qty="10", kind=OrderKind.MARKET) -> TradeIntent:
    return TradeIntent(
        venue="questrade",
        symbol=symbol,
        side=side,
        market_type=MarketType.SPOT,
        quantity=Decimal(qty),
        reference_price=Decimal(380),
        order_kind=kind,
    )


@pytest.fixture
async def venue():
    fake = FakeQuestrade()
    v = QuestradeVenue(refresh_token="test-refresh", practice=True, fetch_fn=fake)
    v._fetch = fake
    await v._refresh_access_token()
    await v._resolve_account()
    return v, fake


class TestConnect:
    async def test_connect_refreshes_token_and_resolves_account(self):
        fake = FakeQuestrade()
        v = await QuestradeVenue.connect(
            refresh_token="test-refresh",
            practice=True,
            fetch_fn=fake,
        )
        assert v._access_token == "test-token"
        assert v._api_server == "https://api-test.questrade.com"
        assert v._account_id == "111"

    async def test_rotated_refresh_token_is_persisted_and_used_after_restart(self):
        persisted = []

        async def persist(token):
            persisted.append(token)

        first = FakeQuestrade()
        await QuestradeVenue.connect(
            refresh_token="initial-refresh",
            fetch_fn=first,
            on_refresh_token=persist,
        )
        assert persisted == ["new-refresh"]
        token_call = next(call for call in first.calls if "oauth2/token" in call[1])
        assert "initial-refresh" not in token_call[1]
        assert token_call[2]["params"]["refresh_token"] == "initial-refresh"

        second = FakeQuestrade()
        await QuestradeVenue.connect(
            refresh_token=persisted[-1],
            fetch_fn=second,
            on_refresh_token=persist,
        )
        restarted_call = next(call for call in second.calls if "oauth2/token" in call[1])
        assert restarted_call[2]["params"]["refresh_token"] == "new-refresh"


class TestSymbolFor:
    def test_spot_returns_ticker(self):
        v = QuestradeVenue(refresh_token="x", fetch_fn=FakeQuestrade())
        assert v.symbol_for("VTI", MarketType.SPOT) == "VTI"

    def test_perpetual_returns_none(self):
        v = QuestradeVenue(refresh_token="x", fetch_fn=FakeQuestrade())
        assert v.symbol_for("BTC", MarketType.PERPETUAL) is None


class TestQuote:
    async def test_quote_returns_mid_and_spread(self, venue):
        v, _fake = venue
        q = await v.quote(_intent())
        assert q.expected_price == Decimal("380.05")
        assert q.slippage > 0
        assert q.fee == Decimal(0)

    async def test_quote_missing_symbol_raises(self, venue):
        v, _fake = venue
        with pytest.raises(VenueUnavailable, match="not found"):
            await v.quote(_intent(symbol="UNKNOWN"))


class TestExecute:
    async def test_order_submission_is_refused_before_any_order_request(self, venue):
        v, fake = venue
        before = list(fake.calls)

        with pytest.raises(VenueUnavailable, match="read-only"):
            await v.execute(_intent())

        assert fake.calls == before

    async def test_order_cancellation_is_refused_before_any_request(self, venue):
        v, fake = venue
        before = list(fake.calls)

        with pytest.raises(VenueUnavailable, match="read-only"):
            await v.cancel("999")

        assert fake.calls == before


class TestPositions:
    async def test_empty_positions(self, venue):
        v, _fake = venue
        positions = await v.positions()
        assert positions == []

    async def test_with_holding(self, venue):
        v, fake = venue
        fake.script("positions", {"positions": [
            {"symbol": "VTI", "openQuantity": 100, "averageEntryPrice": 370.50}
        ]})
        positions = await v.positions()
        assert len(positions) == 1
        assert positions[0].symbol == "VTI"
        assert positions[0].quantity == Decimal(100)
        assert positions[0].average_entry == Decimal("370.50")

    async def test_short_position_marked_margin(self, venue):
        v, fake = venue
        fake.script("positions", {"positions": [
            {"symbol": "SPY", "openQuantity": -50, "averageEntryPrice": 450.00}
        ]})
        positions = await v.positions()
        assert positions[0].quantity < 0
        assert positions[0].market_type == "margin"


class TestBalances:
    async def test_returns_cash(self, venue):
        v, _fake = venue
        balances = await v.balances()
        assert len(balances) == 1
        assert balances[0].asset == "CAD"
        assert balances[0].free == Decimal(10000)


class TestCapabilities:
    def test_read_only_adapter_declares_no_execution_market(self):
        v = QuestradeVenue(refresh_token="x", fetch_fn=FakeQuestrade())
        caps = v.capabilities
        assert caps.spot is False
        assert caps.margin is False
        assert caps.perpetuals is False
        assert caps.shorting is False
        assert caps.limit_orders is False
        assert caps.taker_fee_bps == Decimal(0)
