"""The OI-divergence producer: OI vs price direction -> a reversion call.

Pure-logic tests cover the barrier/confidence construction and the
direction/agreement logic (the load-bearing arithmetic and orientation);
integration tests prove the producer reads OI and price coverage through the
real visibility rule, abstains honestly on every degenerate input, and records
through the real ledger. Every test states what bug it catches.

The dedicated ``TEST_DATABASE_URL`` (``omni_v2_agent_oi_divergence``) keeps this
suite off the shared test database: concurrent agents TRUNCATE it.
"""

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from omni.conviction.oi_divergence import (
    oi_divergence_call,
    produce_oi_divergence_prediction_from_coverage,
)

WINDOW = 10


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


async def _oi_claim(db, entity_id, contracts, event_date):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'open_interest','binance:BTC',$2::jsonb,'deriv',$3,$3,1.0,'allowed')",
        entity_id,
        json.dumps(
            {"contracts": contracts, "symbol": "BTC", "venue": "binance"}
        ),
        event_date,
    )


async def _seed_series(db, entity_id, as_of, *, closes, oi):
    # `closes` and `oi` oldest-first; one claim per day ending at `as_of`.
    n = len(closes)
    assert len(oi) == n
    for i in range(n):
        day = as_of - timedelta(days=n - 1 - i)
        await _price_claim(db, entity_id, closes[i], day)
        await _oi_claim(db, entity_id, oi[i], day)


class TestOiDivergenceCall:
    def test_up_call_when_price_fell_and_oi_rose(self):
        # Hand-derived, NOT copied from the implementation.
        # window_start=110, entry=100 -> price fell 10 (price_move = -10).
        # OI rose (oi_move = +500). Divergence -> crowded short -> squeeze -> UP.
        # invalidation = 2*entry - window_start = 2*100 - 110 = 90 (the level at
        #   which the 10-point down-leg has doubled to 20 against the up call:
        #   100 -> 90 is another 10 down).
        # target upper = entry + target_k*vol = 100 + 2*4 = 108.
        # confidence = (entry - lower)/(upper - lower) = (100-90)/(108-90) = 10/18.
        entry, window_start, vol, k = 100.0, 110.0, 4.0, 2.0
        out = oi_divergence_call(
            entry=entry,
            window_start=window_start,
            vol=vol,
            oi_move=500.0,
            price_move=entry - window_start,
            target_k=k,
        )
        assert out is not None
        direction, upper, lower, conf = out
        assert direction == "up"
        assert upper == pytest.approx(108.0)  # entry + 2*vol
        assert lower == pytest.approx(90.0)  # 2*entry - window_start
        assert conf == pytest.approx(10.0 / 18.0)

    def test_down_call_when_price_rose_and_oi_fell(self):
        # window_start=90, entry=100 -> price rose 10 (price_move = +10).
        # OI fell (oi_move = -500). Divergence -> covering rally fades -> DOWN.
        # invalidation = 2*entry - window_start = 200 - 90 = 110 (a further 10 up
        #   doubles the up-leg against the down call).
        # target lower = entry - target_k*vol = 100 - 8 = 92.
        # confidence = (upper - entry)/(upper - lower) = (110-100)/(110-92) = 10/18.
        entry, window_start, vol, k = 100.0, 90.0, 4.0, 2.0
        out = oi_divergence_call(
            entry=entry,
            window_start=window_start,
            vol=vol,
            oi_move=-500.0,
            price_move=entry - window_start,
            target_k=k,
        )
        assert out is not None
        direction, upper, lower, conf = out
        assert direction == "down"
        assert upper == pytest.approx(110.0)  # 2*entry - window_start
        assert lower == pytest.approx(92.0)  # entry - 2*vol
        assert conf == pytest.approx(10.0 / 18.0)

    def test_invalidation_barrier_is_the_doubled_divergent_leg(self):
        # The arithmetic, written out step by step rather than mirrored from output.
        # A price leg of size L (window_start -> entry) invalidates at entry -/+ L,
        # which is exactly 2*entry - window_start.
        entry, window_start = 250.0, 275.0  # price fell by 25
        leg = abs(window_start - entry)  # 25
        expected_invalidation = entry - leg  # 225
        # and algebraically:
        assert expected_invalidation == 2.0 * entry - window_start
        out = oi_divergence_call(
            entry=entry,
            window_start=window_start,
            vol=6.0,
            oi_move=+1.0,  # OI rose -> diverges from the price fall
            price_move=entry - window_start,
        )
        assert out is not None
        _, upper, lower, _ = out
        # price fell -> up call -> invalidation is the LOWER barrier.
        assert lower == pytest.approx(225.0)
        assert lower == pytest.approx(2.0 * entry - window_start)
        assert upper == pytest.approx(entry + 2.0 * 6.0)

    def test_barriers_straddle_entry_in_each_directions_orientation(self):
        # up:   target ABOVE (upper), invalidation BELOW (lower).
        # down: invalidation ABOVE (upper), target BELOW (lower).
        up = oi_divergence_call(
            entry=100.0, window_start=110.0, vol=4.0,
            oi_move=+1.0, price_move=-10.0,
        )
        down = oi_divergence_call(
            entry=100.0, window_start=90.0, vol=4.0,
            oi_move=-1.0, price_move=+10.0,
        )
        assert up is not None and down is not None
        _, u_up, u_low, _ = up
        _, d_up, d_low, _ = down
        assert u_up > 100.0 > u_low
        assert d_up > 100.0 > d_low
        # Orientation: the invalidation sits on the side of the divergent leg.
        # up call (price had fallen): invalidation is below -- lower = 2*entry - 110.
        assert u_low == pytest.approx(2.0 * 100.0 - 110.0)
        # down call (price had risen): invalidation is above -- upper = 2*entry - 90.
        assert d_up == pytest.approx(2.0 * 100.0 - 90.0)

    def test_confidence_is_monotonic_in_divergence_strength(self):
        # Discriminator: a call on a larger divergent leg (more price travel vs
        # vol) must read higher confidence. If confidence were constant, the
        # conviction gate could not calibrate on it.
        vol, k = 4.0, 2.0
        confs = []
        for leg in (2.0, 4.0, 8.0, 16.0, 32.0):  # window_start this far above entry
            entry = 100.0
            out = oi_divergence_call(
                entry=entry,
                window_start=entry + leg,
                vol=vol,
                oi_move=+1.0,
                price_move=-leg,
                target_k=k,
            )
            assert out is not None
            confs.append(out[3])
        for a, b in pairwise(confs):
            assert b > a, confs
        assert confs[-1] - confs[0] > 0.2  # spans a meaningful range, not collapsed

    def test_agreement_is_not_a_signal_and_abstains(self):
        # Both rising and both falling are CONFIRMATION, not divergence.
        assert oi_divergence_call(
            entry=100.0, window_start=90.0, vol=4.0,
            oi_move=+500.0, price_move=+10.0,  # both up
        ) is None
        assert oi_divergence_call(
            entry=100.0, window_start=110.0, vol=4.0,
            oi_move=-500.0, price_move=-10.0,  # both down
        ) is None

    def test_a_zero_move_abstains(self):
        # A flat OI or flat price has no direction to diverge from.
        assert oi_divergence_call(
            entry=100.0, window_start=100.0, vol=4.0,
            oi_move=+500.0, price_move=0.0,
        ) is None
        assert oi_divergence_call(
            entry=100.0, window_start=110.0, vol=4.0,
            oi_move=0.0, price_move=-10.0,
        ) is None

    def test_non_finite_inputs_abstain(self):
        # Every comparison against NaN is False, so a range check written as a
        # comparison silently passes NaN through. These must refuse explicitly.
        base = {
            "entry": 100.0,
            "window_start": 110.0,
            "oi_move": +1.0,
            "price_move": -10.0,
        }
        assert oi_divergence_call(**base, vol=float("nan")) is None
        assert oi_divergence_call(**base, vol=float("inf")) is None
        assert oi_divergence_call(**base, vol=4.0) is not None  # control
        assert (
            oi_divergence_call(
                entry=100.0, window_start=110.0, vol=4.0,
                oi_move=float("nan"), price_move=-10.0,
            )
            is None
        )
        assert (
            oi_divergence_call(
                entry=100.0, window_start=110.0, vol=4.0,
                oi_move=+1.0, price_move=float("nan"),
            )
            is None
        )

    def test_zero_or_negative_vol_abstains(self):
        # A flat price series -> vol ~ 0 -> target collapses onto entry. Refuse.
        assert oi_divergence_call(
            entry=100.0, window_start=110.0, vol=0.0,
            oi_move=+1.0, price_move=-10.0,
        ) is None
        assert oi_divergence_call(
            entry=100.0, window_start=110.0, vol=-1.0,
            oi_move=+1.0, price_move=-10.0,
        ) is None


class TestProduceFromCoverage:
    async def test_price_falling_oi_rising_produces_an_up_prediction(self, db):
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        # 10 descending closes (price fell) and 10 ascending OI (OI rose).
        closes = [100.0 - i for i in range(WINDOW)]  # 100 .. 91
        oi = [1000.0 + i * 100 for i in range(WINDOW)]  # 1000 .. 1900
        await _seed_series(db, e, as_of, closes=closes, oi=oi)

        pid = await produce_oi_divergence_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7),
            as_of=as_of,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT method, direction, entry_price, upper_barrier, lower_barrier "
            "FROM prediction WHERE id=$1",
            pid,
        )
        assert row["method"] == "oi.divergence"
        assert row["direction"] == "up"
        entry = float(row["entry_price"])
        upper = float(row["upper_barrier"])
        lower = float(row["lower_barrier"])
        assert upper > entry > lower
        # Hand-derived invalidation from the SEEDED endpoints, not the impl:
        # first close = 100, last close (entry) = 91 -> 2*91 - 100 = 82.
        first_close, last_close = closes[0], closes[-1]
        assert lower == pytest.approx(2.0 * last_close - first_close)
        assert lower == pytest.approx(82.0)

    async def test_price_rising_oi_falling_produces_a_down_prediction(self, db):
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        closes = [90.0 + i for i in range(WINDOW)]  # 90 .. 99 (price rose)
        oi = [2000.0 - i * 100 for i in range(WINDOW)]  # 2000 .. 1100 (OI fell)
        await _seed_series(db, e, as_of, closes=closes, oi=oi)

        pid = await produce_oi_divergence_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7),
            as_of=as_of,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT direction, entry_price, upper_barrier, lower_barrier "
            "FROM prediction WHERE id=$1",
            pid,
        )
        assert row["direction"] == "down"
        entry = float(row["entry_price"])
        upper = float(row["upper_barrier"])
        lower = float(row["lower_barrier"])
        assert upper > entry > lower
        # Hand-derived: first close 90, entry 99 -> invalidation = 2*99 - 90 = 108.
        assert upper == pytest.approx(2.0 * closes[-1] - closes[0])
        assert upper == pytest.approx(108.0)

    async def test_oi_and_price_both_rising_abstains(self, db):
        # Agreement is confirmation, not divergence. No call.
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        closes = [90.0 + i for i in range(WINDOW)]
        oi = [1000.0 + i * 100 for i in range(WINDOW)]  # both rising
        await _seed_series(db, e, as_of, closes=closes, oi=oi)

        pid = await produce_oi_divergence_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7),
            as_of=as_of,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_short_oi_history_abstains(self, db):
        # WINDOW price points but only WINDOW-1 OI points -> cannot measure an OI
        # direction over the window. Honest refusal.
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        for i in range(WINDOW):
            await _price_claim(db, e, 100.0 - i, as_of - timedelta(days=WINDOW - 1 - i))
        for i in range(WINDOW - 1):
            await _oi_claim(db, e, 1000.0 + i * 100, as_of - timedelta(days=WINDOW - 2 - i))

        pid = await produce_oi_divergence_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7),
            as_of=as_of,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_short_price_history_abstains(self, db):
        # WINDOW OI points but only WINDOW-1 price points -> no entry/window_start.
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        for i in range(WINDOW - 1):
            await _price_claim(db, e, 100.0 - i, as_of - timedelta(days=WINDOW - 2 - i))
        for i in range(WINDOW):
            await _oi_claim(db, e, 1000.0 + i * 100, as_of - timedelta(days=WINDOW - 1 - i))

        pid = await produce_oi_divergence_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7),
            as_of=as_of,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_flat_oi_abstains(self, db):
        # OI unchanged across the window -> no OI direction -> cannot diverge.
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        closes = [100.0 - i for i in range(WINDOW)]  # price fell
        oi = [1500.0] * WINDOW  # flat
        await _seed_series(db, e, as_of, closes=closes, oi=oi)

        pid = await produce_oi_divergence_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7),
            as_of=as_of,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_flat_price_abstains(self, db):
        # Price unchanged -> no price direction (and zero vol) -> cannot diverge.
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        closes = [100.0] * WINDOW  # flat
        oi = [1000.0 + i * 100 for i in range(WINDOW)]  # OI rose
        await _seed_series(db, e, as_of, closes=closes, oi=oi)

        pid = await produce_oi_divergence_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7),
            as_of=as_of,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_a_nan_oi_input_abstains_rather_than_propagating(self, db):
        # A NaN in the visible OI window must poison the finiteness check into
        # abstaining -- never be dropped and never flow into a "confident" call.
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        days = [as_of - timedelta(days=WINDOW - 1 - i) for i in range(WINDOW)]
        for i, day in enumerate(days):
            await _price_claim(db, e, 100.0 - i, day)
        # First WINDOW-1 OI samples good; the most recent one (at as_of) is NaN,
        # and is what the trailing window reads.
        for i in range(WINDOW - 1):
            await _oi_claim(db, e, 1000.0 + i * 100, days[i])
        await _oi_claim(db, e, "NaN", days[-1])

        pid = await produce_oi_divergence_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7),
            as_of=as_of,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_method_column_is_oi_divergence(self, db):
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        closes = [100.0 - i for i in range(WINDOW)]
        oi = [1000.0 + i * 100 for i in range(WINDOW)]
        await _seed_series(db, e, as_of, closes=closes, oi=oi)

        pid = await produce_oi_divergence_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7),
            as_of=as_of,
            method="oi.divergence",
        )
        assert pid is not None
        method = await db.pool.fetchval("SELECT method FROM prediction WHERE id=$1", pid)
        assert method == "oi.divergence"
