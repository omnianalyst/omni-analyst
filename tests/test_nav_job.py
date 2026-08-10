"""Recording one NAV point, against a real database.

The failure that matters here is not a crash. It is a NAV that gets written
while one position could not be priced: it reads as authoritative, it
understates exactly the exposure nobody could value, and every later point on
the curve inherits the error without anything marking it.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from omni.portfolio.state import create_portfolio
from omni.trading.nav_job import Unmarkable, snapshot
from omni.venue.paper_venue import Bar, PaperVenue, RecordedBars
from omni.venue.protocol import Capabilities

NOW = datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
VENUE = "paper"
PRICE = Decimal(100)
SYMBOLS = ("AAA/USD", "BBB/USD")

CAPABILITIES = Capabilities(
    spot=True, margin=False, perpetuals=True, limit_orders=True, shorting=True,
    funding_data=True, maker_fee_bps=Decimal(0), taker_fee_bps=Decimal(5),
    min_notional=Decimal(0),
)


@pytest.fixture
def venue() -> PaperVenue:
    bars = RecordedBars()
    for symbol in SYMBOLS:
        bars.add(
            Bar(symbol=symbol, open=PRICE, high=PRICE, low=PRICE, close=PRICE,
                volume=Decimal(1_000_000), at=NOW - timedelta(days=30))
        )
    return PaperVenue(bars, CAPABILITIES, name=VENUE, spread_bps=Decimal(4),
                      starting_balances={"USD": Decimal(100000)})


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, users CASCADE")
    yield


@pytest.fixture
async def owner(db) -> UUID:
    return await db.pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1,$2) RETURNING id",
        f"nav-{uuid4().hex}@omni.test", "not-a-real-hash",
    )


@pytest.fixture
async def portfolio_id(db, owner) -> UUID:
    book = await create_portfolio(
        db.pool, user_id=owner, name="carry book", base_currency="USD",
        opening_cash=Decimal(1000), cash_venue=VENUE,
    )
    return book.portfolio_id


async def _world(db, owner, *, priced=SYMBOLS) -> dict[str, UUID]:
    ids = {}
    for symbol in SYMBOLS:
        entity_id = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('crypto_asset',$1,$1) "
            "RETURNING id", symbol,
        )
        ids[symbol] = entity_id
        if symbol in priced:
            await db.pool.execute(
                "INSERT INTO claim (entity_id, claim_type, key, value, source, "
                "event_date, knowledge_date, confidence, redistributable, "
                "audience_user_id) VALUES ($1,'price_snapshot'::claim_type,$2,"
                "$3::jsonb,'prices',$4,$4,1.0,'byo_only',$5)",
                entity_id, f"{VENUE}:{symbol}",
                json.dumps({"close": str(PRICE), "venue": VENUE}),
                NOW - timedelta(days=10), owner,
            )
    return ids


async def _position(db, portfolio_id, symbol, quantity, market="spot"):
    await db.pool.execute(
        "INSERT INTO position (portfolio_id, venue, symbol, market_type, quantity, "
        "average_entry) VALUES ($1,$2,$3,$4,$5,$6)",
        portfolio_id, VENUE, symbol, market, quantity, PRICE,
    )


async def _rows(db, portfolio_id):
    return await db.pool.fetch(
        "SELECT * FROM nav_snapshot WHERE portfolio_id=$1 ORDER BY taken_at",
        portfolio_id,
    )


class TestTheCurveStartsAtTheDeposit:
    async def test_an_empty_book_records_its_cash(
        self, db, owner, portfolio_id, venue
    ):
        """The first point is the money arriving, not the first trade.

        A book holding nothing has a NAV -- its cash -- and recording it is what
        makes the deposit visible on the curve.
        """
        ids = await _world(db, owner)

        nav = await snapshot(
            db.pool, venue=venue, portfolio_id=portfolio_id,
            entity_ids=list(ids.values()), audience_user_id=owner, at=NOW,
        )

        assert nav == Decimal(1000)
        (row,) = await _rows(db, portfolio_id)
        assert row["nav"] == Decimal(1000)
        assert row["cash"] == Decimal(1000)
        assert row["gross_exposure"] == 0


class TestAPartialNavIsNeverWritten:
    async def test_an_unpriced_position_refuses_and_writes_nothing(
        self, db, owner, portfolio_id, venue
    ):
        """The failure this module exists to prevent.

        A NAV missing one position is not a smaller NAV. It is a wrong one that
        reads as authoritative, and every later point inherits the error.
        """
        ids = await _world(db, owner, priced=("AAA/USD",))
        await _position(db, portfolio_id, "BBB/USD", Decimal(5))

        with pytest.raises(Unmarkable, match="no price"):
            await snapshot(
                db.pool, venue=venue, portfolio_id=portfolio_id,
                entity_ids=list(ids.values()), audience_user_id=owner, at=NOW,
            )

        assert await _rows(db, portfolio_id) == []

    async def test_a_position_outside_the_universe_refuses(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)
        await _position(db, portfolio_id, "ZZZ/USD", Decimal(1))

        with pytest.raises(Unmarkable, match="no entity"):
            await snapshot(
                db.pool, venue=venue, portfolio_id=portfolio_id,
                entity_ids=list(ids.values()), audience_user_id=owner, at=NOW,
            )

        assert await _rows(db, portfolio_id) == []


class TestTheMarkIsPointInTime:
    async def test_a_naive_instant_is_refused(self, db, owner, portfolio_id, venue):
        ids = await _world(db, owner)
        with pytest.raises(ValueError, match="naive"):
            await snapshot(
                db.pool, venue=venue, portfolio_id=portfolio_id,
                entity_ids=list(ids.values()), audience_user_id=owner,
                at=datetime(2026, 3, 1, 5, 0),  # noqa: DTZ001
            )

    async def test_a_marked_pair_values_both_legs(
        self, db, owner, portfolio_id, venue
    ):
        """Long spot against short perp, equal size, marked at one price.

        NAV takes the perpetual's unrealised P&L while gross exposure takes the
        full marked notional of both legs, so the two columns answer different
        questions rather than disagreeing.
        """
        ids = await _world(db, owner)
        await _position(db, portfolio_id, "AAA/USD", Decimal(2), "spot")
        await _position(db, portfolio_id, "AAA/USD", Decimal(-2), "perpetual")

        await snapshot(
            db.pool, venue=venue, portfolio_id=portfolio_id,
            entity_ids=list(ids.values()), audience_user_id=owner, at=NOW,
        )

        (row,) = await _rows(db, portfolio_id)
        assert row["gross_exposure"] == Decimal(400)
        assert row["net_exposure"] == 0

    async def test_snapshots_accumulate_as_a_series(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)
        for day in range(3):
            await snapshot(
                db.pool, venue=venue, portfolio_id=portfolio_id,
                entity_ids=list(ids.values()), audience_user_id=owner,
                at=NOW + timedelta(days=day),
            )

        rows = await _rows(db, portfolio_id)
        assert len(rows) == 3
        assert all(r["nav"] == Decimal(1000) for r in rows)
