"""Interactive Brokers venue adapter — full equity trading via IB Gateway.

Connects to a local IB Gateway process (managed by Docker, see settings)
using the ib_async library (maintained fork of ib_insync). The Gateway
handles the socket protocol to IBKR's servers; this adapter speaks to it
over localhost.

**Architecture:**
    Settings → enable IBKR → Docker starts IB Gateway container →
    this adapter connects to localhost:4001 (paper) or 4002 (live) →
    Venue protocol methods proxy through ib_async.

**Paper vs Live:** Paper trading (port 4001/4003) requires no 2FA and is
recommended for always-on automation. Live trading (port 4002/4004) requires
2FA on cold start. The Docker image handles auto-restart and re-auth.

**Dependencies:** ib_async (install with `pip install ib_async`). Lazily
imported so the venue module loads without it installed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from omni.venue.protocol import (
    Balance,
    Capabilities,
    Fill,
    MarketType,
    OrderKind,
    Position,
    Quote,
    Side,
    TradeIntent,
    Venue,
    VenueUnavailable,
)

logger = logging.getLogger("omni.venue.ibkr")

PAPER_PORT = 4002
LIVE_PORT = 4001


class IBKRVenue(Venue):
    """Equity venue backed by Interactive Brokers via IB Gateway."""

    name = "ibkr"

    def __init__(
        self,
        *,
        ib_client: Any | None = None,
        account: str | None = None,
        host: str = "127.0.0.1",
        port: int = PAPER_PORT,
        client_id: int = 1,
    ) -> None:
        self._ib = ib_client
        self._account = account
        self._host = host
        self._port = port
        self._client_id = client_id
        self._capabilities = Capabilities(
            spot=True,
            margin=True,
            perpetuals=False,
            limit_orders=True,
            shorting=True,
            funding_data=False,
            maker_fee_bps=Decimal(0),
            taker_fee_bps=Decimal("0.35"),
            min_notional=Decimal(1),
        )

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    @classmethod
    async def connect(
        cls,
        *,
        host: str = "127.0.0.1",
        port: int = PAPER_PORT,
        client_id: int = 1,
        account: str | None = None,
    ) -> IBKRVenue:
        try:
            from ib_async import IB
        except ImportError as exc:
            raise VenueUnavailable(
                "ib_async not installed. Run: pip install ib_async"
            ) from exc

        ib = IB()
        try:
            await ib.connectAsync(host, port, clientId=client_id)
        except Exception as exc:
            raise VenueUnavailable(
                f"cannot connect to IB Gateway at {host}:{port}: {exc}"
            ) from exc

        venue = cls(ib_client=ib, account=account, host=host, port=port, client_id=client_id)
        if account is None:
            await venue._resolve_account()
        logger.info("ibkr connected (port=%d, account=%s)", port, venue._account)
        return venue

    async def aclose(self) -> None:
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()

    def _ensure_connected(self) -> None:
        if not self._ib:
            raise VenueUnavailable("ibkr: not connected")
        if hasattr(self._ib, "isConnected") and not self._ib.isConnected():
            raise VenueUnavailable("ibkr: connection lost")

    # --- Venue protocol ---

    def symbol_for(self, asset: str, market_type: MarketType) -> str | None:
        if market_type not in (MarketType.SPOT, MarketType.MARGIN):
            return None
        return asset

    async def quote(self, intent: TradeIntent) -> Quote:
        self._ensure_connected()
        contract = self._contract(intent.symbol)
        self._ib.reqMarketDataType(1)  # 1=live, 3=delayed
        ticker = self._ib.reqMktData(contract, "", False, False)
        await asyncio.sleep(2)
        if ticker.bid is None or ticker.ask is None:
            if ticker.last is not None and ticker.last > 0:
                mid = Decimal(str(ticker.last))
                return Quote(
                    intent=intent, expected_price=mid,
                    fee=Decimal(0), slippage=Decimal(0), gas=Decimal(0),
                    as_of=datetime.now(UTC),
                )
            raise VenueUnavailable(f"ibkr: no quote for {intent.symbol}")
        bid = Decimal(str(ticker.bid))
        ask = Decimal(str(ticker.ask))
        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * Decimal(10000) if mid > 0 else Decimal(0)
        return Quote(
            intent=intent, expected_price=mid,
            fee=Decimal(0), slippage=spread_bps / 2, gas=Decimal(0),
            as_of=datetime.now(UTC),
        )

    async def execute(self, intent: TradeIntent) -> Fill:
        self._ensure_connected()
        from ib_async import LimitOrder as IBLimitOrder
        from ib_async import MarketOrder as IBMarketOrder

        contract = self._contract(intent.symbol)
        action = "BUY" if intent.side is Side.BUY else "SELL"
        qty = float(intent.quantity)

        if intent.order_kind is OrderKind.LIMIT:
            assert intent.limit_price is not None
            order = IBLimitOrder(action, qty, float(intent.limit_price))
        else:
            order = IBMarketOrder(action, qty)

        trade = self._ib.placeOrder(contract, order)
        deadline = asyncio.get_event_loop().time() + 30
        while not trade.isDone():
            if asyncio.get_event_loop().time() > deadline:
                raise VenueUnavailable(
                    f"ibkr order for {intent.symbol} did not fill in 30s"
                )
            await asyncio.sleep(0.5)

        fill_data = trade.fills[-1] if trade.fills else None
        if fill_data is None:
            raise VenueUnavailable(f"ibkr order {intent.symbol} completed with no fills")

        execution = fill_data.execution
        fee = Decimal(str(execution.commission)) if execution.commission else Decimal(0)

        return Fill(
            intent_id=intent.idempotency_key,
            venue=self.name,
            symbol=intent.symbol,
            side=intent.side,
            filled_quantity=Decimal(str(execution.shares)),
            average_price=Decimal(str(execution.price)),
            fee_paid=fee,
            filled_at=datetime.now(UTC),
            external_id=str(execution.execId),
            raw={"order_id": str(execution.orderId)},
        )

    async def positions(self) -> list[Position]:
        self._ensure_connected()
        positions = self._ib.positions()
        result: list[Position] = []
        for p in positions:
            if self._account and p.account != self._account:
                continue
            qty = Decimal(str(p.position))
            if qty == 0:
                continue
            symbol = p.contract.symbol if hasattr(p.contract, "symbol") else "?"
            avg_cost = Decimal(str(p.avgCost)) / qty if qty != 0 else Decimal(0)
            result.append(Position(
                venue=self.name, symbol=symbol,
                market_type=MarketType.MARGIN if qty < 0 else MarketType.SPOT,
                quantity=qty, average_entry=abs(avg_cost),
                as_of=datetime.now(UTC),
            ))
        return result

    async def balances(self) -> list[Balance]:
        self._ensure_connected()
        acct_values = await self._ib.accountSummaryAsync(account=self._account)
        balances: list[Balance] = []
        cash = Decimal(0)
        for av in acct_values:
            if av.tag == "TotalCashValue":
                cash = Decimal(str(av.value))
            elif av.tag == "CashBalance":
                cash = max(cash, Decimal(str(av.value)))
        if cash != 0:
            balances.append(Balance(
                venue=self.name, asset="USD",
                free=cash, locked=Decimal(0),
                as_of=datetime.now(UTC),
            ))
        return balances

    async def cancel(self, external_id: str) -> bool:
        self._ensure_connected()
        try:
            trades = self._ib.openTrades()
            for t in trades:
                if str(t.order.orderId) == external_id:
                    self._ib.cancelOrder(t.order)
                    return True
            return False
        except Exception:  # noqa: BLE001
            return False

    # --- internals ---

    async def _resolve_account(self) -> None:
        accounts = self._ib.managedAccounts()
        if accounts:
            self._account = accounts[0]

    def _contract(self, symbol: str) -> Any:
        from ib_async import Stock
        return Stock(symbol, "SMART", "USD")
