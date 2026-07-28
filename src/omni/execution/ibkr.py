"""Interactive Brokers client.

Port of v1 `app/services/brokers/ibkr_client.py`. Like the Alpaca client, the
`ib_insync` import is guarded and the constructor raises `ImportError` when
the SDK is genuinely absent (rule 1). This is the file the work order points at
as the correct IBKR reference, as distinct from v1's `ibkr_integration.py`
which faked fills at a hardcoded 150.0 when the SDK was missing.

What was deliberately left behind from v1:

- `_on_order_status` and `_on_execution_details`. Both reached into
  `app.database_sync.SessionLocal` and mutated SQLAlchemy `Order` rows on every
  status/fill callback. That coupling does not carry (PORTING.md: SQLAlchemy
  sessions do not), and writing execution outcomes to a database is exactly the
  persistence this layer must not do here (rules 2 and 3). They are also where
  an execution outcome could leak into something calibration reads. The order
  submission, translation, and query paths below are independent of them.
- `_on_error` and the `orderStatusEvent`/`execDetailsEvent`/`errorEvent`
  subscriptions in `connect()` existed only to feed those callbacks; with the
  callbacks gone they have nothing to call.
- `OrderStatus` is no longer imported; it was referenced only by the dropped
  callbacks.

Import paths changed (`app.models.order` -> `omni.execution.broker`;
`app.core.logging` -> stdlib `logging`) and `Order` is now `OrderRequest`. The
order-translation logic in `_create_ib_order`, the connect/submit/cancel and
query paths, and the raise-on-disconnected behaviour are bit-for-bit v1.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from omni.execution.broker import (
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)

logger = logging.getLogger(__name__)

try:
    from ib_insync import (
        IB,
        LimitOrder,
        MarketOrder,
        Stock,
        StopOrder,
    )

    IB_INSYNC_AVAILABLE = True
except ImportError:
    IB_INSYNC_AVAILABLE = False
    IB = None  # type: ignore[assignment,misc]


class IBKRClient:
    """Interactive Brokers client using `ib_insync`.

    Requires `ib_insync` (`pip install ib_insync`). Without it, construction
    raises `ImportError` rather than synthesising a connection or a fill.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1):
        if not IB_INSYNC_AVAILABLE:
            raise ImportError("ib_insync not installed. Install with: pip install ib_insync")

        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib: Any = IB()
        self.is_connected = False

        # our_order_id -> ib_order
        self.order_map: dict[str, Any] = {}

    async def connect(self):
        try:
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
            self.is_connected = True
            logger.info("Connected to IBKR at %s:%s", self.host, self.port)
        except Exception as e:
            logger.error("Failed to connect to IBKR: %s", e)
            raise RuntimeError(f"IBKR connection failed: {e}")

    async def disconnect(self):
        if self.is_connected:
            self.ib.disconnect()
            self.is_connected = False
            logger.info("Disconnected from IBKR")

    async def submit_order(self, order: OrderRequest) -> dict[str, Any]:
        if not self.is_connected:
            raise RuntimeError("Not connected to IBKR")

        try:
            contract = Stock(order.symbol, "SMART", "USD")
            await self.ib.qualifyContractsAsync(contract)

            ib_order = self._create_ib_order(order)

            self.ib.placeOrder(contract, ib_order)

            self.order_map[order.id] = ib_order

            logger.info(
                "Order submitted to IBKR: %s %s %s @ %s, IB order ID: %s",
                order.symbol,
                order.side,
                order.quantity,
                order.order_type,
                ib_order.orderId,
            )

            return {
                "order_id": order.id,
                "ib_order_id": ib_order.orderId,
                "status": "submitted",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        except Exception as e:
            logger.error("Failed to submit order to IBKR: %s", e)
            raise RuntimeError(f"Order submission failed: {e}")

    def _create_ib_order(self, order: OrderRequest) -> Any:
        action = "BUY" if order.side == OrderSide.BUY else "SELL"
        quantity = float(order.quantity)

        if order.order_type == OrderType.MARKET:
            ib_order: Any = MarketOrder(action, quantity)
        elif order.order_type == OrderType.LIMIT:
            ib_order = LimitOrder(action, quantity, float(order.limit_price))
        elif order.order_type == OrderType.STOP:
            ib_order = StopOrder(action, quantity, float(order.stop_price))
        elif order.order_type == OrderType.STOP_LIMIT:
            ib_order = StopOrder(action, quantity, float(order.stop_price))
            ib_order.lmtPrice = float(order.limit_price)
        else:
            raise ValueError(f"Unsupported order type: {order.order_type}")

        if order.time_in_force == TimeInForce.DAY:
            ib_order.tif = "DAY"
        elif order.time_in_force == TimeInForce.GTC:
            ib_order.tif = "GTC"
        elif order.time_in_force == TimeInForce.IOC:
            ib_order.tif = "IOC"
        elif order.time_in_force == TimeInForce.FOK:
            ib_order.tif = "FOK"

        ib_order.orderRef = order.id

        return ib_order

    async def get_positions(self) -> list[dict[str, Any]]:
        if not self.is_connected:
            raise RuntimeError("Not connected to IBKR")

        positions = []
        for position in self.ib.positions():
            positions.append(
                {
                    "symbol": position.contract.symbol,
                    "quantity": float(position.position),
                    "average_cost": float(position.avgCost),
                    "market_value": (
                        float(position.marketValue) if hasattr(position, "marketValue") else 0
                    ),
                    "unrealized_pnl": (
                        float(position.unrealizedPNL) if hasattr(position, "unrealizedPNL") else 0
                    ),
                    "account": position.account,
                }
            )

        return positions

    async def get_account_balance(self) -> dict[str, Any]:
        if not self.is_connected:
            raise RuntimeError("Not connected to IBKR")

        account_values = {v.tag: v.value for v in self.ib.accountValues()}

        return {
            "total_cash": float(account_values.get("TotalCashValue", 0)),
            "net_liquidation": float(account_values.get("NetLiquidation", 0)),
            "available_funds": float(account_values.get("AvailableFunds", 0)),
            "buying_power": float(account_values.get("BuyingPower", 0)),
            "gross_position_value": float(account_values.get("GrossPositionValue", 0)),
            "realized_pnl": float(account_values.get("RealizedPnL", 0)),
            "unrealized_pnl": float(account_values.get("UnrealizedPnL", 0)),
        }

    async def cancel_order(self, order_id: str) -> bool:
        if not self.is_connected:
            raise RuntimeError("Not connected to IBKR")

        ib_order = self.order_map.get(order_id)
        if not ib_order:
            logger.warning("Order %s not found in order map", order_id)
            return False

        try:
            self.ib.cancelOrder(ib_order)
            logger.info("Order %s (IB: %s) cancelled", order_id, ib_order.orderId)
            return True
        except Exception as e:
            logger.error("Failed to cancel order %s: %s", order_id, e)
            return False

    async def get_open_orders(self) -> list[dict[str, Any]]:
        if not self.is_connected:
            raise RuntimeError("Not connected to IBKR")

        orders = []
        for trade in self.ib.openTrades():
            orders.append(
                {
                    "ib_order_id": trade.order.orderId,
                    "symbol": trade.contract.symbol,
                    "action": trade.order.action,
                    "quantity": trade.order.totalQuantity,
                    "order_type": trade.order.orderType,
                    "status": trade.orderStatus.status,
                    "filled": trade.orderStatus.filled,
                    "remaining": trade.orderStatus.remaining,
                }
            )

        return orders

    async def get_order_status(self, order_id: str) -> dict[str, Any] | None:
        if not self.is_connected:
            raise RuntimeError("Not connected to IBKR")

        ib_order = self.order_map.get(order_id)
        if not ib_order:
            return None

        for trade in self.ib.trades():
            if trade.order.orderId == ib_order.orderId:
                return {
                    "order_id": order_id,
                    "ib_order_id": trade.order.orderId,
                    "status": trade.orderStatus.status,
                    "filled": trade.orderStatus.filled,
                    "remaining": trade.orderStatus.remaining,
                    "avg_fill_price": trade.orderStatus.avgFillPrice,
                }

        return None
