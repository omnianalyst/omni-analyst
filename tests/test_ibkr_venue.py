"""Tests for the IBKR venue adapter.

ib_async is lazily imported. Tests stub it so they run without the package.
"""

from __future__ import annotations

import sys
import types
from decimal import Decimal
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

# Stub ib_async before importing the adapter
if "ib_async" not in sys.modules:
    _mod = types.ModuleType("ib_async")
    _mod.IB = MagicMock
    _mod.Stock = MagicMock
    _mod.MarketOrder = MagicMock
    _mod.LimitOrder = MagicMock
    sys.modules["ib_async"] = _mod

from omni.venue.ibkr_venue import IBKRVenue
from omni.venue.protocol import MarketType, OrderKind, Side, TradeIntent, VenueUnavailable


class FakeIB:
    _connected = True
    _acct: ClassVar = ("U12345",)

    def isConnected(self): return self._connected
    def disconnect(self): self._connected = False
    def managedAccounts(self): return self._acct
    def positions(self): return []
    def reqMarketDataType(self, _): pass
    def reqMktData(self, *_a, **_k):
        t = MagicMock(); t.bid = 379.50; t.ask = 380.10; t.last = 380.00; return t
    def placeOrder(self, _c, _o):
        tr = MagicMock()
        tr.isDone.return_value = True
        f = MagicMock()
        f.execution.shares = Decimal(10)
        f.execution.price = Decimal("380.05")
        f.execution.commission = Decimal("0.35")
        f.execution.execId = "12345"
        f.execution.orderId = 999
        tr.fills = [f]
        return tr
    async def accountSummaryAsync(self, account=None):
        av = MagicMock(); av.tag = "TotalCashValue"; av.value = "25000.00"
        return [av]


def _intent(symbol="SPY", side=Side.BUY, qty="10"):
    return TradeIntent(
        venue="ibkr", symbol=symbol, side=side,
        market_type=MarketType.SPOT, quantity=Decimal(qty),
        reference_price=Decimal(380), order_kind=OrderKind.MARKET,
    )


def test_spot_returns_ticker():
    v = IBKRVenue(ib_client=FakeIB())
    assert v.symbol_for("SPY", MarketType.SPOT) == "SPY"


def test_perpetual_returns_none():
    v = IBKRVenue(ib_client=FakeIB())
    assert v.symbol_for("BTC", MarketType.PERPETUAL) is None


def test_capabilities():
    v = IBKRVenue(ib_client=FakeIB())
    c = v.capabilities
    assert not c.spot and not c.margin and not c.shorting and not c.perpetuals
    assert not c.limit_orders
    assert c.taker_fee_bps == Decimal("0.35")


async def test_market_buy():
    ib = FakeIB()
    ib.placeOrder = MagicMock(side_effect=AssertionError("must not place an order"))
    v = IBKRVenue(ib_client=ib, account="U12345")

    with pytest.raises(VenueUnavailable, match="unavailable"):
        await v.execute(_intent())

    ib.placeOrder.assert_not_called()


async def test_empty_positions():
    v = IBKRVenue(ib_client=FakeIB(), account="U12345")
    assert await v.positions() == []


async def test_balances():
    v = IBKRVenue(ib_client=FakeIB(), account="U12345")
    balances = await v.balances()
    assert len(balances) == 1
    assert balances[0].asset == "USD"
    assert balances[0].free == Decimal(25000)


def test_not_connected_raises():
    v = IBKRVenue(ib_client=None)
    with pytest.raises(VenueUnavailable):
        v._ensure_connected()
