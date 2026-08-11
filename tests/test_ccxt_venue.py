"""The live exchange adapter, tested against the ways it could lose money.

Every test here injects a fake exchange. Nothing touches the network and
nothing constructs a real ccxt client, so the whole file is deterministic --
which matters more than usual, because the code under test is the code that
places orders.

The fixtures mirror what Binance actually declares, measured live: spot
BTC/USDT requires 5 USDT of cost, the BTC perpetual requires 50, and the SOL
perpetual requires 5. Those three numbers are the reason a single
`min_notional` constant cannot be right, and two of the tests below exist
solely to prove the adapter reads the per-symbol figure rather than any
constant.

The invariants under test, each of which was confirmed to fail when the
corresponding guard is mutated away:

1. the venue does not trade unless a caller typed the opt-in;
2. the minimum is the symbol's own, not one number for the venue;
3. the intent's idempotency key reaches the exchange as the client order id;
4. an ambiguous response raises instead of reporting an empty fill;
5. the amount is rounded to the venue's precision before it is submitted.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

import pytest

from omni.venue.ccxt_venue import (
    CLIENT_ORDER_ID_PARAM,
    CCXTVenue,
    TradingMode,
)
from omni.venue.protocol import (
    MarketType,
    OrderKind,
    Side,
    TradeIntent,
    Venue,
    VenueUnavailable,
)

TS = 1754000000000
AT = datetime.fromtimestamp(TS / 1000, tz=UTC)

# Binance's own declarations, read from the live venue on 2026-08-07. Written
# as literals rather than derived from anything in the module under test: they
# are facts about the exchange, and a fixture computed from the adapter would
# agree with whatever the adapter believed.
SPOT_BTC = {
    "symbol": "BTC/USDT",
    "type": "spot",
    "spot": True,
    "margin": True,
    "swap": False,
    "active": True,
    "precision": {"amount": 5, "price": 2},
    "limits": {"amount": {"min": 0.00001}, "cost": {"min": 5.0}},
}
PERP_BTC = {
    "symbol": "BTC/USDT:USDT",
    "type": "swap",
    "spot": False,
    "margin": False,
    "swap": True,
    "active": True,
    "precision": {"amount": 3, "price": 1},
    "limits": {"amount": {"min": 0.001}, "cost": {"min": 50.0}},
}
PERP_SOL = {
    "symbol": "SOL/USDT:USDT",
    "type": "swap",
    "spot": False,
    "margin": False,
    "swap": True,
    "active": True,
    "precision": {"amount": 0, "price": 4},
    "limits": {"amount": {"min": 1.0}, "cost": {"min": 5.0}},
}

MARKETS = {m["symbol"]: m for m in (SPOT_BTC, PERP_BTC, PERP_SOL)}

HAS = {
    "createOrder": True,
    "createLimitOrder": True,
    "fetchOrder": True,
    "fetchPositions": True,
    "fetchBalance": True,
    "fetchTicker": True,
    "cancelOrder": True,
    "fetchFundingRate": True,
}

# ccxt publishes fees as fractions: 0.001 is 10bps.
FEES = {"trading": {"maker": 0.001, "taker": 0.001}}


class FakeExchange:
    """The ccxt surface this adapter uses, and nothing else.

    `amount_to_precision` / `price_to_precision` truncate to the market's
    declared decimals, which is what ccxt's DECIMAL_PLACES + TRUNCATE mode does
    for Binance. Every call is recorded so a test can assert what was actually
    sent rather than what the adapter meant to send.
    """

    id = "fakex"

    def __init__(
        self,
        *,
        markets: dict | None = None,
        has: dict | None = None,
        fees: dict | None = None,
        order: dict | None = None,
        create_error: BaseException | None = None,
        fetched_order: dict | None = None,
        fetch_error: BaseException | None = None,
        ticker: dict | None = None,
        order_book: dict | None = None,
        balance: dict | None = None,
        positions: list | None = None,
        cancel_result: dict | None = None,
        cancel_error: BaseException | None = None,
    ) -> None:
        self.markets = MARKETS if markets is None else markets
        self.has = HAS if has is None else has
        self.fees = FEES if fees is None else fees
        self._order = order
        self._create_error = create_error
        self._fetched_order = fetched_order
        self._fetch_error = fetch_error
        self._order_book = order_book
        self._ticker = ticker
        self._balance = balance
        self._positions = positions
        self._cancel_result = cancel_result
        self._cancel_error = cancel_error

        self.created: list[dict] = []
        self.fetched: list[dict] = []
        self.cancelled: list[tuple[str, str]] = []

    def _decimals(self, symbol: str, kind: str) -> int:
        return int(self.markets[symbol]["precision"][kind])

    def _truncate(self, symbol: str, kind: str, value) -> str:
        step = Decimal(1).scaleb(-self._decimals(symbol, kind))
        return str(Decimal(str(value)).quantize(step, rounding=ROUND_DOWN))

    def amount_to_precision(self, symbol: str, amount) -> str:
        return self._truncate(symbol, "amount", amount)

    def price_to_precision(self, symbol: str, price) -> str:
        return self._truncate(symbol, "price", price)

    async def create_order(
        self, symbol, type, side, amount, price=None, params=None
    ) -> dict:
        self.created.append(
            {
                "symbol": symbol,
                "type": type,
                "side": side,
                "amount": amount,
                "price": price,
                "params": dict(params or {}),
            }
        )
        if self._create_error is not None:
            raise self._create_error
        if self._order is not None:
            return self._order
        return _order(
            filled=float(amount),
            average=10_000.0,
            client_order_id=(params or {}).get(CLIENT_ORDER_ID_PARAM),
        )

    async def fetch_order(self, id, symbol=None, params=None) -> dict:
        self.fetched.append({"id": id, "symbol": symbol, "params": dict(params or {})})
        if self._fetch_error is not None:
            raise self._fetch_error
        if self._fetched_order is None:
            raise AssertionError("fetch_order called with no response configured")
        return self._fetched_order

    async def cancel_order(self, id, symbol=None, params=None) -> dict:
        self.cancelled.append((id, symbol))
        if self._cancel_error is not None:
            raise self._cancel_error
        return self._cancel_result or {"id": id, "status": "canceled"}

    async def fetch_ticker(self, symbol) -> dict:
        if self._ticker is None:
            raise AssertionError("fetch_ticker called with no ticker configured")
        return self._ticker

    async def fetch_order_book(self, symbol, limit=None) -> dict:
        # Empty unless a test configures one: the adapter falls back here when a
        # ticker is one-sided, and a fake that invented a book would hide the
        # refusal that behaviour is supposed to produce.
        return self._order_book or {"bids": [], "asks": []}

    async def fetch_balance(self) -> dict:
        if self._balance is None:
            raise AssertionError("fetch_balance called with no balance configured")
        return self._balance

    async def fetch_positions(self, symbols=None) -> list:
        if self._positions is None:
            raise AssertionError("fetch_positions called with no positions configured")
        return self._positions


def _order(
    *,
    filled: float | None,
    average: float | None = 10_000.0,
    status: str | None = "closed",
    client_order_id: str | None = None,
    order_id: str = "venue-1",
    fee: dict | None = None,
    **extra,
) -> dict:
    order = {
        "id": order_id,
        "clientOrderId": client_order_id,
        "status": status,
        "filled": filled,
        "average": average,
        "timestamp": TS,
        "fee": {"cost": 0.01, "currency": "USDT"} if fee is None else fee,
    }
    order.update(extra)
    return order


def _venue(exchange: FakeExchange, **kwargs) -> CCXTVenue:
    return CCXTVenue(exchange, **kwargs)


def _live(exchange: FakeExchange, **kwargs) -> CCXTVenue:
    return CCXTVenue(exchange, mode=TradingMode.LIVE, **kwargs)


def _intent(
    *,
    symbol: str = "BTC/USDT",
    market_type: MarketType = MarketType.SPOT,
    quantity: str = "0.001",
    reference_price: str = "10000",
    side: Side = Side.BUY,
    **kwargs,
) -> TradeIntent:
    return TradeIntent(
        venue="fakex",
        symbol=symbol,
        side=side,
        market_type=market_type,
        quantity=Decimal(quantity),
        reference_price=Decimal(reference_price),
        **kwargs,
    )


class TestTradingIsOffByDefault:
    """The state you get by saying nothing must be the state that cannot trade."""

    async def test_execute_refuses_without_an_explicit_opt_in(self):
        exchange = FakeExchange()
        venue = _venue(exchange)

        with pytest.raises(VenueUnavailable, match="read_only"):
            await venue.execute(_intent())

        # The refusal has to happen before the venue is touched at all -- an
        # order placed and then complained about is still an order placed.
        assert exchange.created == []

    async def test_the_opt_in_is_what_makes_it_trade(self):
        exchange = FakeExchange()
        fill = await _live(exchange).execute(_intent())

        assert len(exchange.created) == 1
        assert fill.filled_quantity == Decimal("0.001")

    async def test_no_entry_point_defaults_to_live_trading(self):
        """Rule 5, checked against the signatures rather than against a memory.

        `connect()` is the path that builds a real client, so a permissive
        default there would be reachable without any test exercising it.
        """
        for entry in (CCXTVenue.__init__, CCXTVenue.connect):
            for name, param in inspect.signature(entry).parameters.items():
                if param.default is inspect.Parameter.empty:
                    continue
                assert param.default is not TradingMode.LIVE, name
                assert param.default != "live", name
                if isinstance(param.default, bool):
                    assert param.default is False, name

    async def test_reading_the_account_never_needs_the_opt_in(self):
        """A kill switch that required a trading opt-in fails in the wrong
        direction: it could not read what it had to close."""
        exchange = FakeExchange(
            balance={
                "total": {"USDT": 120.0},
                "USDT": {"free": 100.0, "used": 20.0, "total": 120.0},
            },
            positions=[
                {
                    "symbol": "BTC/USDT:USDT",
                    "contracts": 0.5,
                    "side": "short",
                    "entryPrice": 61000.0,
                    "timestamp": TS,
                }
            ],
            ticker={"bid": 9999.0, "ask": 10001.0, "timestamp": TS},
        )
        venue = _venue(exchange)

        assert venue.mode is TradingMode.READ_ONLY
        assert [b.free for b in await venue.balances()] == [Decimal(100)]
        assert [p.quantity for p in await venue.positions()] == [Decimal("-0.5")]
        assert (await venue.quote(_intent())).expected_price == Decimal(10001)

    async def test_a_truthy_value_is_not_an_opt_in(self):
        with pytest.raises(TypeError):
            CCXTVenue(FakeExchange(), mode=True)


class TestPerSymbolMinimum:
    """`min_notional` is one number; the venue's minimums are not."""

    async def test_the_same_order_clears_spot_and_fails_the_perp(self):
        """0.001 at 10,000 is 10 USDT of cost.

        Binance takes that on spot BTC/USDT (minimum 5) and rejects it on
        BTC/USDT:USDT (minimum 50). Any single constant gets one of these two
        wrong, which is the whole point: 5 lets the perpetual through to be
        rejected by the exchange, 50 blocks a spot order that was always valid.
        """
        spot_exchange = FakeExchange()
        spot_fill = await _live(spot_exchange).execute(_intent())
        assert spot_fill.filled_quantity == Decimal("0.001")
        assert len(spot_exchange.created) == 1

        perp_exchange = FakeExchange()
        perp_fill = await _live(perp_exchange).execute(
            _intent(symbol="BTC/USDT:USDT", market_type=MarketType.PERPETUAL)
        )
        assert perp_fill.is_empty
        assert perp_exchange.created == []

        reason = perp_fill.raw["rejected"]
        assert "BTC/USDT:USDT" in reason
        assert "10" in reason and "50" in reason

    async def test_the_minimum_is_checked_after_rounding_not_before(self):
        """Rounding down can push a valid order under the minimum.

        1.9 SOL perp contracts at 3 USDT is 5.7 of cost, above that market's
        minimum of 5. The market takes whole contracts, so it rounds to 1, and
        the order that would actually be sent is worth 3 -- below the minimum
        the unrounded size cleared. Checking before rounding submits an order
        the exchange rejects.
        """
        exchange = FakeExchange()

        fill = await _live(exchange).execute(
            _intent(
                symbol="SOL/USDT:USDT",
                market_type=MarketType.PERPETUAL,
                quantity="1.9",
                reference_price="3",
            )
        )

        assert fill.is_empty
        assert exchange.created == []
        assert "SOL/USDT:USDT" in fill.raw["rejected"]

    async def test_min_notional_for_reports_the_venues_own_figure(self):
        venue = _venue(FakeExchange())

        assert venue.min_notional_for("BTC/USDT") == Decimal(5)
        assert venue.min_notional_for("BTC/USDT:USDT") == Decimal(50)
        assert venue.min_notional_for("SOL/USDT:USDT") == Decimal(5)
        with pytest.raises(VenueUnavailable, match="does not list"):
            venue.min_notional_for("DOGE/USDT")

    async def test_the_capabilities_scalar_never_understates_the_minimum(self):
        """`Capabilities.min_notional` cannot hold three numbers.

        It is set to the largest in scope, because a scalar that understates
        the constraint would let a caller size an order the venue then rejects.
        Narrowing the scope narrows the scalar; `min_notional_for` stays the
        real answer either way.
        """
        assert _venue(FakeExchange()).capabilities.min_notional == Decimal(50)
        scoped = _venue(FakeExchange(), symbols=["BTC/USDT", "SOL/USDT:USDT"])
        assert scoped.capabilities.min_notional == Decimal(5)


class TestIdempotency:
    """The client order id is the only thing between a retry and a double fill."""

    async def test_the_intents_key_is_sent_as_the_client_order_id(self):
        exchange = FakeExchange()
        intent = _intent()
        await _live(exchange).execute(intent)

        sent = exchange.created[0]["params"]
        assert sent[CLIENT_ORDER_ID_PARAM] == intent.idempotency_key
        assert intent.idempotency_key  # a blank key would satisfy equality alone

    async def test_a_second_attempt_on_the_same_key_returns_the_first_order(self):
        """The venue rejecting a duplicate id is the mechanism working.

        It is resolved by reading the original order back, never by placing
        another one under a fresh id.
        """
        ccxt = pytest.importorskip("ccxt")
        intent = _intent()
        exchange = FakeExchange(
            create_error=ccxt.DuplicateOrderId("duplicate newClientOrderId"),
            fetched_order=_order(
                filled=0.001, average=10_000.0, client_order_id=intent.idempotency_key
            ),
        )

        fill = await _live(exchange).execute(intent)

        assert fill.filled_quantity == Decimal("0.001")
        assert len(exchange.created) == 1
        assert exchange.fetched[0]["params"][CLIENT_ORDER_ID_PARAM] == (
            intent.idempotency_key
        )


class TestHyperliquidCloidFormat:
    """Hyperliquid requires 0x-prefixed 128-bit hex for client order ids.

    The carry loop generates descriptive keys (portfolio:carry:timestamp:
    symbol:...) for traceability. Those fail Hyperliquid's EIP-712 signing
    path. The adapter formats them deterministically; other venues pass
    the key through unchanged.
    """

    def test_cloid_is_0x_prefixed_128_bit_hex(self):
        from omni.venue.ccxt_venue import _venue_cloid

        cloid = _venue_cloid("any descriptive key with: colons and / slashes")
        assert cloid.startswith("0x")
        hex_part = cloid[2:]
        assert len(hex_part) == 32
        int(hex_part, 16)

    def test_cloid_is_deterministic(self):
        from omni.venue.ccxt_venue import _venue_cloid

        key = "97e7737f:carry:2026-08-11T04:00:00:ETH/USDC:spot:long"
        assert _venue_cloid(key) == _venue_cloid(key)

    def test_different_keys_produce_different_cloids(self):
        from omni.venue.ccxt_venue import _venue_cloid

        a = _venue_cloid("portfolio:carry:ETH:spot:long")
        b = _venue_cloid("portfolio:carry:ETH:perp:short")
        assert a != b

    async def test_hyperliquid_venue_formats_the_cloid_on_execute(self):
        exchange = FakeExchange()
        venue = CCXTVenue(
            exchange, mode=TradingMode.LIVE, name="hyperliquid"
        )
        intent = _intent(idempotency_key="portfolio:carry:ETH:spot:long")

        await venue.execute(intent)

        from omni.venue.ccxt_venue import _venue_cloid

        sent = exchange.created[0]["params"]
        assert sent[CLIENT_ORDER_ID_PARAM] == _venue_cloid(
            "portfolio:carry:ETH:spot:long"
        )
        assert sent[CLIENT_ORDER_ID_PARAM] != "portfolio:carry:ETH:spot:long"

    async def test_non_hyperliquid_venue_passes_key_through(self):
        exchange = FakeExchange()
        venue = _live(exchange)
        intent = _intent(idempotency_key="descriptive:key:with:colons")

        await venue.execute(intent)

        sent = exchange.created[0]["params"]
        assert sent[CLIENT_ORDER_ID_PARAM] == "descriptive:key:with:colons"


class TestTimeoutDuringPlacement:
    """The one event where the venue's state and ours can genuinely diverge."""

    async def test_a_timeout_is_resolved_by_reading_not_by_retrying(self):
        ccxt = pytest.importorskip("ccxt")
        intent = _intent()
        exchange = FakeExchange(
            create_error=ccxt.RequestTimeout("read timed out"),
            fetched_order=_order(
                filled=0.001,
                average=10_050.0,
                client_order_id=intent.idempotency_key,
            ),
        )

        fill = await _live(exchange).execute(intent)

        assert len(exchange.created) == 1, "a placement is never retried"
        assert fill.filled_quantity == Decimal("0.001")
        assert fill.average_price == Decimal(10050)
        assert fill.raw["recovered_from"]

    async def test_an_unresolvable_timeout_raises_and_names_the_key(self):
        """The order may exist. Nothing may claim otherwise."""
        ccxt = pytest.importorskip("ccxt")
        intent = _intent()
        exchange = FakeExchange(
            create_error=ccxt.RequestTimeout("read timed out"),
            fetch_error=ccxt.OrderNotFound("Order does not exist"),
        )

        with pytest.raises(VenueUnavailable) as raised:
            await _live(exchange).execute(intent)

        assert intent.idempotency_key in str(raised.value)
        assert len(exchange.created) == 1

    async def test_a_timeout_with_no_way_to_read_back_raises_before_probing(self):
        ccxt = pytest.importorskip("ccxt")
        exchange = FakeExchange(
            has={**HAS, "fetchOrder": False},
            create_error=ccxt.RequestTimeout("read timed out"),
        )

        with pytest.raises(VenueUnavailable, match="fetch_order"):
            await _live(exchange).execute(_intent())

        assert exchange.fetched == []


class TestNeverFabricateAFill:
    """A fill invented from the intent writes a position that does not exist."""

    async def test_an_unreadable_filled_quantity_raises(self):
        exchange = FakeExchange(order=_order(filled=None))

        with pytest.raises(VenueUnavailable, match="filled"):
            await _live(exchange).execute(_intent())

    async def test_an_unknown_status_with_nothing_filled_raises(self):
        """`None` status and zero filled is not 'nothing happened'.

        It is 'the venue did not say', and the two have opposite consequences
        for a caller deciding whether to send the order again.
        """
        exchange = FakeExchange(order=_order(filled=0.0, status=None))

        with pytest.raises(VenueUnavailable, match="nothing filled"):
            await _live(exchange).execute(_intent())

    async def test_an_execution_with_no_usable_price_raises(self):
        exchange = FakeExchange(
            order=_order(filled=0.001, average=None, cost=None, price=None)
        )

        with pytest.raises(VenueUnavailable, match="usable price"):
            await _live(exchange).execute(_intent())

    async def test_a_rejection_is_reported_as_an_empty_fill(self):
        """A terminal status IS information, and an empty fill states it."""
        exchange = FakeExchange(order=_order(filled=0.0, status="rejected"))

        fill = await _live(exchange).execute(_intent())

        assert fill.is_empty
        assert "rejected" in fill.raw
        assert fill.raw["status"] == "rejected"

    async def test_a_resting_order_reports_zero_filled_and_keeps_its_id(self):
        exchange = FakeExchange(
            order=_order(filled=0.0, status="open", order_id="resting-7")
        )

        fill = await _live(exchange).execute(
            _intent(order_kind=OrderKind.LIMIT, limit_price=Decimal(9000))
        )

        assert fill.is_empty
        assert fill.external_id == "resting-7"
        assert fill.raw["resting"] is True

    async def test_the_fill_reports_the_venues_numbers_not_the_intents(self):
        exchange = FakeExchange(
            order=_order(filled=0.0004, average=10_123.45, order_id="partial-1")
        )

        fill = await _live(exchange).execute(_intent(quantity="0.001"))

        assert fill.filled_quantity == Decimal("0.0004")
        assert fill.average_price == Decimal("10123.45")
        assert fill.raw["partial"] is True
        assert fill.raw["requested_quantity"] == "0.001"

    async def test_a_price_is_derived_from_cost_only_from_the_venues_own_numbers(self):
        exchange = FakeExchange(
            order=_order(filled=0.002, average=None, cost=20.5, price=None)
        )

        fill = await _live(exchange).execute(_intent(quantity="0.002"))

        assert fill.average_price == Decimal(10250)


class TestPrecision:
    """An unrounded amount is rejected by the exchange, so rounding is the path."""

    async def test_the_amount_is_rounded_before_it_is_submitted(self):
        exchange = FakeExchange()
        intent = _intent(quantity="0.001234567")

        fill = await _live(exchange).execute(intent)

        submitted = exchange.created[0]["amount"]
        assert submitted == float(Decimal("0.00123")), "BTC/USDT takes 5 decimals of size"
        assert submitted != intent.quantity
        assert fill.filled_quantity == Decimal("0.00123")
        assert fill.raw["submitted_quantity"] == "0.00123"

    async def test_a_limit_price_is_rounded_too(self):
        exchange = FakeExchange(order=_order(filled=0.001, average=9000.12))

        await _live(exchange).execute(
            _intent(order_kind=OrderKind.LIMIT, limit_price=Decimal("9000.129"))
        )

        assert exchange.created[0]["price"] == float(Decimal("9000.12"))

    async def test_a_size_that_rounds_away_is_not_submitted(self):
        exchange = FakeExchange()

        with pytest.raises(VenueUnavailable, match="rounds to"):
            await _live(exchange).execute(_intent(quantity="0.000001"))

        assert exchange.created == []


class TestCapabilitiesComeFromTheExchange:
    async def test_fees_and_market_types_are_read_not_assumed(self):
        caps = _venue(FakeExchange()).capabilities

        assert caps.taker_fee_bps == Decimal(10)
        assert caps.maker_fee_bps == Decimal(10)
        assert caps.spot is True
        assert caps.perpetuals is True
        assert caps.shorting is True
        assert caps.funding_data is True
        assert caps.limit_orders is True

    async def test_a_spot_only_scope_declares_no_perpetuals(self):
        caps = _venue(FakeExchange(), symbols=["BTC/USDT"]).capabilities

        assert caps.perpetuals is False
        assert caps.spot is True

    async def test_a_venue_publishing_no_fee_is_refused(self):
        """A fee this tier invents is a fee no expectancy is measured against."""
        with pytest.raises(ValueError, match="trading fee"):
            _venue(FakeExchange(fees={"trading": {}}))

    async def test_unloaded_markets_are_refused(self):
        with pytest.raises(ValueError, match="markets"):
            _venue(FakeExchange(markets={}))

    async def test_a_symbol_the_venue_does_not_list_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="DOGE/USDT"):
            _venue(FakeExchange(), symbols=["BTC/USDT", "DOGE/USDT"])

    async def test_it_satisfies_the_venue_protocol(self):
        assert isinstance(_venue(FakeExchange()), Venue)


class TestSymbolAndMarketTypeAgree:
    async def test_a_perpetual_intent_on_the_spot_symbol_is_refused(self):
        """`BTC/USDT` and `BTC/USDT:USDT` are four characters and one balance
        sheet apart, and a carry book holds both at once."""
        exchange = FakeExchange()

        with pytest.raises(VenueUnavailable, match="spot market"):
            await _live(exchange).execute(
                _intent(symbol="BTC/USDT", market_type=MarketType.PERPETUAL)
            )

        assert exchange.created == []

    async def test_a_spot_intent_on_the_perpetual_symbol_is_refused(self):
        exchange = FakeExchange()

        with pytest.raises(VenueUnavailable, match="swap market"):
            await _live(exchange).execute(
                _intent(symbol="BTC/USDT:USDT", market_type=MarketType.SPOT)
            )

        assert exchange.created == []

    async def test_reduce_only_is_silently_dropped_on_spot(self):
        exchange = FakeExchange()

        fill = await _live(exchange).execute(_intent(reduce_only=True))

        assert not fill.is_empty
        assert exchange.created[0]["params"] == {
            CLIENT_ORDER_ID_PARAM: exchange.created[0]["params"][CLIENT_ORDER_ID_PARAM]
        }

    async def test_reduce_only_reaches_the_venue_for_a_perpetual(self):
        exchange = FakeExchange()

        await _live(exchange).execute(
            _intent(
                symbol="BTC/USDT:USDT",
                market_type=MarketType.PERPETUAL,
                side=Side.SELL,
                quantity="0.01",
                reduce_only=True,
            )
        )

        assert exchange.created[0]["params"]["reduceOnly"] is True


class TestQuote:
    async def test_a_market_buy_is_quoted_at_the_offer_and_pays_the_half_spread(self):
        exchange = FakeExchange(ticker={"bid": 9990.0, "ask": 10010.0, "timestamp": TS})

        quote = await _venue(exchange).quote(_intent(quantity="1"))

        assert quote.expected_price == Decimal(10010)
        assert quote.slippage == Decimal(10)
        assert quote.fee == Decimal("10.010")
        assert quote.gas == Decimal(0)
        assert quote.as_of == AT

    async def test_a_market_sell_is_quoted_at_the_bid(self):
        exchange = FakeExchange(ticker={"bid": 9990.0, "ask": 10010.0, "timestamp": TS})

        quote = await _venue(exchange).quote(_intent(quantity="1", side=Side.SELL))

        assert quote.expected_price == Decimal(9990)
        assert quote.slippage == Decimal(10)

    async def test_a_resting_limit_is_not_charged_slippage_it_does_not_pay(self):
        exchange = FakeExchange(ticker={"bid": 9990.0, "ask": 10010.0, "timestamp": TS})

        quote = await _venue(exchange).quote(
            _intent(quantity="1", order_kind=OrderKind.LIMIT, limit_price=Decimal(9500))
        )

        assert quote.expected_price == Decimal(9500)
        assert quote.slippage == Decimal(0)

    async def test_a_one_sided_book_cannot_be_quoted(self):
        exchange = FakeExchange(ticker={"bid": 9990.0, "ask": None, "timestamp": TS})

        with pytest.raises(VenueUnavailable, match="two-sided"):
            await _venue(exchange).quote(_intent())

    async def test_a_ticker_without_a_side_falls_back_to_the_order_book(self):
        """Hyperliquid publishes bid/ask on its perpetuals and neither on SPOT.

        Refusing there would leave `quote` unable to price half of every
        cash-and-carry pair on the one venue this book trades, while the same
        two numbers sit at the top of the order book. Measured 2026-08-10:
        SOL/USDC ticker bid=None ask=None, book bid=76.861 ask=76.862.
        """
        exchange = FakeExchange(
            ticker={"bid": None, "ask": None, "timestamp": TS},
            order_book={"bids": [[9990.0, 5.0]], "asks": [[10010.0, 5.0]]},
        )

        quote = await _venue(exchange).quote(_intent())

        assert quote.expected_price == Decimal(10010)

    async def test_a_market_order_is_priced_by_walking_the_book(self):
        """The touch is the small order's price, not every order's.

        Measured on Hyperliquid: PURR quoted 83 bps from the touch and cost 121
        when $70 was walked through it. A cost model that charges the touch for
        any size understates exactly the names where size matters.
        """
        exchange = FakeExchange(
            ticker={"bid": 9990.0, "ask": 10000.0, "timestamp": TS},
            order_book={
                "bids": [[9990.0, 1.0]],
                # One unit at the touch, then the book thins sharply.
                "asks": [[10000.0, 1.0], [10100.0, 1.0], [10500.0, 8.0]],
            },
        )

        touch = await _venue(exchange).quote(_intent(quantity="1"))
        deep = await _venue(exchange).quote(_intent(quantity="3"))

        assert touch.expected_price == Decimal(10000)
        # 1 @ 10000 + 1 @ 10100 + 1 @ 10500 = 30600 / 3
        assert deep.expected_price == Decimal(10200)
        assert deep.slippage > touch.slippage * 3

    async def test_a_size_the_book_cannot_fill_falls_back_to_the_touch(self):
        """Not a guess at invisible depth. An order the book cannot fill is a
        refusal `execute` makes; inventing a worse price here would be the cost
        model imagining levels it cannot see."""
        exchange = FakeExchange(
            ticker={"bid": 9990.0, "ask": 10000.0, "timestamp": TS},
            order_book={"bids": [[9990.0, 1.0]], "asks": [[10000.0, 1.0]]},
        )

        quote = await _venue(exchange).quote(_intent(quantity="500"))

        assert quote.expected_price == Decimal(10000)

    async def test_a_ticker_and_book_both_one_sided_still_refuse(self):
        exchange = FakeExchange(
            ticker={"bid": 9990.0, "ask": None, "timestamp": TS},
            order_book={"bids": [[9990.0, 5.0]], "asks": []},
        )

        with pytest.raises(VenueUnavailable, match="two-sided"):
            await _venue(exchange).quote(_intent())

    async def test_quoting_commits_nothing(self):
        exchange = FakeExchange(ticker={"bid": 9990.0, "ask": 10010.0, "timestamp": TS})

        await _venue(exchange).quote(_intent())

        assert exchange.created == []


class TestPositions:
    async def test_a_flat_row_is_not_an_open_position(self):
        exchange = FakeExchange(
            positions=[
                {"symbol": "BTC/USDT:USDT", "contracts": 0.0, "side": "long",
                 "entryPrice": 0.0, "timestamp": TS},
                {"symbol": "SOL/USDT:USDT", "contracts": 12.0, "side": "short",
                 "entryPrice": 140.5, "timestamp": TS},
            ]
        )

        positions = await _venue(exchange).positions()

        assert len(positions) == 1
        assert positions[0].symbol == "SOL/USDT:USDT"
        assert positions[0].quantity == Decimal(-12)
        assert positions[0].average_entry == Decimal("140.5")
        assert positions[0].market_type is MarketType.PERPETUAL

    async def test_a_position_with_no_entry_price_raises_rather_than_being_dropped(self):
        """Dropping it would report a book flatter than it is."""
        exchange = FakeExchange(
            positions=[
                {"symbol": "BTC/USDT:USDT", "contracts": 0.4, "side": "long",
                 "entryPrice": None, "timestamp": TS}
            ]
        )

        with pytest.raises(VenueUnavailable, match="entryPrice"):
            await _venue(exchange).positions()

    async def test_a_position_with_no_direction_raises(self):
        exchange = FakeExchange(
            positions=[
                {"symbol": "BTC/USDT:USDT", "contracts": 0.4, "side": None,
                 "entryPrice": 61000.0, "timestamp": TS}
            ]
        )

        with pytest.raises(VenueUnavailable, match="direction"):
            await _venue(exchange).positions()

    async def test_a_spot_only_scope_has_no_positions_to_report(self):
        venue = _venue(FakeExchange(), symbols=["BTC/USDT"])

        assert await venue.positions() == []

    async def test_perpetuals_without_a_positions_endpoint_raise(self):
        exchange = FakeExchange(has={**HAS, "fetchPositions": False})

        with pytest.raises(VenueUnavailable, match="fetch_positions"):
            await _venue(exchange).positions()


class TestBalances:
    async def test_free_and_locked_are_reported_separately(self):
        exchange = FakeExchange(
            balance={
                "total": {"USDT": 120.5, "BTC": 0.0},
                "USDT": {"free": 100.5, "used": 20.0, "total": 120.5},
                "BTC": {"free": 0.0, "used": 0.0, "total": 0.0},
            }
        )

        balances = await _venue(exchange).balances()

        assert [(b.asset, b.free, b.locked) for b in balances] == [
            ("USDT", Decimal("100.5"), Decimal(20))
        ]

    async def test_a_negative_balance_is_a_sign_error_and_is_refused(self):
        exchange = FakeExchange(
            balance={
                "total": {"USDT": -5.0},
                "USDT": {"free": -5.0, "used": 0.0, "total": -5.0},
            }
        )

        with pytest.raises(VenueUnavailable, match="negative balance"):
            await _venue(exchange).balances()

    async def test_an_unreadable_balance_is_not_a_balance_of_zero(self):
        exchange = FakeExchange(
            balance={"total": {"USDT": 10.0}, "USDT": {"free": None, "used": 0.0}}
        )

        with pytest.raises(VenueUnavailable, match="not a balance of zero"):
            await _venue(exchange).balances()

    async def test_a_total_with_no_breakdown_is_not_silently_dropped(self):
        """Skipping it would report holding less than the venue says we hold."""
        exchange = FakeExchange(balance={"total": {"USDT": 10.0}})

        with pytest.raises(VenueUnavailable, match="no free/used"):
            await _venue(exchange).balances()


class TestCancel:
    async def test_a_cancelled_order_reports_true(self):
        exchange = FakeExchange()
        venue = _live(exchange)
        fill = await venue.execute(_intent())

        assert await venue.cancel(fill.external_id) is True
        assert exchange.cancelled == [("venue-1", "BTC/USDT")]

    async def test_an_order_that_filled_instead_of_cancelling_reports_false(self):
        exchange = FakeExchange(cancel_result={"id": "venue-1", "status": "closed"})
        venue = _live(exchange)
        fill = await venue.execute(_intent())

        assert await venue.cancel(fill.external_id) is False

    async def test_an_order_already_gone_reports_false(self):
        ccxt = pytest.importorskip("ccxt")
        exchange = FakeExchange(cancel_error=ccxt.OrderNotFound("unknown order"))
        venue = _live(exchange)
        fill = await venue.execute(_intent())

        assert await venue.cancel(fill.external_id) is False

    async def test_an_unknown_id_raises_rather_than_reporting_nothing_to_cancel(self):
        """False would mean 'there was nothing to cancel'. An order this
        adapter cannot place a symbol against is the opposite of that."""
        venue = _live(FakeExchange())

        with pytest.raises(VenueUnavailable, match="never placed it"):
            await venue.cancel("someone-elses-order")


class TestWalletVenuesAuthenticateDifferently:
    """Hyperliquid takes a wallet, not a key pair, and `connect` must not care.

    The credential object carries its own ccxt shape so that adding a venue that
    authenticates differently does not mean teaching `connect` about it. What
    `connect` does owe is a refusal to accept two sources for one field: with
    both passed, which wins is a precedence question nobody stated, and the
    losing one is a credential the operator believes is in use.
    """

    async def test_passing_both_shapes_is_refused_before_any_network_call(self):
        from omni.venue.ccxt_venue import CCXTVenue
        from omni.venue.credentials import WalletCredentials

        wallet = WalletCredentials(
            venue="hyperliquid",
            wallet_address="0x" + "a1" * 20,
            private_key="0x" + "b2" * 32,
        )

        with pytest.raises(ValueError, match="not both"):
            await CCXTVenue.connect(
                venue="hyperliquid", credentials=wallet, api_key="k"
            )

    def test_each_credential_shape_reports_what_its_venue_requires(self):
        """The mapping asserted against ccxt itself rather than against a memory.

        `requiredCredentials` is the venue's own statement of what it needs; if
        ccxt changes it, this fails rather than the first live order.
        """
        import ccxt

        from omni.venue.credentials import TradingCredentials, WalletCredentials

        wallet = WalletCredentials(
            venue="hyperliquid",
            wallet_address="0x" + "a1" * 20,
            private_key="0x" + "b2" * 32,
        )
        key_pair = TradingCredentials(
            venue="binance", api_key="k", api_secret="s"
        )

        for creds, exchange_cls in (
            (wallet, ccxt.hyperliquid),
            (key_pair, ccxt.binance),
        ):
            required = {
                name
                for name, needed in exchange_cls().requiredCredentials.items()
                if needed
            }
            assert required <= set(creds.ccxt_options()), (
                f"{creds.venue} needs {required}, "
                f"credentials supply {set(creds.ccxt_options())}"
            )


class TestAVenueThatPricesEachMarketRatherThanItself:
    """Hyperliquid reports `fees.trading` as all None and prices each market.

    Reading only the venue-level default refuses a venue that does in fact
    state its fees -- which is what happened: `CCXTVenue.connect(venue=
    "hyperliquid")` raised "publishes no usable trading fee" against a venue
    whose markets carry 4/7 bps spot and 1.5/4.5 bps perpetual.
    """

    def _priced(self, *, spot_taker: float, perp_taker: float) -> dict:
        spot = {**SPOT_BTC, "maker": spot_taker / 2, "taker": spot_taker}
        perp = {**PERP_BTC, "maker": perp_taker / 2, "taker": perp_taker}
        return {m["symbol"]: m for m in (spot, perp)}

    async def test_per_market_fees_are_read_when_the_venue_publishes_none(self):
        venue = _venue(
            FakeExchange(
                fees={"trading": {"maker": None, "taker": None}},
                markets=self._priced(spot_taker=0.0007, perp_taker=0.00045),
            )
        )

        assert venue.capabilities.taker_fee_bps == Decimal(7)
        assert venue.capabilities.maker_fee_bps == Decimal("3.5")

    async def test_the_dearest_market_sets_the_fee_not_the_average(self):
        """`Capabilities` carries one fee and it is charged to every leg.

        A delta-neutral pair always holds one spot and one perpetual, so an
        average undercharges the dearer leg on every single trade. Overcharging
        refuses marginal trades, which is the direction that cannot lose money.
        """
        venue = _venue(
            FakeExchange(
                fees={"trading": {}},
                markets=self._priced(spot_taker=0.0007, perp_taker=0.00045),
            )
        )

        assert venue.capabilities.taker_fee_bps == Decimal(7)

    async def test_a_venue_priced_nowhere_at_all_is_still_refused(self):
        # The fallback must not become a way for an unpriced venue to slip
        # through with a zero.
        with pytest.raises(ValueError, match="markets in scope"):
            _venue(FakeExchange(fees={"trading": {}}))


class TestConnectDoesNotLeakTheSessionItOpened:
    async def test_a_venue_that_fails_validation_is_closed(self, monkeypatch):
        """`load_markets` succeeds, then the constructor rejects the venue.

        Nothing owns the exchange at that point, so without an explicit close
        the aiohttp session leaks and its unclosed-connector warning buries the
        error that actually explains the failure.
        """
        import ccxt.async_support as ccxt_async

        closed: list[bool] = []

        class _Unpriced:
            id = "unpriced"
            has = HAS
            fees = {"trading": {}}
            markets = {k: {**v} for k, v in MARKETS.items()}

            def __init__(self, options):
                self.options = options

            async def load_markets(self):
                return self.markets

            async def close(self):
                closed.append(True)

        monkeypatch.setattr(ccxt_async, "unpriced", _Unpriced, raising=False)

        with pytest.raises(ValueError, match="trading fee"):
            await CCXTVenue.connect(venue="unpriced")

        assert closed == [True], "connect left the exchange session open"
