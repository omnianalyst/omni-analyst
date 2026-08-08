"""The fundamentals.protocol producer: P/F mean reversion -> a directional call.

Pure-logic tests cover the barrier/confidence construction (the load-bearing
arithmetic and orientation); integration tests prove it reads market-cap, fee
and revenue coverage through the real visibility rule -- CoinGecko market caps
are ``byo_only``, DefiLlama fees/revenue are ``allowed`` -- and records through
the real ledger. Every test states what bug it catches.

The dedicated ``TEST_DATABASE_URL`` (``omni_v2_agent_protocol_fundamentals``)
keeps this suite off the shared test database: concurrent agents TRUNCATE it.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from omni.conviction.protocol_fundamentals import (
    produce_protocol_fundamentals_prediction_from_coverage,
    protocol_call,
)

WINDOW = 30
AUDIENCE = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _entity(db):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('crypto_asset','UNI','Uniswap') RETURNING id"
    )


async def _marketcap_claim(db, entity_id, price, market_cap, event_date, audience):
    # CoinGecko market caps are byo_only: pinned to the credential owner, never
    # shared. price and market_cap ride in the same value dict.
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable, audience_user_id) "
        "VALUES ($1,'price_snapshot','coingecko',$2::jsonb,'coingecko',$3,$3,1.0,"
        "'byo_only',$4)",
        entity_id,
        json.dumps({"price": price, "market_cap": market_cap}),
        event_date,
        audience,
    )


async def _fees_claim(db, entity_id, fees, event_date):
    # DefiLlama fees are allowed: shared network coverage, no key required.
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'protocol_fees','defillama',$2::jsonb,'defillama',$3,$3,1.0,"
        "'allowed')",
        entity_id,
        json.dumps({"fees": fees}),
        event_date,
    )


async def _revenue_claim(db, entity_id, revenue, event_date):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'protocol_revenue','defillama',$2::jsonb,'defillama',$3,$3,1.0,"
        "'allowed')",
        entity_id,
        json.dumps({"revenue": revenue}),
        event_date,
    )


async def _seed_window(
    db,
    entity_id,
    *,
    market_caps,
    fees=1.0,
    revenue=10.0,
    audience=AUDIENCE,
    end_at,
):
    """Seed ``len(market_caps)`` aligned daily snapshots ending at ``end_at``.

    price mirrors market_cap (supply of 1) so the entry equals the current
    market cap; fees default to 1.0 so the multiple equals the market cap. This
    lets each test control the multiple series directly through ``market_caps``.
    """
    n = len(market_caps)
    for i, mcap in enumerate(market_caps):
        d = end_at - timedelta(days=n - 1 - i)
        await _marketcap_claim(db, entity_id, mcap, mcap, d, audience)
        await _fees_claim(db, entity_id, fees, d)
        await _revenue_claim(db, entity_id, revenue, d)


class TestProtocolCallArithmetic:
    def test_down_call_barriers_hand_derived(self):
        # Hand-derived, NOT copied from the implementation.
        # current multiple 25 is ABOVE its trailing mean 20 -> overvalued ->
        # direction down. sigma of the multiple = 5, barrier_k = 2.
        #   target (lower) = entry * (mean/current) = 100 * (20/25) = 80.0
        #   invalidation (upper) = entry * (1 + k*sigma/current)
        #                        = 100 * (1 + 2*5/25) = 100 * 1.4 = 140.0
        entry, cur, mean, sigma, k = 100.0, 25.0, 20.0, 5.0, 2.0
        out = protocol_call(
            entry=entry, current_multiple=cur, mean_multiple=mean,
            sigma_multiple=sigma, barrier_k=k,
        )
        assert out is not None
        direction, upper, lower, conf = out
        assert direction == "down"
        assert upper == pytest.approx(140.0)  # hand-derived invalidation
        assert lower == pytest.approx(80.0)  # hand-derived reversion target
        assert 0.0 < conf < 1.0

    def test_up_call_barriers_hand_derived(self):
        # current 16 BELOW mean 20 -> undervalued -> direction up. sigma=4, k=2.
        #   target (upper) = entry * (mean/current) = 100 * (20/16) = 125.0
        #   invalidation (lower) = entry * (1 - k*sigma/current)
        #                        = 100 * (1 - 2*4/16) = 100 * 0.5 = 50.0
        entry, cur, mean, sigma, k = 100.0, 16.0, 20.0, 4.0, 2.0
        out = protocol_call(
            entry=entry, current_multiple=cur, mean_multiple=mean,
            sigma_multiple=sigma, barrier_k=k,
        )
        assert out is not None
        direction, upper, lower, _ = out
        assert direction == "up"
        assert upper == pytest.approx(125.0)
        assert lower == pytest.approx(50.0)

    def test_barriers_straddle_entry_in_the_directions_orientation(self):
        # down: target BELOW (lower), invalidation ABOVE (upper).
        # up:   target ABOVE (upper), invalidation BELOW (lower).
        down = protocol_call(
            entry=100.0, current_multiple=25.0, mean_multiple=20.0, sigma_multiple=5.0
        )
        up = protocol_call(
            entry=100.0, current_multiple=16.0, mean_multiple=20.0, sigma_multiple=4.0
        )
        assert down is not None and up is not None
        _, d_upper, d_lower, _ = down
        _, u_upper, u_lower, _ = up
        assert d_upper > 100.0 > d_lower
        assert u_upper > 100.0 > u_lower
        # The target is the mean-reversion level: for down it sits at
        # entry*(mean/current) just below; for up just above.
        assert d_lower == pytest.approx(100.0 * 20.0 / 25.0)
        assert u_upper == pytest.approx(100.0 * 20.0 / 16.0)

    def test_flat_multiple_series_abstains(self):
        # sigma ~ 0 -> no dispersion to set an invalidation. Refuse.
        assert protocol_call(
            entry=100.0, current_multiple=20.0, mean_multiple=20.0, sigma_multiple=0.0
        ) is None

    def test_no_spread_between_current_and_mean_abstains(self):
        # current == mean -> nothing to revert. The target would sit on entry.
        assert protocol_call(
            entry=100.0, current_multiple=20.0, mean_multiple=20.0, sigma_multiple=5.0
        ) is None

    def test_nan_input_abstains_rather_than_propagating(self):
        # Every comparison against NaN is False, so a range check written as a
        # comparison passes it straight through. The explicit isfinite guard is
        # the only thing that stops a confident call computed from NaN.
        assert protocol_call(
            entry=100.0, current_multiple=float("nan"),
            mean_multiple=20.0, sigma_multiple=5.0,
        ) is None
        assert protocol_call(
            entry=100.0, current_multiple=25.0,
            mean_multiple=20.0, sigma_multiple=float("nan"),
        ) is None

    def test_inf_input_abstains(self):
        assert protocol_call(
            entry=100.0, current_multiple=float("inf"),
            mean_multiple=20.0, sigma_multiple=5.0,
        ) is None

    def test_non_positive_multiple_abstains(self):
        assert protocol_call(
            entry=100.0, current_multiple=-5.0, mean_multiple=20.0, sigma_multiple=5.0
        ) is None

    def test_extreme_sigma_making_a_negative_barrier_abstains(self):
        # k*sigma/current >= 1 would push the up-call invalidation to <= 0, a
        # price that can never be reached. Refuse rather than record an
        # unfalsifiable barrier.
        assert protocol_call(
            entry=100.0, current_multiple=10.0, mean_multiple=20.0, sigma_multiple=6.0,
            barrier_k=2.0,  # 2*6/10 = 1.2 -> lower = 100*(1-1.2) < 0
        ) is None


class TestProduceFromCoverage:
    async def test_overvalued_multiple_produces_a_down_prediction(self, db):
        # market caps half 15 half 25 ending at 25 -> mean 20, sigma 5, current 25.
        e = await _entity(db)
        end = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_window(db, e, market_caps=[15.0] * 15 + [25.0] * 15, end_at=end)
        horizon = end + timedelta(days=30)

        pid = await produce_protocol_fundamentals_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=AUDIENCE,
            horizon_ends_at=horizon, as_of=end,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT method, direction, entry_price, upper_barrier, lower_barrier "
            "FROM prediction WHERE id=$1",
            pid,
        )
        assert row["method"] == "fundamentals.protocol"
        assert row["direction"] == "down"
        entry = float(row["entry_price"])
        upper = float(row["upper_barrier"])
        lower = float(row["lower_barrier"])
        assert upper > entry > lower
        # Hand-derived: current=25, mean=20, sigma=5, k=2, entry=25.
        #   target (lower) = 25 * (20/25) = 20.0
        #   invalidation (upper) = 25 * (1 + 2*5/25) = 35.0
        assert entry == pytest.approx(25.0)
        assert lower == pytest.approx(20.0)
        assert upper == pytest.approx(35.0)

    async def test_undervalued_multiple_produces_an_up_prediction(self, db):
        # market caps half 25 half 15 ending at 15 -> mean 20, sigma 5, current 15.
        e = await _entity(db)
        end = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_window(db, e, market_caps=[25.0] * 15 + [15.0] * 15, end_at=end)
        horizon = end + timedelta(days=30)

        pid = await produce_protocol_fundamentals_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=AUDIENCE,
            horizon_ends_at=horizon, as_of=end,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT direction, entry_price, upper_barrier, lower_barrier "
            "FROM prediction WHERE id=$1",
            pid,
        )
        assert row["direction"] == "up"
        entry = float(row["entry_price"])
        upper = float(row["upper_barrier"])
        lower = float(row["lower_barrier"])
        # Hand-derived: current=15, mean=20, sigma=5, k=2, entry=15.
        #   target (upper) = 15 * (20/15) = 20.0
        #   invalidation (lower) = 15 * (1 - 2*5/15) = 5.0
        assert entry == pytest.approx(15.0)
        assert upper == pytest.approx(20.0)
        assert lower == pytest.approx(5.0)

    async def test_short_aligned_history_abstains(self, db):
        # Fewer than WINDOW aligned days -> cannot trust the trailing stats.
        e = await _entity(db)
        end = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_window(
            db, e, market_caps=[15.0] * 14 + [25.0] * 15, end_at=end  # 29 days
        )
        horizon = end + timedelta(days=30)

        pid = await produce_protocol_fundamentals_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=AUDIENCE,
            horizon_ends_at=horizon, as_of=end,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_zero_fees_across_the_window_abstains(self, db):
        # No denominator -> no multiple. Refuse, never divide noise by zero.
        e = await _entity(db)
        end = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_window(
            db, e, market_caps=[15.0] * 15 + [25.0] * 15, fees=0.0, end_at=end
        )
        horizon = end + timedelta(days=30)

        pid = await produce_protocol_fundamentals_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=AUDIENCE,
            horizon_ends_at=horizon, as_of=end,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_zero_revenue_across_the_window_abstains(self, db):
        # Fees exist but nothing accrues to the protocol -> the P/F multiple is
        # misleading (all value leaks to LPs). Abstain per the work order.
        e = await _entity(db)
        end = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_window(
            db, e, market_caps=[15.0] * 15 + [25.0] * 15, revenue=0.0, end_at=end
        )
        horizon = end + timedelta(days=30)

        pid = await produce_protocol_fundamentals_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=AUDIENCE,
            horizon_ends_at=horizon, as_of=end,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_flat_multiple_series_abstains(self, db):
        # Every market cap identical -> the multiple never moves -> sigma ~ 0.
        e = await _entity(db)
        end = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_window(db, e, market_caps=[20.0] * WINDOW, end_at=end)
        horizon = end + timedelta(days=30)

        pid = await produce_protocol_fundamentals_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=AUDIENCE,
            horizon_ends_at=horizon, as_of=end,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_no_spread_abstains_when_current_equals_mean(self, db):
        # A window where the current value equals the mean exactly: build a
        # series whose last point lands on the average. [15]*14 + [25]*14 + [20]
        # -> mean 20, current 20 -> no spread -> abstain.
        e = await _entity(db)
        end = datetime(2025, 1, 31, tzinfo=UTC)
        caps = [15.0] * 14 + [25.0] * 14 + [20.0]
        await _seed_window(db, e, market_caps=caps, end_at=end)
        horizon = end + timedelta(days=30)

        pid = await produce_protocol_fundamentals_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=AUDIENCE,
            horizon_ends_at=horizon, as_of=end,
        )
        assert pid is None

    async def test_a_null_market_cap_on_the_current_bar_abstains(self, db):
        # The headline CoinGecko case: a snapshot carries a price but a null
        # market cap (the market_caps array missed that timestamp). That bar
        # must be dropped, NOT forward-filled with a neighbour's cap. With the
        # current bar dropped, only 29 aligned days remain -> abstain.
        e = await _entity(db)
        end = datetime(2025, 1, 31, tzinfo=UTC)
        caps = [15.0] * 15 + [25.0] * 15
        for i, mcap in enumerate(caps):
            d = end - timedelta(days=WINDOW - 1 - i)
            await _fees_claim(db, e, 1.0, d)
            await _revenue_claim(db, e, 10.0, d)
            if i == WINDOW - 1:
                # price present, market cap absent
                await _marketcap_claim(db, e, mcap, None, d, AUDIENCE)
            else:
                await _marketcap_claim(db, e, mcap, mcap, d, AUDIENCE)
        horizon = end + timedelta(days=30)

        pid = await produce_protocol_fundamentals_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=AUDIENCE,
            horizon_ends_at=horizon, as_of=end,
        )
        assert pid is None

    async def test_no_visible_market_cap_abstains(self, db):
        # The no-CoinGecko-key operator: fees and revenue are shared (allowed),
        # but there is no byo price snapshot visible to the shared audience.
        # Without a market cap the multiple cannot be formed. Correct outcome.
        e = await _entity(db)
        end = datetime(2025, 1, 31, tzinfo=UTC)
        for i in range(WINDOW):
            d = end - timedelta(days=WINDOW - 1 - i)
            await _fees_claim(db, e, 1.0, d)
            await _revenue_claim(db, e, 10.0, d)
        horizon = end + timedelta(days=30)

        pid = await produce_protocol_fundamentals_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,  # shared audience only
            horizon_ends_at=horizon, as_of=end,
        )
        assert pid is None

    async def test_method_column_is_fundamentals_protocol(self, db):
        e = await _entity(db)
        end = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_window(db, e, market_caps=[15.0] * 15 + [25.0] * 15, end_at=end)
        horizon = end + timedelta(days=30)

        pid = await produce_protocol_fundamentals_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=AUDIENCE,
            horizon_ends_at=horizon, as_of=end,
        )
        assert pid is not None
        method = await db.pool.fetchval(
            "SELECT method FROM prediction WHERE id=$1", pid
        )
        assert method == "fundamentals.protocol"
