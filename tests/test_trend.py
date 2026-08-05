"""The trend producer: price/MA -> a falsifiable directional call.

Pure-logic tests cover the barrier/confidence construction (the load-bearing
part); one integration test proves it reads price coverage and records through
the real ledger. Every pure test states what bug it catches.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

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
        direction, upper, lower, conf = out
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
        for a, b in zip(confs, confs[1:]):
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
        assert v > 0.0 and v == v  # finite


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
