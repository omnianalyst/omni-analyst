"""The order ledger. Every test here is about the trail being complete.

Portfolio state is a projection of `trade_order`, so the two ways this module
can lie are a duplicate order (one decision recorded twice, double size) and a
fill whose average price is the last price rather than the weighted one (an
entry misstated on every partially filled order). Both have their own class
below, and both are asserted against a value that no simpler implementation
produces: the weighted average of 6-at-100 and 4-at-150 is 120, which is
neither the last price (150) nor the unweighted mean (125).
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

import pytest

from omni.portfolio.orders import (
    LEGAL_TRANSITIONS,
    IllegalTransition,
    Order,
    OrderLedgerError,
    OrderStatus,
    UnknownOrder,
    get,
    open_orders,
    record_fill,
    record_intent,
    transition,
)
from omni.venue.protocol import Fill, MarketType, OrderKind, Side, TradeIntent

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE trade_order CASCADE")
    yield


def _intent(**overrides) -> TradeIntent:
    fields = {
        "venue": "paper",
        "symbol": "BTC/USD",
        "side": Side.BUY,
        "market_type": MarketType.SPOT,
        "quantity": Decimal(10),
        "reference_price": Decimal(100),
        "provenance": {"method": "trend.sma", "prediction_id": "p-1"},
    }
    fields.update(overrides)
    return TradeIntent(**fields)


def _fill(quantity, price, *, fee=Decimal(0), external_id=None, raw=None) -> Fill:
    return Fill(
        intent_id="i-1",
        venue="paper",
        symbol="BTC/USD",
        side=Side.BUY,
        filled_quantity=quantity,
        average_price=price,
        fee_paid=fee,
        filled_at=NOW,
        external_id=external_id,
        raw=raw or {},
    )


async def _events(db, order_id):
    return await db.pool.fetch(
        "SELECT status, external_id, payload FROM order_event "
        "WHERE order_id = $1 ORDER BY at, id",
        order_id,
    )


async def _order_count(db):
    return await db.pool.fetchval("SELECT count(*) FROM trade_order")


class TestEnumParity:
    """The Python enum and the SQL enum are one state machine, not two."""

    async def test_python_and_sql_members_are_identical(self, db):
        rows = await db.pool.fetch(
            "SELECT e.enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'order_status' ORDER BY e.enumsortorder"
        )
        sql_members = [r["enumlabel"] for r in rows]
        assert sql_members == [s.value for s in OrderStatus]

    def test_every_status_has_a_declared_transition_set(self):
        assert set(LEGAL_TRANSITIONS) == set(OrderStatus)


class TestIdempotency:
    """One decision, one order, however many times it is recorded."""

    async def test_recording_the_same_intent_twice_returns_the_first_id(self, db):
        intent = _intent()
        first = await record_intent(db.pool, uuid4(), intent)
        second = await record_intent(db.pool, uuid4(), intent)
        assert second == first

    async def test_a_duplicate_creates_no_second_row(self, db):
        intent = _intent()
        await record_intent(db.pool, uuid4(), intent)
        await record_intent(db.pool, uuid4(), intent)
        assert await _order_count(db) == 1

    async def test_a_duplicate_creates_no_second_event(self, db):
        intent = _intent()
        order_id = await record_intent(db.pool, uuid4(), intent)
        await record_intent(db.pool, uuid4(), intent)
        events = await _events(db, order_id)
        assert [e["status"] for e in events] == ["intent"]

    async def test_a_duplicate_does_not_repoint_the_order_at_a_new_portfolio(self, db):
        intent = _intent()
        owner = uuid4()
        order_id = await record_intent(db.pool, owner, intent)
        await record_intent(db.pool, uuid4(), intent)
        order = await get(db.pool, order_id)
        assert order.portfolio_id == owner

    async def test_distinct_keys_are_distinct_orders(self, db):
        portfolio = uuid4()
        first = await record_intent(db.pool, portfolio, _intent(idempotency_key="a"))
        second = await record_intent(db.pool, portfolio, _intent(idempotency_key="b"))
        assert first != second
        assert await _order_count(db) == 2

    async def test_concurrent_duplicates_collapse_to_one_order(self, db):
        intent = _intent()
        portfolio = uuid4()
        ids = await asyncio.gather(
            *(record_intent(db.pool, portfolio, intent) for _ in range(5))
        )
        assert len(set(ids)) == 1
        assert await _order_count(db) == 1


class TestIntentIsRecordedFaithfully:
    async def test_the_stored_order_carries_the_intent_as_stated(self, db):
        intent = _intent(
            side=Side.SELL,
            market_type=MarketType.PERPETUAL,
            order_kind=OrderKind.LIMIT,
            quantity=Decimal("0.125"),
            reference_price=Decimal("41000.50"),
            limit_price=Decimal(41100),
            stop_price=Decimal(42000),
            take_profit_price=Decimal(39000),
            expires_at=NOW + timedelta(hours=6),
        )
        order_id = await record_intent(db.pool, uuid4(), intent)
        order = await get(db.pool, order_id)

        assert order.side is Side.SELL
        assert order.market_type is MarketType.PERPETUAL
        assert order.order_kind is OrderKind.LIMIT
        assert order.quantity == Decimal("0.125")
        assert order.reference_price == Decimal("41000.50")
        assert order.limit_price == Decimal(41100)
        assert order.stop_price == Decimal(42000)
        assert order.take_profit_price == Decimal(39000)
        assert order.expires_at == NOW + timedelta(hours=6)

    async def test_money_survives_as_decimal_not_float(self, db):
        intent = _intent(quantity=Decimal("0.1"), reference_price=Decimal("0.3"))
        order = await get(db.pool, await record_intent(db.pool, uuid4(), intent))
        assert isinstance(order.quantity, Decimal)
        assert order.quantity * Decimal(3) == Decimal("0.3")

    async def test_a_new_order_starts_at_intent_with_nothing_filled(self, db):
        order = await get(db.pool, await record_intent(db.pool, uuid4(), _intent()))
        assert order.status is OrderStatus.INTENT
        assert order.filled_quantity == Decimal(0)
        assert order.average_fill_price is None
        assert order.remaining_quantity == Decimal(10)

    async def test_provenance_round_trips(self, db):
        intent = _intent(provenance={"method": "carry.funding", "confidence": "0.71"})
        order = await get(db.pool, await record_intent(db.pool, uuid4(), intent))
        assert order.provenance == {"method": "carry.funding", "confidence": "0.71"}

    async def test_the_intent_event_captures_the_instruction(self, db):
        intent = _intent(quantity=Decimal("2.5"), reference_price=Decimal("99.75"))
        order_id = await record_intent(db.pool, uuid4(), intent)
        (event,) = await _events(db, order_id)
        payload = json.loads(event["payload"])
        assert payload["intent"]["quantity"] == "2.5"
        assert payload["intent"]["reference_price"] == "99.75"


class TestTransitions:
    async def test_a_legal_transition_moves_the_order_and_logs_it(self, db):
        order_id = await record_intent(db.pool, uuid4(), _intent())
        await transition(
            db.pool,
            order_id,
            OrderStatus.SUBMITTED,
            external_id="X-1",
            payload={"sent": True},
        )
        order = await get(db.pool, order_id)
        assert order.status is OrderStatus.SUBMITTED
        assert order.external_id == "X-1"

        events = await _events(db, order_id)
        assert [e["status"] for e in events] == ["intent", "submitted"]
        assert events[1]["external_id"] == "X-1"

    async def test_a_rejection_keeps_the_venue_reason(self, db):
        order_id = await record_intent(db.pool, uuid4(), _intent())
        await transition(db.pool, order_id, OrderStatus.SUBMITTED)
        await transition(
            db.pool,
            order_id,
            OrderStatus.REJECTED,
            payload={"code": 4001, "msg": "insufficient balance"},
        )
        events = await _events(db, order_id)
        assert json.loads(events[-1]["payload"])["msg"] == "insufficient balance"

    async def test_a_backwards_transition_raises(self, db):
        order_id = await record_intent(db.pool, uuid4(), _intent())
        await transition(db.pool, order_id, OrderStatus.SUBMITTED)
        await record_fill(db.pool, order_id, _fill(Decimal(10), Decimal(100)))

        with pytest.raises(IllegalTransition):
            await transition(db.pool, order_id, OrderStatus.SUBMITTED)

    async def test_a_refused_transition_leaves_no_event_behind(self, db):
        order_id = await record_intent(db.pool, uuid4(), _intent())
        await transition(db.pool, order_id, OrderStatus.SUBMITTED)
        await record_fill(db.pool, order_id, _fill(Decimal(10), Decimal(100)))
        before = [e["status"] for e in await _events(db, order_id)]

        with pytest.raises(IllegalTransition):
            await transition(db.pool, order_id, OrderStatus.SUBMITTED)

        assert [e["status"] for e in await _events(db, order_id)] == before
        order = await get(db.pool, order_id)
        assert order.status is OrderStatus.FILLED

    @pytest.mark.parametrize(
        "terminal",
        [OrderStatus.REJECTED, OrderStatus.CANCELLED],
    )
    async def test_a_terminal_order_accepts_nothing_further(self, db, terminal):
        order_id = await record_intent(db.pool, uuid4(), _intent())
        await transition(db.pool, order_id, terminal)
        with pytest.raises(IllegalTransition):
            await transition(db.pool, order_id, OrderStatus.SUBMITTED)

    async def test_a_fill_on_an_order_never_submitted_is_refused(self, db):
        order_id = await record_intent(db.pool, uuid4(), _intent())
        with pytest.raises(IllegalTransition):
            await record_fill(db.pool, order_id, _fill(Decimal(1), Decimal(100)))

    async def test_a_transition_on_an_unknown_order_raises(self, db):
        with pytest.raises(UnknownOrder):
            await transition(db.pool, uuid4(), OrderStatus.SUBMITTED)

    async def test_a_string_status_is_accepted(self, db):
        order_id = await record_intent(db.pool, uuid4(), _intent())
        await transition(db.pool, order_id, "submitted")
        assert (await get(db.pool, order_id)).status is OrderStatus.SUBMITTED


class TestFillAccumulation:
    """The weighted average. Overwriting it is silent on a full fill only."""

    async def _submitted(self, db, **kw):
        order_id = await record_intent(db.pool, uuid4(), _intent(**kw))
        await transition(db.pool, order_id, OrderStatus.SUBMITTED)
        return order_id

    async def test_two_partial_fills_weight_by_quantity(self, db):
        order_id = await self._submitted(db)
        await record_fill(db.pool, order_id, _fill(Decimal(6), Decimal(100)))
        await record_fill(db.pool, order_id, _fill(Decimal(4), Decimal(150)))

        order = await get(db.pool, order_id)
        assert order.filled_quantity == Decimal(10)
        # 120 is the weighted mean. The last price is 150 and the unweighted
        # mean is 125, so neither of the two wrong implementations passes.
        assert order.average_fill_price == Decimal(120)

    async def test_three_fills_weight_over_every_fill_not_the_last_pair(self, db):
        # The two-fill cases below only exercise one accumulation step, where
        # the running average and the first fill's price are the same number.
        # A third fill separates the correct recursion from an implementation
        # that weights only what it can see now: the arithmetic is written out
        # here rather than copied from `record_fill`.
        #
        #   5 @ 100 -> 500
        #   3 @ 200 -> 600     running: 1100 / 8 = 137.5
        #  12 @  50 -> 600     running: 1700 / 20 = 85
        #
        # 85 is not the last price (50), not the unweighted mean of the three
        # prices (116.66...), not the weighted mean of the last two fills
        # (1200 / 15 = 80), and not the running average re-averaged unweighted
        # against each new price ((137.5 + 50) / 2 = 93.75).
        order_id = await self._submitted(db, quantity=Decimal(20))

        await record_fill(db.pool, order_id, _fill(Decimal(5), Decimal(100)))
        await record_fill(db.pool, order_id, _fill(Decimal(3), Decimal(200)))

        midway = await get(db.pool, order_id)
        after_two = (Decimal(5) * Decimal(100) + Decimal(3) * Decimal(200)) / Decimal(8)
        assert after_two == Decimal("137.5")
        assert midway.filled_quantity == Decimal(8)
        assert midway.average_fill_price == after_two

        await record_fill(db.pool, order_id, _fill(Decimal(12), Decimal(50)))

        order = await get(db.pool, order_id)
        after_three = (
            Decimal(5) * Decimal(100) + Decimal(3) * Decimal(200) + Decimal(12) * Decimal(50)
        ) / Decimal(20)
        assert after_three == Decimal(85)
        assert order.filled_quantity == Decimal(20)
        assert order.average_fill_price == after_three
        assert order.status is OrderStatus.FILLED

    async def test_an_uneven_weighting_is_not_the_midpoint(self, db):
        order_id = await self._submitted(db, quantity=Decimal(100))
        await record_fill(db.pool, order_id, _fill(Decimal(90), Decimal(10)))
        await record_fill(db.pool, order_id, _fill(Decimal(10), Decimal(110)))

        order = await get(db.pool, order_id)
        assert order.average_fill_price == Decimal(20)

    async def test_a_non_terminating_weighted_average_keeps_its_precision(self, db):
        order_id = await self._submitted(db, quantity=Decimal(3))
        await record_fill(db.pool, order_id, _fill(Decimal(1), Decimal(100)))
        await record_fill(db.pool, order_id, _fill(Decimal(2), Decimal(101)))

        order = await get(db.pool, order_id)
        expected = Decimal(302) / Decimal(3)
        assert abs(order.average_fill_price - expected) < Decimal("1e-20")

    async def test_a_partial_fill_leaves_the_order_partially_filled(self, db):
        order_id = await self._submitted(db)
        await record_fill(db.pool, order_id, _fill(Decimal(6), Decimal(100)))

        order = await get(db.pool, order_id)
        assert order.status is OrderStatus.PARTIALLY_FILLED
        assert order.remaining_quantity == Decimal(4)
        assert order.is_open

    async def test_reaching_the_ordered_quantity_fills_the_order(self, db):
        order_id = await self._submitted(db)
        await record_fill(db.pool, order_id, _fill(Decimal(6), Decimal(100)))
        await record_fill(db.pool, order_id, _fill(Decimal(4), Decimal(150)))

        order = await get(db.pool, order_id)
        assert order.status is OrderStatus.FILLED
        assert not order.is_open

    async def test_a_shortfall_of_a_hair_is_still_a_partial_fill(self, db):
        order_id = await self._submitted(db)
        await record_fill(db.pool, order_id, _fill(Decimal("9.999999999"), Decimal(100)))
        assert (await get(db.pool, order_id)).status is OrderStatus.PARTIALLY_FILLED

    async def test_a_single_fill_larger_than_the_order_is_refused_without_mutation(self, db):
        order_id = await self._submitted(db)

        with pytest.raises(OrderLedgerError, match="overfill"):
            await record_fill(
                db.pool,
                order_id,
                _fill(Decimal(11), Decimal(125), fee=Decimal("1.25"), external_id="X-11"),
            )

        order = await get(db.pool, order_id)
        assert order.status is OrderStatus.SUBMITTED
        assert order.filled_quantity == Decimal(0)
        assert order.average_fill_price is None
        assert order.fee_paid == Decimal(0)
        assert order.external_id is None
        assert [e["status"] for e in await _events(db, order_id)] == ["intent", "submitted"]

    async def test_cumulative_overfill_preserves_the_valid_partial_fill(self, db):
        order_id = await self._submitted(db)
        await record_fill(
            db.pool,
            order_id,
            _fill(Decimal(6), Decimal(100), fee=Decimal("0.60"), external_id="X-6"),
        )

        with pytest.raises(OrderLedgerError, match="overfill"):
            await record_fill(
                db.pool,
                order_id,
                _fill(Decimal(5), Decimal(200), fee=Decimal("1.00"), external_id="X-5"),
            )

        order = await get(db.pool, order_id)
        assert order.status is OrderStatus.PARTIALLY_FILLED
        assert order.filled_quantity == Decimal(6)
        assert order.remaining_quantity == Decimal(4)
        assert order.average_fill_price == Decimal(100)
        assert order.fee_paid == Decimal("0.60")
        assert order.external_id == "X-6"
        assert [e["status"] for e in await _events(db, order_id)] == [
            "intent",
            "submitted",
            "partially_filled",
        ]

    async def test_fees_accumulate_across_fills(self, db):
        order_id = await self._submitted(db)
        await record_fill(
            db.pool, order_id, _fill(Decimal(6), Decimal(100), fee=Decimal("0.60"))
        )
        await record_fill(
            db.pool, order_id, _fill(Decimal(4), Decimal(150), fee=Decimal("0.45"))
        )
        assert (await get(db.pool, order_id)).fee_paid == Decimal("1.05")

    async def test_each_fill_writes_its_own_event(self, db):
        order_id = await self._submitted(db)
        await record_fill(
            db.pool, order_id, _fill(Decimal(6), Decimal(100), external_id="X-9")
        )
        await record_fill(db.pool, order_id, _fill(Decimal(4), Decimal(150)))

        statuses = [e["status"] for e in await _events(db, order_id)]
        assert statuses == ["intent", "submitted", "partially_filled", "filled"]

    async def test_the_fill_event_captures_the_venue_response(self, db):
        order_id = await self._submitted(db)
        await record_fill(
            db.pool,
            order_id,
            _fill(Decimal(10), Decimal("100.25"), raw={"orderId": "abc"}),
        )
        payload = json.loads((await _events(db, order_id))[-1]["payload"])
        assert payload["fill"]["average_price"] == "100.25"
        assert payload["raw"] == {"orderId": "abc"}

    async def test_a_fill_after_completion_is_refused(self, db):
        order_id = await self._submitted(db)
        await record_fill(db.pool, order_id, _fill(Decimal(10), Decimal(100)))
        with pytest.raises(IllegalTransition):
            await record_fill(db.pool, order_id, _fill(Decimal(1), Decimal(200)))

    async def test_a_refused_fill_does_not_move_the_average(self, db):
        order_id = await self._submitted(db)
        await record_fill(db.pool, order_id, _fill(Decimal(10), Decimal(100)))
        with pytest.raises(IllegalTransition):
            await record_fill(db.pool, order_id, _fill(Decimal(1), Decimal(200)))
        assert (await get(db.pool, order_id)).average_fill_price == Decimal(100)

    async def test_a_fill_on_a_cancelled_order_is_refused(self, db):
        order_id = await self._submitted(db)
        await transition(db.pool, order_id, OrderStatus.CANCELLED)
        with pytest.raises(IllegalTransition):
            await record_fill(db.pool, order_id, _fill(Decimal(1), Decimal(100)))

    async def test_a_fill_on_an_unknown_order_raises(self, db):
        with pytest.raises(UnknownOrder):
            await record_fill(db.pool, uuid4(), _fill(Decimal(1), Decimal(100)))


class TestRefusals:
    """The failure paths. Each one is a way fabricated state could enter."""

    async def test_an_empty_fill_is_refused(self, db):
        order_id = await record_intent(db.pool, uuid4(), _intent())
        await transition(db.pool, order_id, OrderStatus.SUBMITTED)
        empty = Fill(
            intent_id="i-1",
            venue="paper",
            symbol="BTC/USD",
            side=Side.BUY,
            filled_quantity=Decimal(0),
            average_price=Decimal(100),
            fee_paid=Decimal(0),
            filled_at=NOW,
        )
        with pytest.raises(ValueError, match="empty fill"):
            await record_fill(db.pool, order_id, empty)

        order = await get(db.pool, order_id)
        assert order.status is OrderStatus.SUBMITTED
        assert order.average_fill_price is None

    async def test_an_infinite_fill_quantity_is_refused(self, db):
        """Infinity clears every guard in `Fill`: not < 0, and > 0."""
        order_id = await record_intent(db.pool, uuid4(), _intent())
        await transition(db.pool, order_id, OrderStatus.SUBMITTED)
        with pytest.raises(ValueError, match="finite"):
            await record_fill(
                db.pool, order_id, _fill(Decimal("Infinity"), Decimal(100))
            )
        order = await get(db.pool, order_id)
        assert order.filled_quantity == Decimal(0)
        assert order.average_fill_price is None

    async def test_an_infinite_fill_price_is_refused(self, db):
        order_id = await record_intent(db.pool, uuid4(), _intent())
        await transition(db.pool, order_id, OrderStatus.SUBMITTED)
        with pytest.raises(ValueError, match="finite"):
            await record_fill(
                db.pool, order_id, _fill(Decimal(1), Decimal("Infinity"))
            )

    async def test_an_infinite_fee_is_refused(self, db):
        order_id = await record_intent(db.pool, uuid4(), _intent())
        await transition(db.pool, order_id, OrderStatus.SUBMITTED)
        with pytest.raises(ValueError, match="finite"):
            await record_fill(
                db.pool,
                order_id,
                _fill(Decimal(1), Decimal(100), fee=Decimal("Infinity")),
            )

    def test_a_nan_fill_cannot_reach_the_ledger_at_all(self):
        """The guard `record_fill` relies on: NaN dies at Fill construction.

        A Decimal NaN raises `InvalidOperation` on any ordered comparison, so
        `Fill.__post_init__` cannot silently pass one through the way a float
        NaN would. The ledger's own refusal therefore only has to cover the
        infinities.
        """
        with pytest.raises(InvalidOperation):
            _fill(Decimal("NaN"), Decimal(100))

    async def test_getting_an_absent_order_returns_none(self, db):
        assert await get(db.pool, uuid4()) is None


class TestOpenOrders:
    async def test_only_live_orders_are_open_and_only_for_this_portfolio(self, db):
        mine, theirs = uuid4(), uuid4()

        live = await record_intent(db.pool, mine, _intent(idempotency_key="live"))
        await transition(db.pool, live, OrderStatus.SUBMITTED)

        part = await record_intent(db.pool, mine, _intent(idempotency_key="part"))
        await transition(db.pool, part, OrderStatus.SUBMITTED)
        await record_fill(db.pool, part, _fill(Decimal(3), Decimal(100)))

        done = await record_intent(db.pool, mine, _intent(idempotency_key="done"))
        await transition(db.pool, done, OrderStatus.SUBMITTED)
        await record_fill(db.pool, done, _fill(Decimal(10), Decimal(100)))

        killed = await record_intent(db.pool, mine, _intent(idempotency_key="killed"))
        await transition(db.pool, killed, OrderStatus.CANCELLED)

        other = await record_intent(db.pool, theirs, _intent(idempotency_key="other"))
        await transition(db.pool, other, OrderStatus.SUBMITTED)

        ids = [o.id for o in await open_orders(db.pool, mine)]
        assert ids == [live, part]

    async def test_open_orders_come_back_as_orders_with_usable_state(self, db):
        portfolio = uuid4()
        order_id = await record_intent(db.pool, portfolio, _intent())
        await transition(db.pool, order_id, OrderStatus.SUBMITTED)
        await record_fill(db.pool, order_id, _fill(Decimal("2.5"), Decimal(100)))

        (order,) = await open_orders(db.pool, portfolio)
        assert isinstance(order, Order)
        assert order.remaining_quantity == Decimal("7.5")
        assert order.status is OrderStatus.PARTIALLY_FILLED

    async def test_a_portfolio_with_nothing_live_has_no_open_orders(self, db):
        assert await open_orders(db.pool, uuid4()) == []
