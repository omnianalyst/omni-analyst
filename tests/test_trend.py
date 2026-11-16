"""The trend producer: price/MA -> a falsifiable directional call.

Pure-logic tests cover the barrier/confidence construction (the load-bearing
part); one integration test proves it reads price coverage and records through
the real ledger. Every pure test states what bug it catches.
"""

import json
import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from omni.conviction.trend import (
    _realized_vol,
    produce_trend_prediction_from_coverage,
    trend_call,
)


class TestTrendCall:
    def test_up_call_when_price_above_ma(self):
        # entry 1 vol above sma; k=2 -> upper = entry + 2vol, lower = sma
        out = trend_call(entry=110.0, sma=100.0, vol=5.0, target_k=2.0)
        assert out is not None
        direction, upper, lower, conf = out
        assert direction == "up"
        assert lower == pytest.approx(100.0)  # the MA is the invalidation
        assert upper == pytest.approx(120.0)  # entry + 2*vol
        assert 0.0 < conf < 1.0

    def test_down_call_when_price_below_ma(self):
        out = trend_call(entry=100.0, sma=110.0, vol=5.0, target_k=2.0)
        assert out is not None
        direction, upper, lower, _conf = out
        assert direction == "down"
        assert upper == pytest.approx(110.0)  # MA invalidation, above entry
        assert lower == pytest.approx(90.0)  # entry - 2*vol

    def test_flat_series_abstains(self):
        # vol ~ 0 -> barriers would collapse onto entry. Refuse, don't fabricate.
        assert trend_call(entry=100.0, sma=90.0, vol=0.0) is None
        assert trend_call(entry=100.0, sma=90.0, vol=-1.0) is None

    def test_price_on_the_ma_abstains(self):
        # No trend to call either way.
        assert trend_call(entry=100.0, sma=100.0, vol=5.0) is None

    def test_confidence_is_monotonic_in_trend_strength(self):
        """The discriminator: a call entering further into the trend (more vols
        from the MA) must read higher confidence than one entering near the MA.
        If confidence were constant (e.g. symmetric barriers -> 0.5), the
        conviction gate could not calibrate on it."""
        sma, vol, k = 100.0, 5.0, 2.0
        confs = []
        for d_vol in (0.5, 1.0, 2.0, 4.0, 8.0):  # entry this many vols above MA
            out = trend_call(entry=sma + d_vol * vol, sma=sma, vol=vol, target_k=k)
            assert out is not None
            confs.append(out[3])
        # Strictly increasing with trend strength.
        for a, b in pairwise(confs):
            assert b > a, (confs)
        # And spans a meaningful range (not collapsed to a constant).
        assert confs[-1] - confs[0] > 0.2

    def test_barriers_always_straddle_entry(self):
        sma, vol, k = 100.0, 5.0, 2.0
        for entry in (101.0, 110.0, 150.0, 99.0, 50.0):
            out = trend_call(entry=entry, sma=sma, vol=vol, target_k=k)
            assert out is not None
            _, upper, lower, _ = out
            assert upper > entry > lower


class TestRealizedVol:
    def test_flat_series_is_near_zero(self):
        assert _realized_vol([100.0] * 20) <= 1e-9

    def test_varying_series_is_positive_and_finite(self):
        v = _realized_vol([100.0, 101.0, 99.0, 102.0, 98.0, 103.0])
        assert v > 0.0
        assert math.isfinite(v)


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _price_claim(db, entity_id, close, event_date):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'price_snapshot','close',$2::jsonb,'poly',$3,$3,1.0,'allowed')",
        entity_id, json.dumps({"close": close}), event_date,
    )


class TestProduceFromCoverage:
    async def test_records_an_up_prediction_from_an_uptrend(self, db):
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','X','X') RETURNING id"
        )
        # 50 ascending daily closes -> entry above SMA -> up-call
        base = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(50):
            await _price_claim(db, e, 100.0 + i, base + timedelta(days=i))
        as_of = base + timedelta(days=49)

        pid = await produce_trend_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            as_of=as_of, horizon_ends_at=as_of + timedelta(days=30),
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT method, direction, entry_price, upper_barrier, lower_barrier "
            "FROM prediction WHERE id=$1", pid,
        )
        assert row["method"] == "trend.sma"
        assert row["direction"] == "up"
        assert float(row["lower_barrier"]) < float(row["entry_price"]) < float(row["upper_barrier"])

    async def test_abstains_with_insufficient_history(self, db):
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','X','X') RETURNING id"
        )
        base = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(10):  # window defaults to 50
            await _price_claim(db, e, 100.0 + i, base + timedelta(days=i))
        pid = await produce_trend_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            as_of=base + timedelta(days=9), horizon_ends_at=base + timedelta(days=39),
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0


class TestContinuationGeometry:
    """The measured-geometry fix: the target is the stop distance scaled by
    the method's own realized continuation ratio, closing the loop the payoff
    view opened. These pin the three properties that matter."""

    def test_a_measured_ratio_sets_the_target_from_the_stop_distance(self):
        # stop = 10 (entry 110, MA 100); ratio 0.6 -> target 6 above entry
        out = trend_call(entry=110.0, sma=100.0, vol=5.0, continuation_ratio=0.6)
        assert out is not None
        direction, upper, lower, _ = out
        assert direction == "up"
        assert lower == pytest.approx(100.0)
        assert upper == pytest.approx(116.0)  # 110 + 0.6*10, NOT 110 + 2*5

    def test_down_call_mirrors_the_ratio_geometry(self):
        out = trend_call(entry=90.0, sma=100.0, vol=5.0, continuation_ratio=0.6)
        assert out is not None
        direction, upper, lower, _ = out
        assert direction == "down"
        assert upper == pytest.approx(100.0)
        assert lower == pytest.approx(84.0)  # 90 - 0.6*10

    def test_without_a_ratio_the_conservative_vol_default_holds(self):
        # same inputs, no measured ratio: the old target_k*vol geometry
        out = trend_call(entry=110.0, sma=100.0, vol=5.0)
        assert out is not None
        _, upper, _, _ = out
        assert upper == pytest.approx(120.0)

    def test_the_ratio_actually_changes_the_geometry(self):
        # discrimination: if ratio and target_k produce identical barriers the
        # fix is wired to nothing
        a = trend_call(entry=110.0, sma=100.0, vol=5.0, continuation_ratio=1.5)
        b = trend_call(entry=110.0, sma=100.0, vol=5.0)
        assert a is not None and b is not None
        assert a[1] != pytest.approx(b[1])

    async def test_measured_ratio_refuses_below_the_sample_floor(self, db):
        from omni.conviction.trend import measured_continuation_ratio

        # no resolved calls at all -> None, so the conservative default holds
        assert await measured_continuation_ratio(db.pool) is None


async def _surfaced_call(db, entity_id, *, outcome, resolved_at, exit_price=None):
    """A surfaced, resolved up-call: entry 100, target 110, stop 95.

    Written geometry is 10 / 5 = 2.0 on every one of these, whatever happened.
    """
    pid = await db.pool.fetchval(
        "INSERT INTO prediction (entity_id, method, direction, confidence, "
        "entry_price, upper_barrier, lower_barrier, horizon_ends_at, provenance, "
        "created_at, outcome, resolved_at, exit_price, exit_at) "
        "VALUES ($1,'trend.sma','up',0.33,100,110,95,$2,'{}'::jsonb,$3,"
        "$4::prediction_outcome,$2,$5,$6) RETURNING id",
        entity_id, resolved_at, resolved_at - timedelta(days=5), outcome,
        exit_price, resolved_at if exit_price is not None else None,
    )
    await db.pool.execute(
        "INSERT INTO finding (entity_id, status, method, confidence, threshold, "
        "calibrated_hit_rate, supporting, disconfirming, prediction_id, created_at) "
        "VALUES ($1,'surfaced','trend.sma',0.33,0.3,0.5,$4::jsonb,'[]'::jsonb,$2,$3)",
        entity_id, pid, resolved_at - timedelta(days=5), json.dumps(["fixture"]),
    )


class TestContinuationIsMeasuredNotReadBack:
    """`avg_payoff_pct / avg_risk_pct` is the geometry the calls were WRITTEN
    with -- 2.0 for every fixture here, win or lose. The ratio must come from
    what the calls realized."""

    async def _entity(self, db):
        return await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','X','X') RETURNING id"
        )

    async def test_calls_that_all_stopped_out_have_no_continuation_ratio(self, db):
        from omni.conviction.trend import measured_continuation_ratio

        e = await self._entity(db)
        when = datetime(2025, 3, 1, tzinfo=UTC)
        for i in range(12):
            await _surfaced_call(db, e, outcome="lower", resolved_at=when + timedelta(hours=i))

        # Realized -1R on every call. The written geometry would have said 2.0.
        ratio = await measured_continuation_ratio(
            db.pool, as_of=when + timedelta(days=1)
        )
        assert ratio is None

    async def test_the_ratio_is_the_mean_realized_move_per_unit_risked(self, db):
        from omni.conviction.trend import measured_continuation_ratio

        e = await self._entity(db)
        when = datetime(2025, 3, 1, tzinfo=UTC)
        # 8 targets (+10 / 5 = +2R) and 4 stops (-1R): mean = (16 - 4) / 12 = 1.0
        for i in range(12):
            await _surfaced_call(
                db, e, outcome="upper" if i < 8 else "lower",
                resolved_at=when + timedelta(hours=i),
            )
        ratio = await measured_continuation_ratio(
            db.pool, as_of=when + timedelta(days=1)
        )
        assert ratio == pytest.approx(1.0)

    async def test_the_read_is_point_in_time(self, db):
        """A replay deciding before the calls resolved cannot see their
        outcomes -- the look-ahead a backfill used to inherit from today's
        statistics."""
        from omni.conviction.trend import measured_continuation_ratio

        e = await self._entity(db)
        when = datetime(2025, 3, 1, tzinfo=UTC)
        for i in range(12):
            await _surfaced_call(db, e, outcome="upper", resolved_at=when + timedelta(hours=i))

        assert await measured_continuation_ratio(
            db.pool, as_of=when - timedelta(days=1)
        ) is None
        assert await measured_continuation_ratio(
            db.pool, as_of=when + timedelta(days=1)
        ) == pytest.approx(2.0)

    async def test_a_replay_sets_no_target_from_outcomes_after_its_decision(self, db):
        e = await self._entity(db)
        base = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(50):
            await _price_claim(db, e, 100.0 + i, base + timedelta(days=i))
        as_of = base + timedelta(days=49)
        # Twelve winners that resolve AFTER the replay's decision time.
        for i in range(12):
            await _surfaced_call(
                db, e, outcome="upper", resolved_at=as_of + timedelta(days=30, hours=i)
            )

        pid = await produce_trend_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None, as_of=as_of,
            horizon_ends_at=as_of + timedelta(days=30), created_at=as_of,
        )
        provenance = json.loads(
            await db.pool.fetchval("SELECT provenance FROM prediction WHERE id=$1", pid)
        )
        assert provenance["assumptions"]["continuation_ratio"] is None


class TestTheWindowIsOneVenue:
    async def test_a_second_venues_prints_do_not_interleave_into_the_window(self, db):
        """Two venues, fifty days each. A flat `LIMIT 50` over both is 25 days
        of each interleaved: the SMA spans half the window and the realized vol
        is the venues' basis bouncing between rows."""
        from omni.conviction.trend import _price_window_with_venue

        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('crypto_asset','B','B') RETURNING id"
        )
        base = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(50):
            day = base + timedelta(days=i)
            await _price_claim(db, e, 100.0 + i, day)
            # The other venue prints an hour EARLIER each day, 50 higher.
            await db.pool.execute(
                "INSERT INTO claim (entity_id, claim_type, key, value, source, "
                "event_date, knowledge_date, confidence, redistributable) "
                "VALUES ($1,'price_snapshot','close',$2::jsonb,'other',$3,$3,1.0,'allowed')",
                e, json.dumps({"close": 150.0 + i, "venue": "other"}),
                day - timedelta(hours=1),
            )

        closes, venue = await _price_window_with_venue(
            db.pool, entity_id=e, audience=None,
            as_of=base + timedelta(days=49), limit=50,
        )
        assert venue == "poly"  # the venue of the newest print
        assert closes == [100.0 + i for i in range(50)]

    async def test_a_restated_bar_counts_once_at_its_latest_knowable_value(self, db):
        from omni.conviction.trend import _price_window_with_venue

        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','R','R') RETURNING id"
        )
        base = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(5):
            await _price_claim(db, e, 100.0 + i, base + timedelta(days=i))
        # Day 2 restated, knowable a day later.
        await db.pool.execute(
            "INSERT INTO claim (entity_id, claim_type, key, value, source, "
            "event_date, knowledge_date, confidence, redistributable) "
            "VALUES ($1,'price_snapshot','close',$2::jsonb,'poly',$3,$4,1.0,'allowed')",
            e, json.dumps({"close": 500.0}), base + timedelta(days=2),
            base + timedelta(days=3),
        )
        closes, _ = await _price_window_with_venue(
            db.pool, entity_id=e, audience=None, as_of=base + timedelta(days=4), limit=10,
        )
        assert closes == [100.0, 101.0, 500.0, 103.0, 104.0]

    async def test_the_call_records_the_venue_it_was_priced_on(self, db):
        e = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','X','X') RETURNING id"
        )
        base = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(50):
            await _price_claim(db, e, 100.0 + i, base + timedelta(days=i))
        as_of = base + timedelta(days=49)
        pid = await produce_trend_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            as_of=as_of, horizon_ends_at=as_of + timedelta(days=30),
        )
        provenance = json.loads(
            await db.pool.fetchval("SELECT provenance FROM prediction WHERE id=$1", pid)
        )
        assert provenance["assumptions"]["price_venue"] == "poly"
