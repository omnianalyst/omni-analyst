"""The venue protocol's self-validating value objects.

The assertions worth having here are the ones that catch a *coherent-looking*
mistake, because an incoherent one fails on its own. Three are load-bearing:

1. **Inverted barriers on a short.** A bridge that reads a `down` prediction,
   writes `Side.SELL`, and assigns the prediction's `lower_barrier` to
   `stop_price` produces a short whose stop is below its entry -- a take profit
   wearing a stop's name. Both orderings satisfy the schema's
   `prediction_barriers_straddle_entry`, so the database cannot catch it.

2. **A fill with a quantity but no price.** v1's `ibkr_integration.py` faked
   fills at a hardcoded 150.0 when its SDK was absent. A `Fill` that accepts a
   zero or negative `average_price` alongside a non-zero quantity is the same
   defect with a different constant.

3. **Perpetuals without shorting.** A venue declaring perps it cannot short
   would be selected by the router for a carry leg it then cannot open.

Each of these is checked by constructing the wrong thing and asserting the
refusal, not by constructing the right thing and asserting it works.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from omni.portfolio.state import CashPosition
from omni.venue.protocol import (
    Balance,
    Capabilities,
    Fill,
    InvalidIntent,
    MarketType,
    OrderKind,
    Position,
    Quote,
    Side,
    TradeIntent,
    Venue,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _caps(**overrides) -> Capabilities:
    base = {
        "spot": True,
        "margin": False,
        "perpetuals": False,
        "limit_orders": True,
        "shorting": False,
        "funding_data": False,
        "maker_fee_bps": Decimal(2),
        "taker_fee_bps": Decimal(10),
        "min_notional": Decimal(10),
    }
    return Capabilities(**{**base, **overrides})


def _long(**overrides) -> TradeIntent:
    base = {
        "venue": "paper",
        "symbol": "BTC/USD",
        "side": Side.BUY,
        "market_type": MarketType.SPOT,
        "quantity": Decimal("0.5"),
        "reference_price": Decimal(100),
        "stop_price": Decimal(98),
        "take_profit_price": Decimal(103),
    }
    return TradeIntent(**{**base, **overrides})


class TestCapabilities:
    def test_a_venue_offering_perpetuals_must_support_shorting(self):
        with pytest.raises(ValueError, match="perpetuals must support shorting"):
            _caps(perpetuals=True, shorting=False)

    def test_perpetuals_with_shorting_is_accepted(self):
        caps = _caps(perpetuals=True, shorting=True)
        assert caps.supports(MarketType.PERPETUAL)

    def test_supports_reports_each_market_type_independently(self):
        caps = _caps(spot=True, margin=False, perpetuals=True, shorting=True)
        assert caps.supports(MarketType.SPOT) is True
        assert caps.supports(MarketType.MARGIN) is False
        assert caps.supports(MarketType.PERPETUAL) is True

    def test_negative_fees_are_refused(self):
        with pytest.raises(ValueError, match="negative fees"):
            _caps(maker_fee_bps=Decimal(-2))

    def test_negative_min_notional_is_refused(self):
        with pytest.raises(ValueError, match="min_notional"):
            _caps(min_notional=Decimal(-1))


class TestTradeIntentBarriers:
    def test_long_accepts_stop_below_and_target_above(self):
        intent = _long()
        assert intent.stop_price < intent.reference_price < intent.take_profit_price

    def test_short_accepts_stop_above_and_target_below(self):
        intent = _long(
            side=Side.SELL,
            stop_price=Decimal(103),
            take_profit_price=Decimal(98),
        )
        assert intent.take_profit_price < intent.reference_price < intent.stop_price

    def test_short_refuses_the_barriers_a_long_would_use(self):
        # The exact inversion the bridge is at risk of: `down` -> SELL, but the
        # prediction's lower_barrier assigned to stop_price out of habit. The
        # refusal names take_profit_price because for a short that is the
        # barrier required to sit below entry, and 103 does not.
        with pytest.raises(InvalidIntent, match="take_profit_price 103 must be below"):
            _long(
                side=Side.SELL,
                stop_price=Decimal(98),
                take_profit_price=Decimal(103),
            )

    def test_long_refuses_the_barriers_a_short_would_use(self):
        with pytest.raises(InvalidIntent, match="stop_price"):
            _long(stop_price=Decimal(103), take_profit_price=Decimal(98))

    def test_stop_equal_to_reference_is_refused(self):
        with pytest.raises(InvalidIntent, match="must be below"):
            _long(stop_price=Decimal(100))

    def test_target_equal_to_reference_is_refused(self):
        with pytest.raises(InvalidIntent, match="must be above"):
            _long(take_profit_price=Decimal(100))

    def test_barriers_are_optional(self):
        intent = _long(stop_price=None, take_profit_price=None)
        assert intent.stop_price is None
        assert intent.take_profit_price is None

    def test_one_barrier_alone_is_still_validated(self):
        with pytest.raises(InvalidIntent):
            _long(stop_price=Decimal(105), take_profit_price=None)


class TestTradeIntentShape:
    def test_zero_quantity_is_refused(self):
        with pytest.raises(InvalidIntent, match="quantity must be positive"):
            _long(quantity=Decimal(0))

    def test_negative_quantity_is_refused_because_side_carries_direction(self):
        with pytest.raises(InvalidIntent, match="direction is carried by"):
            _long(quantity=Decimal(-1))

    def test_limit_order_without_a_price_is_refused(self):
        with pytest.raises(InvalidIntent, match="limit order requires"):
            _long(order_kind=OrderKind.LIMIT, limit_price=None)

    def test_market_order_with_a_limit_price_is_refused(self):
        with pytest.raises(InvalidIntent, match="market order carries no limit_price"):
            _long(order_kind=OrderKind.MARKET, limit_price=Decimal(99))

    def test_notional_is_quantity_times_reference(self):
        assert _long(
            quantity=Decimal("0.5"), reference_price=Decimal(100)
        ).notional == Decimal(50)

    def test_each_intent_gets_a_distinct_idempotency_key(self):
        assert _long().idempotency_key != _long().idempotency_key

    def test_an_explicit_idempotency_key_is_preserved(self):
        assert _long(idempotency_key="fixed").idempotency_key == "fixed"


class TestQuote:
    def _quote(self, **overrides) -> Quote:
        base = {
            "intent": _long(quantity=Decimal(1), reference_price=Decimal(100)),
            "expected_price": Decimal(100),
            "fee": Decimal("0.10"),
            "slippage": Decimal("0.05"),
            "gas": Decimal(0),
            "as_of": NOW,
        }
        return Quote(**{**base, **overrides})

    def test_total_cost_sums_the_components(self):
        assert self._quote().total_cost == Decimal("0.15")

    def test_cost_bps_is_against_executed_notional_not_reference_notional(self):
        # A quote whose expected price differs from the intent's reference must
        # express cost against what would actually be paid; using the reference
        # notional understates cost exactly when the market has moved away.
        quote = self._quote(expected_price=Decimal(200))
        assert quote.quantity_notional == Decimal(200)
        assert quote.cost_bps == Decimal("0.15") / Decimal(200) * Decimal(10_000)

    def test_negative_slippage_is_refused(self):
        with pytest.raises(ValueError, match="slippage"):
            self._quote(slippage=Decimal("-0.01"))

    def test_zero_expected_price_is_refused(self):
        with pytest.raises(ValueError, match="expected_price"):
            self._quote(expected_price=Decimal(0))


class TestFill:
    def _fill(self, **overrides) -> Fill:
        base = {
            "intent_id": "abc",
            "venue": "paper",
            "symbol": "BTC/USD",
            "side": Side.BUY,
            "filled_quantity": Decimal("0.5"),
            "average_price": Decimal(100),
            "fee_paid": Decimal("0.05"),
            "filled_at": NOW,
        }
        return Fill(**{**base, **overrides})

    def test_a_filled_quantity_without_a_price_is_refused(self):
        with pytest.raises(ValueError, match="needs a real average_price"):
            self._fill(average_price=Decimal(0))

    def test_a_negative_price_is_refused(self):
        with pytest.raises(ValueError, match="needs a real average_price"):
            self._fill(average_price=Decimal(-100))

    def test_an_empty_fill_needs_no_price(self):
        fill = self._fill(filled_quantity=Decimal(0), average_price=Decimal(0))
        assert fill.is_empty
        assert fill.notional == Decimal(0)

    def test_negative_filled_quantity_is_refused(self):
        with pytest.raises(ValueError, match="filled_quantity"):
            self._fill(filled_quantity=Decimal(-1))

    def test_notional_uses_the_executed_price_not_the_requested_one(self):
        assert self._fill(
            filled_quantity=Decimal(2), average_price=Decimal(101)
        ).notional == Decimal(202)


class TestPosition:
    def _position(self, **overrides) -> Position:
        base = {
            "venue": "paper",
            "symbol": "BTC/USD",
            "market_type": MarketType.SPOT,
            "quantity": Decimal(1),
            "average_entry": Decimal(100),
            "as_of": NOW,
        }
        return Position(**{**base, **overrides})

    def test_negative_quantity_is_a_short_not_an_error(self):
        assert self._position(quantity=Decimal(-1)).is_short

    def test_notional_is_absolute_so_a_short_has_positive_exposure(self):
        assert self._position(quantity=Decimal(-2)).notional == Decimal(200)

    def test_an_open_position_without_an_entry_price_is_refused(self):
        with pytest.raises(ValueError, match="needs a real average_entry"):
            self._position(quantity=Decimal(1), average_entry=Decimal(0))

    def test_a_flat_position_needs_no_entry_price(self):
        assert self._position(
            quantity=Decimal(0), average_entry=Decimal(0)
        ).notional == Decimal(0)


class TestBalance:
    def test_total_sums_free_and_locked(self):
        balance = Balance(
            venue="paper",
            asset="USD",
            free=Decimal(100),
            locked=Decimal(25),
            as_of=NOW,
        )
        assert balance.total == Decimal(125)

    def test_negative_balance_is_refused(self):
        with pytest.raises(ValueError, match="must not be negative"):
            Balance(
                venue="paper",
                asset="USD",
                free=Decimal(-1),
                locked=Decimal(0),
                as_of=NOW,
            )

    def test_a_venue_balance_is_not_the_local_cash_row(self):
        """The overdraw migration 033 stores is refused here, and stored there.

        Bridging the two by relaxing this guard is the mistake this pins: a
        local book may be overdrawn by 200, a venue's `free` may not, and the
        type that admits both is the type that lets an adapter's sign error
        through as a real reading.
        """
        overdrawn = CashPosition(
            venue="paper",
            asset="USD",
            free=Decimal(-200),
            locked=Decimal(0),
            as_of=NOW,
        )
        assert overdrawn.total == Decimal(-200)

        with pytest.raises(ValueError, match="must not be negative"):
            Balance(
                venue="paper",
                asset="USD",
                free=overdrawn.free,
                locked=overdrawn.locked,
                as_of=NOW,
            )


class TestVenueProtocol:
    def test_an_object_missing_execute_is_not_a_venue(self):
        class Incomplete:
            name = "incomplete"
            capabilities = _caps()

            async def quote(self, intent): ...
            async def positions(self): ...
            async def balances(self): ...
            async def cancel(self, external_id): ...

        assert not isinstance(Incomplete(), Venue)

    def test_an_object_with_the_full_surface_is_a_venue(self):
        class Complete:
            name = "complete"
            capabilities = _caps()

            # An asset is not a symbol. A venue that cannot say what it calls
            # `BTC` is one a strategy has to guess at, and the guess put a bare
            # ticker into a TradeIntent (Finding 21).
            def symbol_for(self, asset, market_type): ...

            # The raw spellings a HELD position may carry beside the tradable
            # symbol (Hyperliquid reports UETH/USDC for ETH/USDC). Empty is the
            # honest answer for a venue with one spelling per market.
            def held_symbol_aliases(self, asset, market_type): return ()

            async def quote(self, intent): ...
            async def execute(self, intent): ...
            async def positions(self): ...
            async def balances(self): ...
            async def cancel(self, external_id): ...

        assert isinstance(Complete(), Venue)

    def test_a_venue_that_cannot_name_its_own_symbols_is_incomplete(self):
        # The surface above, minus the resolver. Protocol conformance is the
        # only thing standing between a new adapter and a strategy composing
        # `f"{asset}/USD"` on its behalf.
        class NoResolver:
            name = "no-resolver"
            capabilities = _caps()

            async def quote(self, intent): ...
            async def execute(self, intent): ...
            async def positions(self): ...
            async def balances(self): ...
            async def cancel(self, external_id): ...

        assert not isinstance(NoResolver(), Venue)
