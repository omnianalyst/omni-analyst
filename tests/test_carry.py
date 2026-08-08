"""The carry-funding producer: persistent perp funding -> a directional call.

Pure-logic tests cover the barrier/confidence construction (the load-bearing
arithmetic and orientation); integration tests prove it reads funding and price
coverage through the real visibility rule and records through the real ledger.
Every test states what bug it catches.

The dedicated ``TEST_DATABASE_URL`` (``omni_v2_agent_carry``) keeps this suite
off the shared test database: concurrent agents TRUNCATE it.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from omni.conviction.carry import (
    carry_call,
    produce_carry_prediction_from_coverage,
)

PERSISTENCE = 8
SETTLEMENT = timedelta(hours=8)


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _entity(db):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('crypto_asset','BTC','BTC') RETURNING id"
    )


async def _price_claim(db, entity_id, close, event_date):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'price_snapshot','close',$2::jsonb,'cg',$3,$3,1.0,'allowed')",
        entity_id,
        json.dumps({"close": close}),
        event_date,
    )


async def _funding_claim(db, entity_id, rate, event_date):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'funding_rate','binance:BTC',$2::jsonb,'deriv',$3,$3,1.0,'allowed')",
        entity_id,
        json.dumps({"rate": rate, "symbol": "BTC", "venue": "binance"}),
        event_date,
    )


async def _seed_prices(db, entity_id, as_of, n=20):
    # A varying series so realized vol is nonzero (and finite). Entry is the last.
    for i in range(n):
        await _price_claim(db, entity_id, 100.0 + (i % 4), as_of - timedelta(days=n - 1 - i))


async def _seed_funding(db, entity_id, rates, end_at):
    # `rates` oldest-first; settlements spaced `SETTLEMENT` apart, ending at end_at.
    start = end_at - SETTLEMENT * (len(rates) - 1)
    for i, r in enumerate(rates):
        await _funding_claim(db, entity_id, r, start + SETTLEMENT * i)


class TestCarryCallArithmetic:
    def test_invalidation_barrier_erases_expected_carry_down(self):
        # Hand-derived, NOT copied from the implementation.
        # mean_rate = 0.0001 (positive -> short collects -> direction down),
        # 10 settlements remaining -> expected carry = 0.0001 * 10 = 0.001.
        # carry_distance = entry * expected_carry = 100 * 0.001 = 0.1.
        # A rise of 0.1 from entry 100 -> invalidation (upper) = 100.1.
        # target (lower) = entry - target_k * vol = 100 - 2 * 5 = 90.0.
        entry, vol, mean_rate, settlements, k = 100.0, 5.0, 0.0001, 10, 2.0
        expected_carry = mean_rate * settlements
        carry_distance = entry * abs(expected_carry)
        out = carry_call(
            entry=entry,
            vol=vol,
            mean_rate=mean_rate,
            settlements_remaining=settlements,
            target_k=k,
        )
        assert out is not None
        direction, upper, lower, conf = out
        assert direction == "down"
        assert upper == pytest.approx(entry + carry_distance)
        assert upper == pytest.approx(100.1)  # the hand-derived level
        assert lower == pytest.approx(90.0)
        assert 0.0 < conf < 1.0

    def test_invalidation_barrier_erases_expected_carry_up(self):
        # Negative mean -> long collects -> direction up. Invalidation is a FALL
        # to entry - carry_distance.
        entry, vol, mean_rate, settlements, k = 100.0, 5.0, -0.0002, 10, 2.0
        expected_carry = mean_rate * settlements
        carry_distance = entry * abs(expected_carry)
        out = carry_call(
            entry=entry,
            vol=vol,
            mean_rate=mean_rate,
            settlements_remaining=settlements,
            target_k=k,
        )
        assert out is not None
        direction, upper, lower, _ = out
        assert direction == "up"
        assert lower == pytest.approx(entry - carry_distance)
        assert lower == pytest.approx(100.0 - 0.2)
        assert upper == pytest.approx(110.0)  # target = entry + k*vol

    def test_barriers_straddle_entry_in_the_directions_orientation(self):
        # down: invalidation ABOVE (upper), target BELOW (lower).
        # up:   target ABOVE (upper), invalidation BELOW (lower).
        down = carry_call(entry=100.0, vol=5.0, mean_rate=0.0001, settlements_remaining=10)
        up = carry_call(entry=100.0, vol=5.0, mean_rate=-0.0001, settlements_remaining=10)
        assert down is not None and up is not None
        _, d_upper, d_lower, _ = down
        _, u_upper, u_lower, _ = up
        assert d_upper > 100.0 > d_lower
        assert u_upper > 100.0 > u_lower
        # Orientation: the carry-derived barrier sits on the AGAINST side.
        assert d_upper - 100.0 == pytest.approx(100.0 * 0.0001 * 10)  # just above
        assert 100.0 - u_lower == pytest.approx(100.0 * 0.0001 * 10)  # just below

    def test_zero_realized_vol_abstains(self):
        # A flat price series -> vol ~ 0 -> no honest target barrier. Refuse.
        assert carry_call(entry=100.0, vol=0.0, mean_rate=0.0001, settlements_remaining=10) is None

    def test_non_finite_vol_abstains(self):
        assert (
            carry_call(entry=100.0, vol=float("inf"), mean_rate=0.0001, settlements_remaining=10)
            is None
        )

    def test_zero_expected_carry_abstains(self):
        # settlements_remaining = 0 -> nothing to collect -> no edge to protect.
        assert carry_call(entry=100.0, vol=5.0, mean_rate=0.0001, settlements_remaining=0) is None

    def test_non_finite_expected_carry_abstains(self):
        assert (
            carry_call(entry=100.0, vol=5.0, mean_rate=float("nan"), settlements_remaining=10)
            is None
        )


class TestProduceFromCoverage:
    async def test_persistently_positive_funding_produces_a_down_prediction(self, db):
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        await _seed_funding(db, e, ["0.0001"] * PERSISTENCE, as_of)
        horizon = as_of + SETTLEMENT * 10  # 10 settlements remaining

        pid = await produce_carry_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT method, direction, entry_price, upper_barrier, lower_barrier "
            "FROM prediction WHERE id=$1",
            pid,
        )
        assert row["method"] == "carry.funding"
        assert row["direction"] == "down"
        entry = float(row["entry_price"])
        upper = float(row["upper_barrier"])
        lower = float(row["lower_barrier"])
        assert upper > entry > lower
        # The invalidation is the carry-erasing level, hand-derived:
        # upper = entry * (1 + mean_rate * settlements) = entry * 1.001.
        assert upper == pytest.approx(entry * (1 + 0.0001 * 10))

    async def test_persistently_negative_funding_produces_an_up_prediction(self, db):
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        await _seed_funding(db, e, ["-0.0001"] * PERSISTENCE, as_of)
        horizon = as_of + SETTLEMENT * 10

        pid = await produce_carry_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT direction, entry_price, lower_barrier FROM prediction WHERE id=$1",
            pid,
        )
        assert row["direction"] == "up"
        entry = float(row["entry_price"])
        lower = float(row["lower_barrier"])
        # Invalidation is a fall that erases carry: lower = entry * (1 - 0.001).
        assert lower == pytest.approx(entry * (1 - 0.0001 * 10))

    async def test_a_sign_flip_inside_the_window_abstains(self, db):
        # The headline: carry that is not persistent is not carry. One flipped
        # settlement among the trailing PERSISTENCE breaks the stream.
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        rates = ["0.0001"] * (PERSISTENCE - 1) + ["-0.0001"]
        await _seed_funding(db, e, rates, as_of)
        horizon = as_of + SETTLEMENT * 10

        pid = await produce_carry_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_fewer_than_persistence_settlements_abstains(self, db):
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        await _seed_funding(db, e, ["0.0001"] * (PERSISTENCE - 1), as_of)
        horizon = as_of + SETTLEMENT * 10

        pid = await produce_carry_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_a_nan_funding_rate_abstains_rather_than_propagating(self, db):
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        rates = ["0.0001"] * (PERSISTENCE - 1) + ["NaN"]
        await _seed_funding(db, e, rates, as_of)
        horizon = as_of + SETTLEMENT * 10

        pid = await produce_carry_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_a_flat_price_series_abstains_via_zero_vol(self, db):
        # Every close identical -> realized vol ~ 0 -> no honest target barrier.
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        for i in range(20):
            await _price_claim(db, e, 100.0, as_of - timedelta(days=19 - i))
        await _seed_funding(db, e, ["0.0001"] * PERSISTENCE, as_of)
        horizon = as_of + SETTLEMENT * 10

        pid = await produce_carry_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
        )
        assert pid is None

    async def test_no_price_coverage_abstains(self, db):
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        # Funding but no price coverage at all -> nothing to anchor entry on.
        await _seed_funding(db, e, ["0.0001"] * PERSISTENCE, as_of)
        horizon = as_of + SETTLEMENT * 10

        pid = await produce_carry_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
        )
        assert pid is None

    async def test_a_horizon_with_no_settlement_abstains(self, db):
        # Horizon shorter than one settlement interval -> no settlement occurs ->
        # no carry to collect -> expected carry ~ 0.
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        await _seed_funding(db, e, ["0.0001"] * PERSISTENCE, as_of)
        horizon = as_of + timedelta(hours=1)  # well inside one 8h settlement

        pid = await produce_carry_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
        )
        assert pid is None

    async def test_method_column_is_carry_funding(self, db):
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        await _seed_funding(db, e, ["0.0001"] * PERSISTENCE, as_of)
        horizon = as_of + SETTLEMENT * 10

        pid = await produce_carry_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
        )
        assert pid is not None
        method = await db.pool.fetchval("SELECT method FROM prediction WHERE id=$1", pid)
        assert method == "carry.funding"
