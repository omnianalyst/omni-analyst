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
        if "orders" in url and method == "POST":
            return {"id": 999}
        if "orders" in url and method == "GET":
            return {"orders": [{"state": "Filled", "filledQuantity": 10,
                                "executionPrice": 380.05, "side": "Buy",
                                "symbol": "VTI"}]}
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
    async def test_market_buy_fills(self, venue):
        v, _fake = venue
        fill = await v.execute(_intent())
        assert fill.filled_quantity == Decimal(10)
        assert fill.average_price == Decimal("380.05")
        assert fill.side is Side.BUY
        assert fill.external_id == "999"


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
    def test_equity_only(self):
        v = QuestradeVenue(refresh_token="x", fetch_fn=FakeQuestrade())
        caps = v.capabilities
        assert caps.spot is True
        assert caps.perpetuals is False
        assert caps.shorting is True
        assert caps.taker_fee_bps == Decimal(0)
