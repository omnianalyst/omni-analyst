"""Execution facade and the order value object shared by the broker clients.

This is the only layer in v2 that acts on the world rather than describing it.
A port of v1 `app/services/brokers/broker_service.py`, with three things that
file did deliberately removed because each is a silent no-op in disguise:

- the module-level singleton (`_broker_service_instance` / `get_broker_service`):
  app state that does not carry per PORTING.md, and a global broker is the wrong
  shape when credentials are per-operator;
- `BrokerType.TD_AMERITRADE`, whose `initialize()` set `self.client = None` and
  *returned an error dict* instead of raising — a caller that did not check the
  dict would proceed as if connected;
- `is_connected()` returning `True` when the client had no `is_connected`
  attribute ("assume connected if no explicit check available"). The same
  assumption in v1's `ibkr_integration.py` is what let it fake fills at a
  hardcoded 150.0. Connection state is reported by the client or it is false.

Credentials are passed in at construction (rule 4) and forwarded to the chosen
client; neither this facade nor the clients read the environment. The clients
raise `ImportError` from their own constructor when their SDK is absent, so a
missing SDK surfaces from `connect()` rather than being swallowed.

Execution writes no claim and feeds no calibration (rules 2 and 3). Nothing in
this package imports `omni.coverage`, `omni.conviction`, or `omni.capability`;
`tests/test_execution.py::test_*_imports_nothing_from_*` enforces that
mechanically.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


class BrokerType(str, enum.Enum):
    ALPACA = "alpaca"
    IBKR = "ibkr"


class OrderSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, enum.Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


@dataclass(frozen=True)
class OrderRequest:
    """A broker-agnostic order handed to a client.

    v1's `Order` was a SQLAlchemy model bound to `users`/`portfolios` tables.
    The SQLAlchemy layer does not carry (PORTING.md), and execution has no
    business deciding persistence here, so the port is a plain value object
    holding only the fields the two clients actually read. `quantity` and the
    prices are `Decimal` because that is what money is; the clients cast to
    float at the SDK boundary exactly as v1 did.
    """

    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    id: str = field(default_factory=lambda: uuid4().hex)


class Broker:
    """Unified facade over a single configured broker client.

    The client is constructed eagerly inside `connect()` so that merely naming
    a broker type does not require its SDK to be importable; the SDK guard
    fires only when a connection is actually attempted. Every operation routes
    through `_require_client()`, which means "no client configured" is a raise,
    never a return — the failure mode v1's TD Ameritrade branch and
    `is_connected()` default both hid.
    """

    def __init__(
        self,
        broker_type: BrokerType | str,
        credentials: dict[str, Any] | None = None,
    ) -> None:
        self.broker_type = (
            broker_type if isinstance(broker_type, BrokerType) else BrokerType(broker_type)
        )
        self._credentials: dict[str, Any] = dict(credentials or {})
        self._client: Any = None

    async def connect(self) -> None:
        if self.broker_type is BrokerType.ALPACA:
            from omni.execution.alpaca import AlpacaClient

            client: Any = AlpacaClient(
                api_key=self._credentials.get("api_key", ""),
                api_secret=self._credentials.get("api_secret", ""),
                paper=self._credentials.get("paper", True),
            )
        elif self.broker_type is BrokerType.IBKR:
            from omni.execution.ibkr import IBKRClient

            client = IBKRClient(
                host=self._credentials.get("host", "127.0.0.1"),
                port=self._credentials.get("port", 7497),
                client_id=self._credentials.get("client_id", 1),
            )
        else:
            # Unreachable: __init__ rejects unknown values via BrokerType(...).
            raise ValueError(f"Unsupported broker type: {self.broker_type}")

        await client.connect()
        self._client = client
        logger.info("Connected to %s broker", self.broker_type.value)

    async def disconnect(self) -> None:
        if self._client is not None and hasattr(self._client, "disconnect"):
            await self._client.disconnect()
            logger.info("Disconnected from %s broker", self.broker_type.value)

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError(
                f"broker {self.broker_type.value!r} not connected; call connect() first"
            )
        return self._client

    def is_connected(self) -> bool:
        if self._client is None:
            return False
        return bool(getattr(self._client, "is_connected", False))

    async def submit_order(self, order: OrderRequest) -> dict[str, Any]:
        return await self._require_client().submit_order(order)

    async def cancel_order(self, order_id: str) -> bool:
        return await self._require_client().cancel_order(order_id)

    async def get_positions(self) -> list[dict[str, Any]]:
        return await self._require_client().get_positions()

    async def get_account_balance(self) -> dict[str, Any]:
        return await self._require_client().get_account_balance()

    async def get_open_orders(self) -> list[dict[str, Any]]:
        return await self._require_client().get_open_orders()

    async def get_order_status(self, order_id: str) -> dict[str, Any] | None:
        return await self._require_client().get_order_status(order_id)
