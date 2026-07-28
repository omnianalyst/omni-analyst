"""Execution tier: broker clients and the unified facade.

Two properties matter above all here, both direct regressions of v1's
`ibkr_integration.py` defect (a missing SDK made `connect()` return `True` and
`place_order()` return a fake fill at a hardcoded 150.0, indistinguishable from
a real execution):

1. a client whose SDK is absent raises `ImportError` and never returns a
   success-shaped dict;
2. the facade never silently no-ops when no client is connected.

`alpaca-py` and `ib_insync` are intentionally not installed. The SDK-present
paths are exercised by installing a stub package tree into `sys.modules` and
reloading the client module, which re-runs its guarded import against the
stubs -- so the assertions hit the real field-translation code, not a parallel
implementation kept in step by hand. Each stub is torn down per test, so the
SDK-absent path stays honest in the tests that depend on it.

Rules 2 and 3 (execution writes no claim, feeds no calibration) are enforced
mechanically by an AST scan of every module in the package, not by reading the
code and trusting it.
"""

from __future__ import annotations

import ast
import enum
import importlib
import sys
import types
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omni.execution.broker import (
    Broker,
    BrokerType,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)

EXEC_DIR = Path(__file__).resolve().parent.parent / "src" / "omni" / "execution"


def _order(**overrides) -> OrderRequest:
    base: dict = {
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("100"),
    }
    base.update(overrides)
    return OrderRequest(**base)


def _alpaca_submitted_order() -> SimpleNamespace:
    # The shape submit_order() reads back from a real alpaca order response.
    return SimpleNamespace(
        id="alpaca-1",
        client_order_id="cid-1",
        status=SimpleNamespace(value="pending_new"),
        submitted_at=None,
        symbol="AAPL",
        qty=Decimal("100"),
        side=SimpleNamespace(value="buy"),
        type=SimpleNamespace(value="market"),
    )


# --- Stub SDK builders ------------------------------------------------------
# Built per-test by the fixtures. The recording request classes store their
# constructor kwargs so the field-translation tests can assert exactly what the
# client handed the SDK.


class _RecordingRequest:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


ALPACA_STUB_NAMES = [
    "alpaca",
    "alpaca.trading",
    "alpaca.trading.client",
    "alpaca.trading.requests",
    "alpaca.trading.enums",
    "alpaca.data",
    "alpaca.data.historical",
    "alpaca.data.requests",
]


def _build_alpaca_stub() -> dict[str, types.ModuleType]:
    alpaca = types.ModuleType("alpaca")
    alpaca.__path__ = []
    trading = types.ModuleType("alpaca.trading")
    trading.__path__ = []
    tclient = types.ModuleType("alpaca.trading.client")
    trequests = types.ModuleType("alpaca.trading.requests")
    tenums = types.ModuleType("alpaca.trading.enums")
    data = types.ModuleType("alpaca.data")
    data.__path__ = []
    dhist = types.ModuleType("alpaca.data.historical")
    drequests = types.ModuleType("alpaca.data.requests")

    class MarketOrderRequest(_RecordingRequest):
        pass

    class LimitOrderRequest(_RecordingRequest):
        pass

    class StopOrderRequest(_RecordingRequest):
        pass

    class StopLimitOrderRequest(_RecordingRequest):
        pass

    class StockLatestQuoteRequest(_RecordingRequest):
        pass

    class _OrderSide(enum.Enum):
        BUY = "buy"
        SELL = "sell"

    class _TimeInForce(enum.Enum):
        DAY = "day"
        GTC = "gtc"
        IOC = "ioc"
        FOK = "fok"

    class _OrderType(enum.Enum):
        MARKET = "market"
        LIMIT = "limit"
        STOP = "stop"
        STOP_LIMIT = "stop_limit"

    class _OrderStatus(enum.Enum):
        NEW = "new"
        PARTIALLY_FILLED = "partially_filled"
        FILLED = "filled"

    class TradingClient:
        def __init__(self, **kwargs):
            self.init_kwargs = kwargs
            self.get_account = MagicMock(return_value=SimpleNamespace(account_number="PAPER-1"))
            self.submit_order = MagicMock()
            self.cancel_order_by_id = MagicMock()
            self.get_all_positions = MagicMock(return_value=[])
            self.get_orders = MagicMock(return_value=[])
            self.get_order_by_id = MagicMock()
            self.close_position = MagicMock()
            self.close_all_positions = MagicMock()

    class StockHistoricalDataClient:
        def __init__(self, **kwargs):
            self.init_kwargs = kwargs

    tclient.TradingClient = TradingClient
    dhist.StockHistoricalDataClient = StockHistoricalDataClient
    trequests.MarketOrderRequest = MarketOrderRequest
    trequests.LimitOrderRequest = LimitOrderRequest
    trequests.StopOrderRequest = StopOrderRequest
    trequests.StopLimitOrderRequest = StopLimitOrderRequest
    tenums.OrderSide = _OrderSide
    tenums.TimeInForce = _TimeInForce
    tenums.OrderType = _OrderType
    tenums.OrderStatus = _OrderStatus
    drequests.StockLatestQuoteRequest = StockLatestQuoteRequest

    return {
        "alpaca": alpaca,
        "alpaca.trading": trading,
        "alpaca.trading.client": tclient,
        "alpaca.trading.requests": trequests,
        "alpaca.trading.enums": tenums,
        "alpaca.data": data,
        "alpaca.data.historical": dhist,
        "alpaca.data.requests": drequests,
    }


IBKR_STUB_NAMES = ["ib_insync"]


class _FakeIBOrder:
    def __init__(self, action="", quantity=0.0):
        self.action = action
        self.totalQuantity = quantity
        self.orderId = None
        self.tif = None
        self.orderRef = None
        self.lmtPrice = None
        self.auxPrice = None
        self.orderType = None


def _build_ibkr_stub() -> dict[str, types.ModuleType]:
    class _MarketOrder(_FakeIBOrder):
        def __init__(self, action, quantity):
            super().__init__(action, quantity)
            self.orderType = "MKT"

    class _LimitOrder(_FakeIBOrder):
        def __init__(self, action, quantity, lmt_price):
            super().__init__(action, quantity)
            self.lmtPrice = lmt_price
            self.orderType = "LMT"

    class _StopOrder(_FakeIBOrder):
        def __init__(self, action, quantity, stop_price):
            super().__init__(action, quantity)
            self.auxPrice = stop_price
            self.orderType = "STP"

    class _Stock:
        def __init__(self, symbol, exchange, currency):
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency

    class _IB:
        def __init__(self):
            self._next_id = 1
            self.placed: list = []

        async def connectAsync(self, *args, **kwargs):
            return True

        def disconnect(self):
            pass

        async def qualifyContractsAsync(self, *contracts):
            return list(contracts)

        def placeOrder(self, contract, order):
            order.orderId = self._next_id
            self._next_id += 1
            self.placed.append((contract, order))

        def positions(self):
            return []

        def accountValues(self):
            return []

        def openTrades(self):
            return []

        def trades(self):
            return []

        def cancelOrder(self, order):
            pass

    mod = types.ModuleType("ib_insync")
    mod.IB = _IB
    mod.Stock = _Stock
    mod.Order = _FakeIBOrder
    mod.MarketOrder = _MarketOrder
    mod.LimitOrder = _LimitOrder
    mod.StopOrder = _StopOrder
    return {"ib_insync": mod}


@pytest.fixture
def alpaca_sdk():
    import omni.execution.alpaca as mod

    saved = {name: sys.modules.get(name) for name in ALPACA_STUB_NAMES}
    sys.modules.update(_build_alpaca_stub())
    importlib.reload(mod)
    try:
        yield mod
    finally:
        for name in ALPACA_STUB_NAMES:
            if saved[name] is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved[name]
        importlib.reload(mod)


@pytest.fixture
def ibkr_sdk():
    import omni.execution.ibkr as mod

    saved = {name: sys.modules.get(name) for name in IBKR_STUB_NAMES}
    sys.modules.update(_build_ibkr_stub())
    importlib.reload(mod)
    try:
        yield mod
    finally:
        for name in IBKR_STUB_NAMES:
            if saved[name] is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved[name]
        importlib.reload(mod)


# --- Rule 2 & 3: mechanical import isolation -------------------------------


class TestImportIsolation:
    def test_execution_imports_nothing_from_coverage_conviction_or_capability(self):
        """Rules 2 and 3, enforced by AST not convention.

        Scans every import in the execution package and fails if any references
        `omni.coverage`, `omni.conviction`, or `omni.capability`, including
        relative imports that climb out of `omni.execution` up to `omni`.
        """
        forbidden = ("omni.coverage", "omni.conviction", "omni.capability")
        forbidden_short = {p.split(".", 1)[1] for p in forbidden}
        files = sorted(EXEC_DIR.glob("*.py"))
        assert files, "execution package sources not found"

        offenders: list[tuple[str, str]] = []
        for path in files:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if any(
                            alias.name == p or alias.name.startswith(p + ".") for p in forbidden
                        ):
                            offenders.append((path.name, alias.name))
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if node.level == 0:
                        if any(mod == p or mod.startswith(p + ".") for p in forbidden):
                            offenders.append((path.name, f"from {mod} import"))
                    elif node.level >= 2 and mod in forbidden_short:
                        offenders.append((path.name, f"relative .{'.' * (node.level - 1)}{mod}"))
        assert not offenders, f"execution package imports forbidden modules: {offenders}"


# --- Alpaca: SDK absent (the path that matters most) -----------------------


class TestAlpacaSdkAbsent:
    def test_the_module_reports_the_sdk_unavailable(self):
        import omni.execution.alpaca as mod

        assert mod.ALPACA_AVAILABLE is False

    def test_construction_raises_import_error_naming_the_package(self):
        import omni.execution.alpaca as mod

        with pytest.raises(ImportError, match="alpaca-py"):
            mod.AlpacaClient(api_key="k", api_secret="s")

    def test_no_success_dict_is_reachable_without_the_sdk(self):
        """The ibkr_integration regression returned a success-shaped dict from a
        fake fill when the SDK was missing. Construction raising means there is
        no instance that could return one."""
        import omni.execution.alpaca as mod

        with pytest.raises(ImportError):
            client = mod.AlpacaClient(api_key="k", api_secret="s")
            # Unreachable, but makes the intent explicit: no object, no dict.
            assert not hasattr(client, "submit_order")


# --- Alpaca: order translation with the SDK stubbed ------------------------


class TestAlpacaOrderMapping:
    @staticmethod
    async def _connected(mod):
        client = mod.AlpacaClient(api_key="k", api_secret="s", paper=True)
        await client.connect()
        return client

    async def test_market_buy_maps_to_market_order_request(self, alpaca_sdk):
        mod = alpaca_sdk
        client = await self._connected(mod)
        client.trading_client.submit_order.return_value = _alpaca_submitted_order()

        result = await client.submit_order(_order())

        req = client.trading_client.submit_order.call_args.args[0]
        assert isinstance(req, mod.MarketOrderRequest)
        assert req.kwargs == {
            "symbol": "AAPL",
            "qty": Decimal("100"),
            "side": mod.AlpacaOrderSide.BUY,
            "time_in_force": mod.AlpacaTimeInForce.DAY,
        }
        assert result["order_id"] == "alpaca-1"
        assert result["status"] == "pending_new"

    async def test_limit_order_carries_limit_price_as_float(self, alpaca_sdk):
        mod = alpaca_sdk
        client = await self._connected(mod)
        client.trading_client.submit_order.return_value = _alpaca_submitted_order()

        await client.submit_order(_order(order_type=OrderType.LIMIT, limit_price=Decimal("185.50")))

        req = client.trading_client.submit_order.call_args.args[0]
        assert isinstance(req, mod.LimitOrderRequest)
        assert req.kwargs["limit_price"] == 185.5
        assert isinstance(req.kwargs["limit_price"], float)

    async def test_stop_order_carries_stop_price_as_float(self, alpaca_sdk):
        mod = alpaca_sdk
        client = await self._connected(mod)
        client.trading_client.submit_order.return_value = _alpaca_submitted_order()

        await client.submit_order(_order(order_type=OrderType.STOP, stop_price=Decimal("180")))

        req = client.trading_client.submit_order.call_args.args[0]
        assert isinstance(req, mod.StopOrderRequest)
        assert req.kwargs["stop_price"] == 180.0
        assert isinstance(req.kwargs["stop_price"], float)

    async def test_stop_limit_carries_both_prices(self, alpaca_sdk):
        mod = alpaca_sdk
        client = await self._connected(mod)
        client.trading_client.submit_order.return_value = _alpaca_submitted_order()

        await client.submit_order(
            _order(
                order_type=OrderType.STOP_LIMIT,
                stop_price=Decimal("180"),
                limit_price=Decimal("185.50"),
            )
        )

        req = client.trading_client.submit_order.call_args.args[0]
        assert isinstance(req, mod.StopLimitOrderRequest)
        assert req.kwargs["stop_price"] == 180.0
        assert req.kwargs["limit_price"] == 185.5

    async def test_time_in_force_translates(self, alpaca_sdk):
        mod = alpaca_sdk
        client = await self._connected(mod)
        client.trading_client.submit_order.return_value = _alpaca_submitted_order()

        await client.submit_order(_order(time_in_force=TimeInForce.GTC))

        req = client.trading_client.submit_order.call_args.args[0]
        assert req.kwargs["time_in_force"] == mod.AlpacaTimeInForce.GTC

    async def test_sell_side_translates(self, alpaca_sdk):
        mod = alpaca_sdk
        client = await self._connected(mod)
        client.trading_client.submit_order.return_value = _alpaca_submitted_order()

        await client.submit_order(_order(side=OrderSide.SELL))

        req = client.trading_client.submit_order.call_args.args[0]
        assert req.kwargs["side"] == mod.AlpacaOrderSide.SELL

    async def test_our_order_id_is_recorded_in_the_order_map(self, alpaca_sdk):
        mod = alpaca_sdk
        client = await self._connected(mod)
        client.trading_client.submit_order.return_value = _alpaca_submitted_order()

        order = _order()
        await client.submit_order(order)

        assert client.order_map[order.id] == "alpaca-1"

    async def test_credentials_are_forwarded_to_the_trading_client(self, alpaca_sdk):
        """Rule 4: credentials arrive as constructor args and are forwarded to
        the SDK; nothing is read from the environment inside the client."""
        mod = alpaca_sdk
        client = mod.AlpacaClient(api_key="KEY", api_secret="SECRET", paper=True)
        await client.connect()

        assert client.trading_client.init_kwargs == {
            "api_key": "KEY",
            "secret_key": "SECRET",
            "paper": True,
        }

    async def test_unsupported_order_type_is_refused_explicitly(self, alpaca_sdk):
        """The dispatch has four branches; an order type outside them must be
        refused, not silently dropped. Forced past the enum since the four real
        types each have a branch."""
        mod = alpaca_sdk
        client = await self._connected(mod)
        order = _order()
        object.__setattr__(order, "order_type", "trailing_stop")

        with pytest.raises(RuntimeError, match="Unsupported order type"):
            await client.submit_order(order)

    async def test_submit_before_connect_raises_not_returns_default(self, alpaca_sdk):
        mod = alpaca_sdk
        client = mod.AlpacaClient(api_key="k", api_secret="s")
        # connect() intentionally not called.
        with pytest.raises(RuntimeError, match="not connected"):
            await client.submit_order(_order())


# --- IBKR: SDK absent ------------------------------------------------------


class TestIbkrSdkAbsent:
    def test_the_module_reports_the_sdk_unavailable(self):
        import omni.execution.ibkr as mod

        assert mod.IB_INSYNC_AVAILABLE is False

    def test_construction_raises_import_error_naming_the_package(self):
        import omni.execution.ibkr as mod

        with pytest.raises(ImportError, match="ib_insync"):
            mod.IBKRClient()

    def test_no_success_dict_is_reachable_without_the_sdk(self):
        import omni.execution.ibkr as mod

        with pytest.raises(ImportError):
            client = mod.IBKRClient()
            assert not hasattr(client, "submit_order")


# --- IBKR: order translation with the SDK stubbed --------------------------


class TestIbkrOrderMapping:
    async def test_connect_sets_connected_and_logs_host(self, ibkr_sdk):
        mod = ibkr_sdk
        client = mod.IBKRClient(host="127.0.0.1", port=7497, client_id=1)
        assert client.is_connected is False
        await client.connect()
        assert client.is_connected is True

    def test_market_order_translates_action_quantity_and_tif(self, ibkr_sdk):
        mod = ibkr_sdk
        client = mod.IBKRClient()
        order = _order(
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
        )

        ib_order = client._create_ib_order(order)

        assert isinstance(ib_order, mod.MarketOrder)
        assert ib_order.action == "BUY"
        assert ib_order.totalQuantity == 100.0
        assert ib_order.tif == "DAY"
        assert ib_order.orderType == "MKT"

    def test_limit_order_sets_limit_price(self, ibkr_sdk):
        mod = ibkr_sdk
        client = mod.IBKRClient()
        order = _order(order_type=OrderType.LIMIT, limit_price=Decimal("185.50"))

        ib_order = client._create_ib_order(order)

        assert isinstance(ib_order, mod.LimitOrder)
        assert ib_order.lmtPrice == 185.5
        assert ib_order.orderType == "LMT"

    def test_stop_order_sets_the_stop_aux_price(self, ibkr_sdk):
        mod = ibkr_sdk
        client = mod.IBKRClient()
        order = _order(order_type=OrderType.STOP, stop_price=Decimal("180"))

        ib_order = client._create_ib_order(order)

        assert isinstance(ib_order, mod.StopOrder)
        assert ib_order.auxPrice == 180.0
        assert ib_order.orderType == "STP"

    def test_stop_limit_sets_both_prices(self, ibkr_sdk):
        mod = ibkr_sdk
        client = mod.IBKRClient()
        order = _order(
            order_type=OrderType.STOP_LIMIT,
            stop_price=Decimal("180"),
            limit_price=Decimal("185.50"),
        )

        ib_order = client._create_ib_order(order)

        assert isinstance(ib_order, mod.StopOrder)
        assert ib_order.auxPrice == 180.0
        assert ib_order.lmtPrice == 185.5

    def test_sell_side_translates_to_sell_action(self, ibkr_sdk):
        mod = ibkr_sdk
        client = mod.IBKRClient()
        order = _order(side=OrderSide.SELL)

        ib_order = client._create_ib_order(order)

        assert ib_order.action == "SELL"

    def test_each_time_in_force_maps_to_its_ibkr_string(self, ibkr_sdk):
        mod = ibkr_sdk
        client = mod.IBKRClient()
        expected = {
            TimeInForce.DAY: "DAY",
            TimeInForce.GTC: "GTC",
            TimeInForce.IOC: "IOC",
            TimeInForce.FOK: "FOK",
        }
        for tif, expected_tif in expected.items():
            order = _order(time_in_force=tif)
            assert client._create_ib_order(order).tif == expected_tif

    def test_order_ref_records_our_order_id(self, ibkr_sdk):
        mod = ibkr_sdk
        client = mod.IBKRClient()
        order = _order()

        ib_order = client._create_ib_order(order)

        assert ib_order.orderRef == order.id

    def test_unsupported_order_type_is_refused_explicitly(self, ibkr_sdk):
        mod = ibkr_sdk
        client = mod.IBKRClient()
        order = _order()
        object.__setattr__(order, "order_type", "trailing_stop")

        with pytest.raises(ValueError, match="Unsupported order type"):
            client._create_ib_order(order)

    async def test_submit_order_places_and_returns_submitted(self, ibkr_sdk):
        mod = ibkr_sdk
        client = mod.IBKRClient()
        await client.connect()
        order = _order()

        result = await client.submit_order(order)

        assert result["status"] == "submitted"
        assert result["order_id"] == order.id
        assert result["ib_order_id"] == 1
        assert client.order_map[order.id] is not None
        placed_contract, placed_order = client.ib.placed[0]
        assert placed_contract.symbol == "AAPL"
        assert placed_order.orderRef == order.id

    async def test_submit_before_connect_raises_not_returns_default(self, ibkr_sdk):
        mod = ibkr_sdk
        client = mod.IBKRClient()
        with pytest.raises(RuntimeError, match="Not connected to IBKR"):
            await client.submit_order(_order())


# --- Facade ----------------------------------------------------------------


class TestBrokerFacade:
    def test_unknown_broker_type_is_refused_at_construction(self):
        with pytest.raises(ValueError):
            Broker("td_ameritrade", {})

    def test_unknown_broker_enum_is_refused_at_construction(self):
        with pytest.raises(ValueError):
            BrokerType("schwab")

    def test_is_connected_is_false_before_connect(self):
        broker = Broker(BrokerType.ALPACA, {"api_key": "k", "api_secret": "s"})
        assert broker.is_connected() is False

    @pytest.mark.parametrize(
        "method, args",
        [
            ("submit_order", (_order(),)),
            ("cancel_order", ("some-id",)),
            ("get_positions", ()),
            ("get_account_balance", ()),
            ("get_open_orders", ()),
            ("get_order_status", ("some-id",)),
        ],
    )
    async def test_every_operation_raises_when_no_client_connected(self, method, args):
        broker = Broker(BrokerType.ALPACA, {"api_key": "k", "api_secret": "s"})
        fn = getattr(broker, method)
        with pytest.raises(RuntimeError, match="not connected"):
            await fn(*args)

    async def test_alpaca_connect_raises_import_error_without_sdk(self):
        broker = Broker(BrokerType.ALPACA, {"api_key": "k", "api_secret": "s"})
        with pytest.raises(ImportError, match="alpaca-py"):
            await broker.connect()
        assert broker.is_connected() is False
        # The facade must not hand back a success dict in lieu of a connection.
        with pytest.raises(RuntimeError, match="not connected"):
            await broker.submit_order(_order())

    async def test_ibkr_connect_raises_import_error_without_sdk(self):
        broker = Broker(BrokerType.IBKR, {})
        with pytest.raises(ImportError, match="ib_insync"):
            await broker.connect()
        assert broker.is_connected() is False
        with pytest.raises(RuntimeError, match="not connected"):
            await broker.get_positions()

    async def test_facade_delegates_submit_to_alpaca_client(self, alpaca_sdk):
        broker = Broker("alpaca", {"api_key": "k", "api_secret": "s", "paper": True})
        await broker.connect()
        assert broker.is_connected() is True

        broker._client.trading_client.submit_order.return_value = _alpaca_submitted_order()
        result = await broker.submit_order(_order())
        assert result["status"] == "pending_new"

    async def test_facade_delegates_submit_to_ibkr_client(self, ibkr_sdk):
        broker = Broker("ibkr", {"host": "127.0.0.1", "port": 7497, "client_id": 1})
        await broker.connect()
        assert broker.is_connected() is True

        order = _order()
        result = await broker.submit_order(order)
        assert result["status"] == "submitted"
        assert result["order_id"] == order.id

    async def test_disconnect_is_safe_when_never_connected(self):
        broker = Broker(BrokerType.ALPACA, {"api_key": "k", "api_secret": "s"})
        # Must not raise -- nothing to disconnect from.
        await broker.disconnect()
        assert broker.is_connected() is False
