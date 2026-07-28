"""Alpaca broker client.

Port of v1 `app/services/brokers/alpaca_client.py`. The SDK import is guarded
so the module imports cleanly without `alpaca-py`; the constructor raises
`ImportError` when the SDK is genuinely absent, which is the property the rest
of the system relies on (rule 1). Credentials arrive as constructor arguments
and are never read from the environment here (rule 4).

What changed from v1: import paths (`app.models.order` ->
`omni.execution.broker`; `app.core.logging` -> stdlib `logging`), and the
`Order` parameter type is now the `OrderRequest` value object from
`omni.execution.broker`. The order-translation logic, the four request shapes,
the connect/account/position/order-status paths, and the failure behaviour
(raise, do not return a stub) are bit-for-bit the v1 source.
"""

from __future__ import annotations

import logging
from typing import Any

from omni.execution.broker import (
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)

logger = logging.getLogger(__name__)

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import (
        OrderSide as AlpacaOrderSide,
    )
    from alpaca.trading.enums import (
        TimeInForce as AlpacaTimeInForce,
    )
    from alpaca.trading.requests import (
        LimitOrderRequest,
        MarketOrderRequest,
        StopLimitOrderRequest,
        StopOrderRequest,
    )

    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False
    TradingClient = None  # type: ignore[assignment,misc]
    StockHistoricalDataClient = None  # type: ignore[assignment,misc]


class AlpacaClient:
    """Alpaca broker client for paper and live trading.

    Requires `alpaca-py` (`pip install alpaca-py`). Without it, construction
    raises `ImportError` rather than synthesising a fake fill.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        paper: bool = True,
    ):
        if not ALPACA_AVAILABLE:
            raise ImportError("alpaca-py not installed. Install with: pip install alpaca-py")

        self.api_key = api_key
        self.api_secret = api_secret
        self.paper = paper
        self.trading_client: Any = None
        self.data_client: Any = None
        self.is_connected = False

        # our_order_id -> alpaca_order_id
        self.order_map: dict[str, str] = {}

    async def connect(self):
        """Connect to Alpaca by constructing the clients and verifying the account."""
        try:
            self.trading_client = TradingClient(
                api_key=self.api_key,
                secret_key=self.api_secret,
                paper=self.paper,
            )
            self.data_client = StockHistoricalDataClient(
                api_key=self.api_key,
                secret_key=self.api_secret,
            )
            account = self.trading_client.get_account()
            self.is_connected = True

            mode = "paper" if self.paper else "live"
            logger.info(
                "Connected to Alpaca (%s), Account: %s",
                mode,
                getattr(account, "account_number", "?"),
            )
        except Exception as e:
            logger.error("Failed to connect to Alpaca: %s", e)
            raise RuntimeError(f"Alpaca connection failed: {e}")

    async def disconnect(self):
        self.is_connected = False
        self.trading_client = None
        self.data_client = None
        logger.info("Disconnected from Alpaca")

    async def submit_order(self, order: OrderRequest) -> dict[str, Any]:
        if not self.is_connected or not self.trading_client:
            raise RuntimeError("Alpaca client not connected")

        try:
            alpaca_side = (
                AlpacaOrderSide.BUY if order.side == OrderSide.BUY else AlpacaOrderSide.SELL
            )

            tif_map = {
                TimeInForce.DAY: AlpacaTimeInForce.DAY,
                TimeInForce.GTC: AlpacaTimeInForce.GTC,
                TimeInForce.IOC: AlpacaTimeInForce.IOC,
                TimeInForce.FOK: AlpacaTimeInForce.FOK,
            }
            alpaca_tif = tif_map.get(order.time_in_force, AlpacaTimeInForce.DAY)

            if order.order_type == OrderType.MARKET:
                order_request: Any = MarketOrderRequest(
                    symbol=order.symbol,
                    qty=order.quantity,
                    side=alpaca_side,
                    time_in_force=alpaca_tif,
                )
            elif order.order_type == OrderType.LIMIT:
                order_request = LimitOrderRequest(
                    symbol=order.symbol,
                    qty=order.quantity,
                    side=alpaca_side,
                    time_in_force=alpaca_tif,
                    limit_price=float(order.limit_price),
                )
            elif order.order_type == OrderType.STOP:
                order_request = StopOrderRequest(
                    symbol=order.symbol,
                    qty=order.quantity,
                    side=alpaca_side,
                    time_in_force=alpaca_tif,
                    stop_price=float(order.stop_price),
                )
            elif order.order_type == OrderType.STOP_LIMIT:
                order_request = StopLimitOrderRequest(
                    symbol=order.symbol,
                    qty=order.quantity,
                    side=alpaca_side,
                    time_in_force=alpaca_tif,
                    stop_price=float(order.stop_price),
                    limit_price=float(order.limit_price),
                )
            else:
                raise ValueError(f"Unsupported order type: {order.order_type}")

            alpaca_order = self.trading_client.submit_order(order_request)

            self.order_map[order.id] = alpaca_order.id

            logger.info("Submitted order to Alpaca: %s for %s", alpaca_order.id, order.symbol)

            return {
                "order_id": alpaca_order.id,
                "client_order_id": alpaca_order.client_order_id,
                "status": alpaca_order.status.value,
                "submitted_at": alpaca_order.submitted_at.isoformat()
                if alpaca_order.submitted_at
                else None,
                "symbol": alpaca_order.symbol,
                "qty": str(alpaca_order.qty),
                "side": alpaca_order.side.value,
                "type": alpaca_order.type.value,
            }
        except Exception as e:
            logger.error("Failed to submit order: %s", e)
            raise RuntimeError(f"Order submission failed: {e}")

    async def cancel_order(self, order_id: str) -> bool:
        if not self.is_connected or not self.trading_client:
            raise RuntimeError("Alpaca client not connected")

        try:
            alpaca_order_id = self.order_map.get(order_id, order_id)
            self.trading_client.cancel_order_by_id(alpaca_order_id)
            logger.info("Cancelled order: %s", alpaca_order_id)
            return True
        except Exception as e:
            logger.error("Failed to cancel order: %s", e)
            return False

    async def get_positions(self) -> list[dict[str, Any]]:
        if not self.is_connected or not self.trading_client:
            raise RuntimeError("Alpaca client not connected")

        try:
            positions = self.trading_client.get_all_positions()
            return [
                {
                    "symbol": pos.symbol,
                    "quantity": float(pos.qty),
                    "market_value": float(pos.market_value),
                    "cost_basis": float(pos.cost_basis),
                    "unrealized_pl": float(pos.unrealized_pl),
                    "unrealized_plpc": float(pos.unrealized_plpc),
                    "current_price": float(pos.current_price),
                    "avg_entry_price": float(pos.avg_entry_price),
                    "side": pos.side,
                }
                for pos in positions
            ]
        except Exception as e:
            logger.error("Failed to get positions: %s", e)
            raise

    async def get_account_balance(self) -> dict[str, Any]:
        if not self.is_connected or not self.trading_client:
            raise RuntimeError("Alpaca client not connected")

        try:
            account = self.trading_client.get_account()
            return {
                "account_number": account.account_number,
                "status": account.status.value,
                "currency": account.currency,
                "cash": float(account.cash),
                "portfolio_value": float(account.portfolio_value),
                "buying_power": float(account.buying_power),
                "equity": float(account.equity),
                "last_equity": float(account.last_equity),
                "long_market_value": float(account.long_market_value),
                "short_market_value": float(account.short_market_value),
                "initial_margin": float(account.initial_margin),
                "maintenance_margin": float(account.maintenance_margin),
                "daytrade_count": account.daytrade_count,
                "pattern_day_trader": account.pattern_day_trader,
                "trading_blocked": account.trading_blocked,
                "transfers_blocked": account.transfers_blocked,
            }
        except Exception as e:
            logger.error("Failed to get account balance: %s", e)
            raise

    async def get_open_orders(self) -> list[dict[str, Any]]:
        if not self.is_connected or not self.trading_client:
            raise RuntimeError("Alpaca client not connected")

        try:
            orders = self.trading_client.get_orders(status="open")
            return [
                {
                    "order_id": order.id,
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "quantity": str(order.qty),
                    "filled_qty": str(order.filled_qty) if order.filled_qty else "0",
                    "side": order.side.value,
                    "type": order.type.value,
                    "status": order.status.value,
                    "limit_price": str(order.limit_price) if order.limit_price else None,
                    "stop_price": str(order.stop_price) if order.stop_price else None,
                    "time_in_force": order.time_in_force.value,
                    "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
                }
                for order in orders
            ]
        except Exception as e:
            logger.error("Failed to get open orders: %s", e)
            raise

    async def get_order_status(self, order_id: str) -> dict[str, Any] | None:
        if not self.is_connected or not self.trading_client:
            raise RuntimeError("Alpaca client not connected")

        try:
            alpaca_order_id = self.order_map.get(order_id, order_id)
            order = self.trading_client.get_order_by_id(alpaca_order_id)
            return {
                "order_id": order.id,
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "quantity": str(order.qty),
                "filled_qty": str(order.filled_qty) if order.filled_qty else "0",
                "filled_avg_price": str(order.filled_avg_price) if order.filled_avg_price else None,
                "side": order.side.value,
                "type": order.type.value,
                "status": order.status.value,
                "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
                "filled_at": order.filled_at.isoformat() if order.filled_at else None,
                "canceled_at": order.canceled_at.isoformat() if order.canceled_at else None,
            }
        except Exception as e:
            logger.error("Failed to get order status: %s", e)
            return None

    async def get_quote(self, symbol: str) -> dict[str, Any] | None:
        if not self.data_client:
            raise RuntimeError("Alpaca data client not connected")

        try:
            request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self.data_client.get_stock_latest_quote(request)
            if symbol in quotes:
                quote = quotes[symbol]
                return {
                    "symbol": symbol,
                    "bid_price": float(quote.bid_price),
                    "ask_price": float(quote.ask_price),
                    "bid_size": quote.bid_size,
                    "ask_size": quote.ask_size,
                    "timestamp": quote.timestamp.isoformat(),
                }
            return None
        except Exception as e:
            logger.error("Failed to get quote for %s: %s", symbol, e)
            return None

    async def close_position(self, symbol: str) -> bool:
        if not self.is_connected or not self.trading_client:
            raise RuntimeError("Alpaca client not connected")

        try:
            self.trading_client.close_position(symbol)
            logger.info("Closed position for %s", symbol)
            return True
        except Exception as e:
            logger.error("Failed to close position for %s: %s", symbol, e)
            return False

    async def close_all_positions(self) -> bool:
        if not self.is_connected or not self.trading_client:
            raise RuntimeError("Alpaca client not connected")

        try:
            self.trading_client.close_all_positions()
            logger.info("Closed all positions")
            return True
        except Exception as e:
            logger.error("Failed to close all positions: %s", e)
            return False
