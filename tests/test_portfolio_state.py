import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest

from omni.portfolio import orders, state
from omni.portfolio.state import (
    UnaccountedClose,
    UnknownPortfolio,
    UnmarkedPosition,
    apply_fill,
    load,
    realised_pnl,
    rebuild_from_fills,
    snapshot_nav,
)
from omni.venue.protocol import Fill, MarketType, Side, TradeIntent

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

# Marks are keyed by (venue, symbol), because positions are.
BTC = ("binance", "BTC/USD")
KRAKEN_BTC = ("kraken", "BTC/USD")

# The positions read, made slow enough to hold the read path open while another
# connection commits a fill into the window between it and the cash read.
# pg_sleep sits in the target list so it is evaluated once per row returned,
# rather than in a qual the planner is free to skip.
_SLOW_POSITIONS = """
SELECT venue, symbol, market_type, quantity, average_entry, updated_at,
       pg_sleep(0.4)
FROM position
WHERE portfolio_id = $1
ORDER BY venue COLLATE "C", symbol COLLATE "C", market_type COLLATE "C"
"""

_ORIGINAL_POSITIONS = state._POSITIONS


def _fill(
    *,
    venue: str = "binance",
    symbol: str = "BTC/USD",
    side: Side = Side.BUY,
    quantity: str = "2",
    price: str = "100",
    fee: str = "0",
    at: datetime | None = None,
) -> Fill:
    return Fill(
        intent_id=uuid4().hex,
        venue=venue,
        symbol=symbol,
        side=side,
        filled_quantity=Decimal(quantity),
        average_price=Decimal(price),
        fee_paid=Decimal(fee),
        filled_at=NOW if at is None else at,
    )


@pytest.fixture
async def portfolio_id(db):
    pid = await db.pool.fetchval(
        "INSERT INTO portfolio (name, base_currency) VALUES ($1, $2) RETURNING id",
        "test book",
        "USD",
    )
    yield pid
    await db.pool.execute("DELETE FROM portfolio WHERE id = $1", pid)


async def _row(db, pid, venue="binance", symbol="BTC/USD", market_type="spot"):
    return await db.pool.fetchrow(
        "SELECT quantity, average_entry, updated_at FROM position "
        "WHERE portfolio_id = $1 AND venue = $2 AND symbol = $3 AND market_type = $4",
        pid,
        venue,
        symbol,
        market_type,
    )


class TestApplyFill:
    async def test_unknown_portfolio_raises_and_writes_nothing(self, db):
        missing = uuid4()
        with pytest.raises(UnknownPortfolio):
            await apply_fill(db.pool, missing, _fill(), MarketType.SPOT)

        assert await db.pool.fetchval(
            "SELECT count(*) FROM cash_balance WHERE portfolio_id = $1", missing
        ) == 0

    async def test_opening_fill_takes_the_fill_price_and_debits_cash(self, db, portfolio_id):
        result = await apply_fill(
            db.pool,
            portfolio_id,
            _fill(quantity="2", price="100", fee="1.5"),
            MarketType.SPOT,
        )

        held = result.position_for("binance", "BTC/USD", MarketType.SPOT)
        assert held.quantity == Decimal(2)
        assert held.average_entry == Decimal(100)
        assert held.as_of == NOW
        assert result.cash == Decimal("-201.5")
        assert result.nav == Decimal("-1.5")

    async def test_adding_averages_the_entry_weighted_by_quantity(self, db, portfolio_id):
        await apply_fill(db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT)
        result = await apply_fill(
            db.pool,
            portfolio_id,
            _fill(quantity="1", price="130", at=NOW + timedelta(minutes=1)),
            MarketType.SPOT,
        )

        held = result.position_for("binance", "BTC/USD", MarketType.SPOT)
        assert held.quantity == Decimal(3)
        # (2*100 + 1*130) / 3 -- not the last price, not the first, not the
        # unweighted mean of 115.
        assert held.average_entry == Decimal(110)
        assert held.as_of == NOW + timedelta(minutes=1)

    async def test_reducing_keeps_the_original_entry(self, db, portfolio_id):
        await apply_fill(db.pool, portfolio_id, _fill(quantity="4", price="100"), MarketType.SPOT)
        result = await apply_fill(
            db.pool,
            portfolio_id,
            _fill(
                quantity="1",
                price="250",
                side=Side.SELL,
                at=NOW + timedelta(minutes=1),
            ),
            MarketType.SPOT,
        )

        held = result.position_for("binance", "BTC/USD", MarketType.SPOT)
        assert held.quantity == Decimal(3)
        assert held.average_entry == Decimal(100)
        assert result.cash == Decimal(-150)

    async def test_flip_takes_the_new_fill_price(self, db, portfolio_id):
        await apply_fill(db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT)
        result = await apply_fill(
            db.pool,
            portfolio_id,
            _fill(
                quantity="5",
                price="120",
                side=Side.SELL,
                at=NOW + timedelta(minutes=1),
            ),
            MarketType.SPOT,
        )

        held = result.position_for("binance", "BTC/USD", MarketType.SPOT)
        assert held.quantity == Decimal(-3)
        # Any blend of 100 and 120 would be an entry this short never had.
        assert held.average_entry == Decimal(120)
        assert held.is_short

    async def test_closing_deletes_the_row(self, db, portfolio_id):
        await apply_fill(db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT)
        result = await apply_fill(
            db.pool,
            portfolio_id,
            _fill(
                quantity="2",
                price="110",
                side=Side.SELL,
                at=NOW + timedelta(minutes=1),
            ),
            MarketType.SPOT,
        )

        assert result.positions == ()
        assert result.position_for("binance", "BTC/USD", MarketType.SPOT) is None
        assert await _row(db, portfolio_id) is None
        assert result.cash == Decimal(20)
        assert result.nav == Decimal(20)

    async def test_spot_and_perpetual_of_one_symbol_stay_separate(self, db, portfolio_id):
        await apply_fill(db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT)
        result = await apply_fill(
            db.pool,
            portfolio_id,
            _fill(quantity="2", price="101", side=Side.SELL),
            MarketType.PERPETUAL,
        )

        spot = result.position_for("binance", "BTC/USD", MarketType.SPOT)
        perp = result.position_for("binance", "BTC/USD", MarketType.PERPETUAL)
        assert spot.quantity == Decimal(2)
        assert perp.quantity == Decimal(-2)
        assert result.gross_exposure == Decimal(402)
        assert result.net_exposure == Decimal(-2)

    async def test_cash_and_position_commit_together(self, db, portfolio_id, monkeypatch):
        monkeypatch.setattr(
            state,
            "_UPSERT_CASH",
            "INSERT INTO cash_balance (portfolio_id) VALUES ($1, $2, $3, $4, $5)",
        )

        with pytest.raises(asyncpg.PostgresError):
            await apply_fill(db.pool, portfolio_id, _fill(), MarketType.SPOT)

        assert await _row(db, portfolio_id) is None

    async def test_concurrent_fills_do_not_lose_an_update(self, db, portfolio_id):
        await asyncio.gather(
            apply_fill(db.pool, portfolio_id, _fill(quantity="1", price="100"), MarketType.SPOT),
            apply_fill(db.pool, portfolio_id, _fill(quantity="1", price="100"), MarketType.SPOT),
        )

        row = await _row(db, portfolio_id)
        assert row["quantity"] == Decimal(2)

    async def test_naive_filled_at_is_refused(self, db, portfolio_id):
        naive = _fill(at=NOW.replace(tzinfo=None))
        with pytest.raises(ValueError, match="naive"):
            await apply_fill(db.pool, portfolio_id, naive, MarketType.SPOT)


class TestLoad:
    async def test_cash_ignores_other_currencies(self, db, portfolio_id):
        await apply_fill(db.pool, portfolio_id, _fill(quantity="1", price="100"), MarketType.SPOT)
        await db.pool.execute(
            "INSERT INTO cash_balance (portfolio_id, venue, asset, free) "
            "VALUES ($1, 'binance', 'EUR', 500)",
            portfolio_id,
        )

        result = await load(db.pool, portfolio_id)
        assert result.cash == Decimal(-100)

    async def test_locked_cash_is_still_the_portfolio_s_cash(self, db, portfolio_id):
        await db.pool.execute(
            "INSERT INTO cash_balance (portfolio_id, venue, asset, free, locked) "
            "VALUES ($1, 'binance', 'USD', 100, 25)",
            portfolio_id,
        )

        result = await load(db.pool, portfolio_id)
        assert result.cash == Decimal(125)

    async def test_exposures_and_nav_are_cost_basis(self, db, portfolio_id):
        await apply_fill(db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT)
        await apply_fill(
            db.pool,
            portfolio_id,
            _fill(symbol="ETH/USD", quantity="3", price="50", side=Side.SELL),
            MarketType.PERPETUAL,
        )

        result = await load(db.pool, portfolio_id)
        assert result.gross_exposure == Decimal(350)
        assert result.net_exposure == Decimal(50)
        assert result.cash == Decimal(-50)
        assert result.nav == Decimal(0)

    async def test_position_for_discriminates_market_type_and_venue(self, db, portfolio_id):
        await apply_fill(db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT)

        result = await load(db.pool, portfolio_id)
        assert result.position_for("binance", "BTC/USD", MarketType.PERPETUAL) is None
        assert result.position_for("kraken", "BTC/USD", MarketType.SPOT) is None
        assert result.position_for("binance", "ETH/USD", MarketType.SPOT) is None
        assert result.position_for("binance", "BTC/USD", MarketType.SPOT).quantity == Decimal(2)

    async def test_cash_positions_carry_every_row_signed(self, db, portfolio_id):
        """What the reconciler compares against a venue, read in one snapshot.

        `cash` nets to a single base-currency number; a venue reports per asset,
        so the rows have to survive the read intact -- including the negative
        `free` a margin buy leaves, which `venue.protocol.Balance` refuses.
        """
        await apply_fill(
            db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT
        )
        await db.pool.execute(
            "INSERT INTO cash_balance (portfolio_id, venue, asset, free, locked) "
            "VALUES ($1, 'kraken', 'EUR', 300, 40)",
            portfolio_id,
        )

        result = await load(db.pool, portfolio_id)

        by_key = {(c.venue, c.asset): c for c in result.cash_positions}
        assert set(by_key) == {("binance", "USD"), ("kraken", "EUR")}
        assert by_key[("binance", "USD")].free == Decimal(-200)
        assert by_key[("binance", "USD")].total == Decimal(-200)
        assert by_key[("kraken", "EUR")].total == Decimal(340)
        assert result.cash == Decimal(-200), "EUR is not converted into USD"

    async def test_unknown_portfolio_raises(self, db):
        with pytest.raises(UnknownPortfolio):
            await load(db.pool, uuid4())


class TestSnapshotNav:
    async def test_unmarked_position_raises_and_writes_no_snapshot(self, db, portfolio_id):
        await apply_fill(db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT)
        await apply_fill(
            db.pool,
            portfolio_id,
            _fill(symbol="ETH/USD", quantity="1", price="2000"),
            MarketType.SPOT,
        )

        with pytest.raises(UnmarkedPosition, match="ETH/USD"):
            await snapshot_nav(db.pool, portfolio_id, {BTC: Decimal(150)})

        assert await db.pool.fetchval(
            "SELECT count(*) FROM nav_snapshot WHERE portfolio_id = $1", portfolio_id
        ) == 0

    async def test_nav_marks_the_book_and_records_it(self, db, portfolio_id):
        await apply_fill(db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT)

        nav = await snapshot_nav(db.pool, portfolio_id, {BTC: Decimal(150)})

        # Cost basis would be 0 here (cash -200, book 200). The mark is what
        # makes this 100.
        assert nav == Decimal(100)
        row = await db.pool.fetchrow(
            "SELECT nav, cash, gross_exposure, net_exposure FROM nav_snapshot "
            "WHERE portfolio_id = $1",
            portfolio_id,
        )
        assert row["nav"] == Decimal(100)
        assert row["cash"] == Decimal(-200)
        assert row["gross_exposure"] == Decimal(300)
        assert row["net_exposure"] == Decimal(300)

    async def test_short_is_a_liability_at_the_mark(self, db, portfolio_id):
        await apply_fill(
            db.pool,
            portfolio_id,
            _fill(quantity="2", price="100", side=Side.SELL),
            MarketType.PERPETUAL,
        )

        nav = await snapshot_nav(db.pool, portfolio_id, {BTC: Decimal(90)})

        assert nav == Decimal(20)
        row = await db.pool.fetchrow(
            "SELECT gross_exposure, net_exposure FROM nav_snapshot WHERE portfolio_id = $1",
            portfolio_id,
        )
        assert row["gross_exposure"] == Decimal(180)
        assert row["net_exposure"] == Decimal(-180)

    async def test_marks_that_are_not_prices_are_refused(self, db, portfolio_id):
        await apply_fill(db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT)

        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal(0), Decimal(-5)):
            with pytest.raises(UnmarkedPosition):
                await snapshot_nav(db.pool, portfolio_id, {BTC: bad})

        assert await db.pool.fetchval(
            "SELECT count(*) FROM nav_snapshot WHERE portfolio_id = $1", portfolio_id
        ) == 0

    async def test_flat_portfolio_marks_to_its_cash(self, db, portfolio_id):
        await db.pool.execute(
            "INSERT INTO cash_balance (portfolio_id, venue, asset, free) "
            "VALUES ($1, 'binance', 'USD', 7500)",
            portfolio_id,
        )

        assert await snapshot_nav(db.pool, portfolio_id, {}) == Decimal(7500)


class TestMarksAreKeyedByVenue:
    """Two venues, one symbol: the difference between the prices IS the position."""

    async def _basis(self, db, portfolio_id) -> None:
        """Long 2 BTC spot at binance, short 2 BTC perp at kraken, both at 100.

        Cash nets to zero and the cost-basis book nets to zero, so every number
        in the resulting NAV comes from the marks.
        """
        await apply_fill(
            db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT
        )
        await apply_fill(
            db.pool,
            portfolio_id,
            _fill(venue="kraken", quantity="2", price="100", side=Side.SELL),
            MarketType.PERPETUAL,
        )

    async def test_each_venue_is_marked_at_its_own_price(self, db, portfolio_id):
        await self._basis(db, portfolio_id)

        nav = await snapshot_nav(
            db.pool,
            portfolio_id,
            {BTC: Decimal(101), KRAKEN_BTC: Decimal(100)},
        )

        # The basis widened by 1 across 2 units, so the pair is worth 2. A
        # symbol-keyed marks dict prices both legs at whatever single number it
        # holds for "BTC/USD" and reports exactly 0 -- the P&L of the only
        # position whose P&L is the spread.
        assert nav == Decimal(2)

        row = await db.pool.fetchrow(
            "SELECT nav, gross_exposure, net_exposure FROM nav_snapshot "
            "WHERE portfolio_id = $1",
            portfolio_id,
        )
        assert row["nav"] == Decimal(2)
        assert row["gross_exposure"] == Decimal(402)
        assert row["net_exposure"] == Decimal(2)

    async def test_a_flat_basis_is_flat_and_the_test_can_tell(self, db, portfolio_id):
        await self._basis(db, portfolio_id)

        nav = await snapshot_nav(
            db.pool,
            portfolio_id,
            {BTC: Decimal(100), KRAKEN_BTC: Decimal(100)},
        )

        assert nav == Decimal(0)

    async def test_another_venue_s_price_is_not_a_substitute(self, db, portfolio_id):
        """The bug wearing a different hat: falling back is as wrong as guessing."""
        await self._basis(db, portfolio_id)

        with pytest.raises(UnmarkedPosition, match="kraken"):
            await snapshot_nav(db.pool, portfolio_id, {BTC: Decimal(101)})

        assert await db.pool.fetchval(
            "SELECT count(*) FROM nav_snapshot WHERE portfolio_id = $1", portfolio_id
        ) == 0

    async def test_the_same_symbol_at_the_wrong_venue_is_not_a_mark(
        self, db, portfolio_id
    ):
        await self._basis(db, portfolio_id)

        with pytest.raises(UnmarkedPosition, match="binance"):
            await snapshot_nav(
                db.pool,
                portfolio_id,
                {KRAKEN_BTC: Decimal(100), ("coinbase", "BTC/USD"): Decimal(101)},
            )


class TestReadIsolation:
    """A read that spans two statements must not see half of a concurrent fill.

    The interleaving is forced rather than hoped for: the positions read is
    slowed, and a second connection commits a full round trip into the gap
    between it and the cash read. Under READ COMMITTED each statement takes its
    own snapshot, so the reader sees the position from before the close and the
    cash from after it -- a NAV of 200 for a book that was worth 0 both before
    and after. `snapshot_nav` would then persist that as an authoritative row.
    """

    async def _close_after(self, db, portfolio_id, delay: float) -> None:
        await asyncio.sleep(delay)
        await apply_fill(
            db.pool,
            portfolio_id,
            _fill(
                quantity="2",
                price="100",
                side=Side.SELL,
                at=NOW + timedelta(minutes=1),
            ),
            MarketType.SPOT,
        )

    async def test_snapshot_nav_never_persists_half_a_fill(
        self, db, portfolio_id, monkeypatch
    ):
        await apply_fill(
            db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT
        )
        monkeypatch.setattr(state, "_POSITIONS", _SLOW_POSITIONS)

        nav, _ = await asyncio.gather(
            snapshot_nav(db.pool, portfolio_id, {BTC: Decimal(100)}),
            self._close_after(db, portfolio_id, 0.1),
        )

        # Wholly before the fill: position 2 at 100, cash -200. Wholly after
        # would be 0 and 0. Both are worth 0; the torn read is worth 200.
        assert nav == Decimal(0)

        row = await db.pool.fetchrow(
            "SELECT nav, cash, gross_exposure, net_exposure FROM nav_snapshot "
            "WHERE portfolio_id = $1",
            portfolio_id,
        )
        assert row["nav"] == row["cash"] + row["net_exposure"], (
            "the persisted row must describe one moment"
        )
        assert row["cash"] == Decimal(-200)
        assert row["net_exposure"] == Decimal(200)
        assert row["gross_exposure"] == Decimal(200)

    async def test_load_reads_positions_and_cash_from_one_moment(
        self, db, portfolio_id, monkeypatch
    ):
        await apply_fill(
            db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT
        )
        monkeypatch.setattr(state, "_POSITIONS", _SLOW_POSITIONS)

        result, _ = await asyncio.gather(
            load(db.pool, portfolio_id),
            self._close_after(db, portfolio_id, 0.1),
        )

        assert result.cash == Decimal(-200)
        assert len(result.positions) == 1
        assert result.nav == Decimal(0)
        assert result.nav == result.cash + result.net_exposure

    async def test_the_fill_did_land_so_the_read_was_a_real_race(
        self, db, portfolio_id, monkeypatch
    ):
        """Without this the isolation tests would pass against a fill that never
        committed, which is not a race at all."""
        await apply_fill(
            db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT
        )
        monkeypatch.setattr(state, "_POSITIONS", _SLOW_POSITIONS)

        await asyncio.gather(
            load(db.pool, portfolio_id),
            self._close_after(db, portfolio_id, 0.1),
        )

        monkeypatch.setattr(state, "_POSITIONS", _ORIGINAL_POSITIONS)
        after = await load(db.pool, portfolio_id)
        assert after.positions == ()
        assert after.cash == Decimal(0)


def _sequence() -> list[tuple[Fill, MarketType]]:
    """Two venues, three market keys, an average, a reduce, a flip and a close."""
    return [
        (_fill(quantity="2", price="100", fee="0.2", at=NOW), MarketType.SPOT),
        (
            _fill(quantity="1", price="130", fee="0.1", at=NOW + timedelta(minutes=1)),
            MarketType.SPOT,
        ),
        (
            _fill(
                symbol="ETH/USD",
                quantity="3",
                price="2000",
                fee="3",
                side=Side.SELL,
                at=NOW + timedelta(minutes=2),
            ),
            MarketType.PERPETUAL,
        ),
        (
            _fill(
                quantity="5",
                price="120",
                fee="0.6",
                side=Side.SELL,
                at=NOW + timedelta(minutes=3),
            ),
            MarketType.SPOT,
        ),
        (
            _fill(
                venue="kraken",
                quantity="1",
                price="100",
                fee="0.05",
                at=NOW + timedelta(minutes=4),
            ),
            MarketType.SPOT,
        ),
        (
            _fill(
                venue="kraken",
                quantity="2",
                price="110",
                fee="0.11",
                at=NOW + timedelta(minutes=5),
            ),
            MarketType.SPOT,
        ),
        (
            _fill(
                symbol="ETH/USD",
                quantity="1",
                price="1900",
                fee="1",
                at=NOW + timedelta(minutes=6),
            ),
            MarketType.PERPETUAL,
        ),
        (
            _fill(
                symbol="ETH/USD",
                quantity="2",
                price="1800",
                fee="2",
                at=NOW + timedelta(minutes=7),
            ),
            MarketType.PERPETUAL,
        ),
    ]


class TestRebuild:
    async def test_replay_matches_incremental_application(self, db, portfolio_id):
        fills = _sequence()
        for fill, market_type in fills:
            await apply_fill(db.pool, portfolio_id, fill, market_type)

        materialised = await load(db.pool, portfolio_id)
        replayed = await rebuild_from_fills(db.pool, portfolio_id, fills)

        assert replayed.positions == materialised.positions
        assert replayed.cash == materialised.cash
        assert replayed.nav == materialised.nav

        # And the replay is not vacuously equal: it has to have got the flip,
        # the close and a non-terminating average right.
        flipped = materialised.position_for("binance", "BTC/USD", MarketType.SPOT)
        assert flipped.quantity == Decimal(-2)
        assert flipped.average_entry == Decimal(120)

        assert materialised.position_for("binance", "ETH/USD", MarketType.PERPETUAL) is None

        averaged = materialised.position_for("kraken", "BTC/USD", MarketType.SPOT)
        assert averaged.quantity == Decimal(3)
        assert averaged.average_entry == Decimal(320) / Decimal(3)
        assert len(materialised.positions) == 2

    async def test_rebuild_writes_nothing(self, db, portfolio_id):
        replayed = await rebuild_from_fills(db.pool, portfolio_id, _sequence())

        assert len(replayed.positions) == 2
        assert await db.pool.fetchval(
            "SELECT count(*) FROM position WHERE portfolio_id = $1", portfolio_id
        ) == 0
        assert await db.pool.fetchval(
            "SELECT count(*) FROM cash_balance WHERE portfolio_id = $1", portfolio_id
        ) == 0

    async def test_rebuild_of_an_unknown_portfolio_raises(self, db):
        with pytest.raises(UnknownPortfolio):
            await rebuild_from_fills(db.pool, uuid4(), _sequence())

    async def test_rebuild_refuses_a_naive_timestamp(self, db, portfolio_id):
        fills = [(_fill(at=NOW.replace(tzinfo=None)), MarketType.SPOT)]
        with pytest.raises(ValueError, match="naive"):
            await rebuild_from_fills(db.pool, portfolio_id, fills)


DAY = datetime(2026, 8, 7, tzinfo=UTC)
NEXT_DAY = DAY + timedelta(days=1)


async def _ledger_fill(
    db,
    portfolio_id,
    *,
    side: Side,
    quantity: str,
    price: str,
    fee: str = "0",
    at: datetime,
    venue: str = "binance",
    symbol: str = "BTC/USD",
    market_type: MarketType = MarketType.SPOT,
    apply: bool = True,
) -> Fill:
    """Put one fill through the ledger and, unless told not to, into the book.

    `apply=False` is how a divergence is staged: a ledger the position rows do
    not agree with.
    """
    intent = TradeIntent(
        venue=venue,
        symbol=symbol,
        side=side,
        market_type=market_type,
        quantity=Decimal(quantity),
        reference_price=Decimal(price),
    )
    order_id = await orders.record_intent(db.pool, portfolio_id, intent)
    await orders.transition(db.pool, order_id, orders.OrderStatus.SUBMITTED)

    fill = Fill(
        intent_id=intent.idempotency_key,
        venue=venue,
        symbol=symbol,
        side=side,
        filled_quantity=Decimal(quantity),
        average_price=Decimal(price),
        fee_paid=Decimal(fee),
        filled_at=at,
    )
    await orders.record_fill(db.pool, order_id, fill)
    if apply:
        await apply_fill(db.pool, portfolio_id, fill, market_type)
    return fill


class TestRealisedPnl:
    async def test_a_closed_round_trip_realises_net_of_the_closing_fee(
        self, db, portfolio_id
    ):
        await _ledger_fill(
            db, portfolio_id, side=Side.BUY, quantity="2", price="100", fee="1",
            at=DAY + timedelta(hours=1),
        )
        await _ledger_fill(
            db, portfolio_id, side=Side.SELL, quantity="2", price="110", fee="2",
            at=DAY + timedelta(hours=2),
        )

        result = await realised_pnl(db.pool, portfolio_id, since=DAY, until=NEXT_DAY)

        # 2 * (110 - 100) = 20, less the 2 paid to close. The 1 paid to open was
        # a cash cost at the time it was paid and is not attributed to the close.
        assert result == Decimal(18)

    async def test_an_open_position_realises_nothing(self, db, portfolio_id):
        await _ledger_fill(
            db, portfolio_id, side=Side.BUY, quantity="2", price="100", fee="1",
            at=DAY + timedelta(hours=1),
        )
        await _ledger_fill(
            db, portfolio_id, side=Side.BUY, quantity="3", price="500",
            at=DAY + timedelta(hours=2),
        )

        result = await realised_pnl(db.pool, portfolio_id, since=DAY, until=NEXT_DAY)

        # The position is five times the size and five times the price it
        # started at. None of that is realised, and the fee paid to open is not
        # a realised loss either.
        assert result == Decimal(0)

    async def test_a_partial_close_realises_only_the_part_closed(
        self, db, portfolio_id
    ):
        await _ledger_fill(
            db, portfolio_id, side=Side.BUY, quantity="4", price="100",
            at=DAY + timedelta(hours=1),
        )
        await _ledger_fill(
            db, portfolio_id, side=Side.SELL, quantity="1", price="250",
            at=DAY + timedelta(hours=2),
        )

        result = await realised_pnl(db.pool, portfolio_id, since=DAY, until=NEXT_DAY)

        # 1 * (250 - 100). Not 4 * 150, which is the whole position marked at
        # the exit, and not 0.
        assert result == Decimal(150)

    async def test_a_short_realises_when_it_is_bought_back(self, db, portfolio_id):
        await _ledger_fill(
            db, portfolio_id, side=Side.SELL, quantity="2", price="100",
            at=DAY + timedelta(hours=1), market_type=MarketType.PERPETUAL,
        )
        await _ledger_fill(
            db, portfolio_id, side=Side.BUY, quantity="2", price="90", fee="1",
            at=DAY + timedelta(hours=2), market_type=MarketType.PERPETUAL,
        )

        result = await realised_pnl(db.pool, portfolio_id, since=DAY, until=NEXT_DAY)

        # A short that falls 10 makes 10, so the sign of the whole calculation
        # is pinned here: 2 * (100 - 90) - 1, not -21.
        assert result == Decimal(19)

    async def test_a_flip_realises_the_close_and_prorates_that_fill_s_fee(
        self, db, portfolio_id
    ):
        await _ledger_fill(
            db, portfolio_id, side=Side.BUY, quantity="2", price="100",
            at=DAY + timedelta(hours=1),
        )
        await _ledger_fill(
            db, portfolio_id, side=Side.SELL, quantity="5", price="120", fee="5",
            at=DAY + timedelta(hours=2),
        )

        result = await realised_pnl(db.pool, portfolio_id, since=DAY, until=NEXT_DAY)

        # 2 of the 5 sold closed the long: 2 * 20 = 40. Two fifths of the fee
        # belongs to that close; the other three fifths is a cost of the short
        # now open, which has realised nothing.
        assert result == Decimal(38)

        book = await load(db.pool, portfolio_id)
        assert book.position_for("binance", "BTC/USD", MarketType.SPOT).quantity == (
            Decimal(-3)
        )

    async def test_the_window_selects_by_the_close_not_by_the_open(
        self, db, portfolio_id
    ):
        await _ledger_fill(
            db, portfolio_id, side=Side.BUY, quantity="2", price="100",
            at=DAY + timedelta(hours=1),
        )
        await _ledger_fill(
            db, portfolio_id, side=Side.SELL, quantity="2", price="130",
            at=NEXT_DAY + timedelta(hours=1),
        )

        opened = await realised_pnl(db.pool, portfolio_id, since=DAY, until=NEXT_DAY)
        assert opened == Decimal(0), "the day it opened realised nothing"

        closed = await realised_pnl(
            db.pool, portfolio_id, since=NEXT_DAY, until=NEXT_DAY + timedelta(days=1)
        )
        # The entry came from a fill before the window. A derivation that only
        # replayed fills inside the window would have no entry to value against.
        assert closed == Decimal(60)

    async def test_a_close_the_ledger_cannot_account_for_raises(
        self, db, portfolio_id
    ):
        """An opening fill missing from the ledger, which is the case that turns
        a 20 into a 0 -- and a kill switch that fires into one that does not."""
        opening = _fill(quantity="2", price="100", at=DAY + timedelta(hours=1))
        await apply_fill(db.pool, portfolio_id, opening, MarketType.SPOT)

        await _ledger_fill(
            db, portfolio_id, side=Side.SELL, quantity="2", price="110",
            at=DAY + timedelta(hours=2),
        )

        with pytest.raises(UnaccountedClose):
            await realised_pnl(db.pool, portfolio_id, since=DAY, until=NEXT_DAY)

    async def test_a_fill_recorded_but_never_applied_raises(self, db, portfolio_id):
        await _ledger_fill(
            db, portfolio_id, side=Side.BUY, quantity="2", price="100",
            at=DAY + timedelta(hours=1), apply=False,
        )

        with pytest.raises(UnaccountedClose):
            await realised_pnl(db.pool, portfolio_id, since=DAY, until=NEXT_DAY)

    async def test_a_portfolio_that_has_never_traded_realises_zero(
        self, db, portfolio_id
    ):
        result = await realised_pnl(db.pool, portfolio_id, since=DAY, until=NEXT_DAY)
        assert result == Decimal(0)

    async def test_unknown_portfolio_raises(self, db):
        with pytest.raises(UnknownPortfolio):
            await realised_pnl(db.pool, uuid4(), since=DAY, until=NEXT_DAY)

    async def test_naive_bounds_are_refused(self, db, portfolio_id):
        with pytest.raises(ValueError, match="naive"):
            await realised_pnl(
                db.pool, portfolio_id, since=DAY.replace(tzinfo=None), until=NEXT_DAY
            )
        with pytest.raises(ValueError, match="naive"):
            await realised_pnl(
                db.pool, portfolio_id, since=DAY, until=NEXT_DAY.replace(tzinfo=None)
            )

    async def test_an_inverted_window_is_refused(self, db, portfolio_id):
        with pytest.raises(ValueError, match="before"):
            await realised_pnl(db.pool, portfolio_id, since=NEXT_DAY, until=DAY)

    async def test_each_venue_and_market_type_closes_against_its_own_entry(
        self, db, portfolio_id
    ):
        await _ledger_fill(
            db, portfolio_id, side=Side.BUY, quantity="2", price="100",
            at=DAY + timedelta(hours=1),
        )
        await _ledger_fill(
            db, portfolio_id, side=Side.BUY, quantity="2", price="140",
            at=DAY + timedelta(hours=2), venue="kraken",
        )
        await _ledger_fill(
            db, portfolio_id, side=Side.SELL, quantity="2", price="150",
            at=DAY + timedelta(hours=3), venue="kraken",
        )

        result = await realised_pnl(db.pool, portfolio_id, since=DAY, until=NEXT_DAY)

        # The kraken leg closed against 140, its own entry -- not against 100,
        # and not against a blend of the two venues.
        assert result == Decimal(20)


class TestSchema:
    async def test_flat_position_row_is_rejected(self, db, portfolio_id):
        with pytest.raises(asyncpg.CheckViolationError):
            await db.pool.execute(
                "INSERT INTO position (portfolio_id, venue, symbol, market_type, "
                "quantity, average_entry) VALUES ($1, 'binance', 'BTC/USD', 'spot', 0, 100)",
                portfolio_id,
            )

    async def test_unknown_market_type_is_rejected(self, db, portfolio_id):
        with pytest.raises(asyncpg.CheckViolationError):
            await db.pool.execute(
                "INSERT INTO position (portfolio_id, venue, symbol, market_type, "
                "quantity, average_entry) VALUES ($1, 'binance', 'BTC/USD', 'futures', 1, 100)",
                portfolio_id,
            )

    async def test_one_row_per_venue_symbol_market_type(self, db, portfolio_id):
        await db.pool.execute(
            "INSERT INTO position (portfolio_id, venue, symbol, market_type, "
            "quantity, average_entry) VALUES ($1, 'binance', 'BTC/USD', 'spot', 1, 100)",
            portfolio_id,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await db.pool.execute(
                "INSERT INTO position (portfolio_id, venue, symbol, market_type, "
                "quantity, average_entry) VALUES ($1, 'binance', 'BTC/USD', 'spot', 2, 200)",
                portfolio_id,
            )

    async def test_negative_locked_cash_is_rejected(self, db, portfolio_id):
        with pytest.raises(asyncpg.CheckViolationError):
            await db.pool.execute(
                "INSERT INTO cash_balance (portfolio_id, venue, asset, free, locked) "
                "VALUES ($1, 'binance', 'USD', 0, -1)",
                portfolio_id,
            )
