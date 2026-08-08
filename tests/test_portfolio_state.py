import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest

from omni.portfolio import state
from omni.portfolio.state import (
    UnknownPortfolio,
    UnmarkedPosition,
    apply_fill,
    load,
    rebuild_from_fills,
    snapshot_nav,
)
from omni.venue.protocol import Fill, MarketType, Side

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


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
            await snapshot_nav(db.pool, portfolio_id, {"BTC/USD": Decimal(150)})

        assert await db.pool.fetchval(
            "SELECT count(*) FROM nav_snapshot WHERE portfolio_id = $1", portfolio_id
        ) == 0

    async def test_nav_marks_the_book_and_records_it(self, db, portfolio_id):
        await apply_fill(db.pool, portfolio_id, _fill(quantity="2", price="100"), MarketType.SPOT)

        nav = await snapshot_nav(db.pool, portfolio_id, {"BTC/USD": Decimal(150)})

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

        nav = await snapshot_nav(db.pool, portfolio_id, {"BTC/USD": Decimal(90)})

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
                await snapshot_nav(db.pool, portfolio_id, {"BTC/USD": bad})

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
