"""A replayed prediction must be readable as replayed, from the row alone.

`trading/policy.py` opens GATE C on `live_resolved_n` -- resolved predictions
whose provenance carries no backfill marker. The gate is only real if the
backfill actually writes that marker, and only if the writer and the reader
agree about where it lives. So the predicate policy reads with is copied here
verbatim and run against one row from each path: if either side moves, these
tests fail rather than the gate silently opening on replayed history.

The other half of the contract is that the marker must not cost the backfill
its evidential value: a replayed outcome is a real outcome, it solves the
cold-start problem, and it must keep feeding calibration.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from omni.autonomous.backfill import backfill_trend_predictions
from omni.conviction.trend import produce_trend_prediction_from_coverage
from omni.trading import policy

# Copied verbatim from policy._NOT_BACKFILLED, deliberately NOT imported. The
# property under test is that two modules agree; a test that imported the
# predicate would follow policy.py wherever it drifted to and prove nothing.
NOT_BACKFILLED = (
    "NOT (p.provenance ? 'backfill' "
    "OR COALESCE(p.provenance -> 'assumptions' ? 'backfill', false))"
)

WINDOW = 10
LOOKBACK_DAYS = 40
INTERVAL_DAYS = 10
HORIZON_DAYS = 5
# ts steps cutoff, +10, +20, +30 while ts < now - horizon.
EXPECTED_STEPS = 4


async def _entity(db):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company',$1,$1) RETURNING id",
        uuid4().hex[:12],
    )


async def _prices(db, entity_id, *, days=140):
    """A drifting zig-zag: enough history for every replay step, non-zero vol.

    A straight ramp has near-zero stdev, which the producer treats as a flat
    series and abstains on -- the fixture would then prove nothing about the
    marker because no prediction would exist.
    """
    start = datetime.now(UTC) - timedelta(days=days)
    for i in range(days):
        close = 100.0 + i * 0.5 + (1.5 if i % 2 else -1.5)
        await db.pool.execute(
            "INSERT INTO claim (entity_id, claim_type, key, value, source, "
            "event_date, knowledge_date, confidence, redistributable) "
            "VALUES ($1,'price_snapshot','close',$2::jsonb,'test',$3,$3,1.0,'allowed')",
            entity_id,
            json.dumps({"close": close}),
            start + timedelta(days=i),
        )


async def _backfill(db, entity_id):
    """Run the real backfill over one entity, under a method name of its own."""
    method = "trend.sma.t" + uuid4().hex[:8]
    report = await backfill_trend_predictions(
        db.pool,
        lookback_days=LOOKBACK_DAYS,
        interval_days=INTERVAL_DAYS,
        horizon_days=HORIZON_DAYS,
        window=WINDOW,
        method_suffix=method.removeprefix("trend.sma"),
        entity_ids=[entity_id],
    )
    return method, report


async def _live_prediction(db, entity_id):
    """A prediction from the normal producer path, the way the scheduler calls it."""
    now = datetime.now(UTC)
    return await produce_trend_prediction_from_coverage(
        db.pool,
        entity_id=entity_id,
        audience_user_id=None,
        as_of=now,
        horizon_ends_at=now + timedelta(days=HORIZON_DAYS),
        window=WINDOW,
    )


async def _provenance(db, prediction_id):
    raw = await db.pool.fetchval(
        "SELECT provenance FROM prediction WHERE id = $1", prediction_id
    )
    return json.loads(raw) if isinstance(raw, (str, bytes)) else raw


async def _ids(db, method):
    rows = await db.pool.fetch(
        "SELECT id FROM prediction WHERE method = $1 ORDER BY created_at", method
    )
    return [r["id"] for r in rows]


class TestBackfillStamp:
    async def test_every_backfilled_prediction_carries_the_marker(self, db):
        """Read back from Postgres, not from the return value: the marker is a
        property of the stored row, which is the only thing policy.py sees."""
        entity_id = await _entity(db)
        await _prices(db, entity_id)
        method, report = await _backfill(db, entity_id)

        assert report.predictions_written == EXPECTED_STEPS
        ids = await _ids(db, method)
        assert len(ids) == report.predictions_written

        run_ids = set()
        for pid in ids:
            marker = (await _provenance(db, pid))["assumptions"]["backfill"]
            UUID(marker["run_id"])
            assert datetime.fromisoformat(marker["cutoff"]) < datetime.now(UTC)
            datetime.fromisoformat(marker["as_of"])
            run_ids.add(marker["run_id"])

        assert len(run_ids) == 1, "one backfill run, one run identifier"

    async def test_marker_records_the_replayed_decision_time(self, db):
        """`as_of` must be the historical timestamp the producer was replayed at,
        not the wall clock the UPDATE ran at -- otherwise the marker cannot say
        which point in history the call was manufactured from."""
        entity_id = await _entity(db)
        await _prices(db, entity_id)
        method, _ = await _backfill(db, entity_id)

        rows = await db.pool.fetch(
            "SELECT created_at, provenance FROM prediction WHERE method = $1", method
        )
        for row in rows:
            raw = row["provenance"]
            prov = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            stamped = datetime.fromisoformat(prov["assumptions"]["backfill"]["as_of"])
            assert stamped == row["created_at"]

    async def test_marker_survives_jsonb_as_an_object(self, db):
        """A double-encoded marker would land as a JSON string. `?` on a string
        is false, so the round trip is the difference between a live gate and an
        open one -- and the producer's own assumptions must survive the merge."""
        entity_id = await _entity(db)
        await _prices(db, entity_id)
        method, _ = await _backfill(db, entity_id)
        pid = (await _ids(db, method))[0]

        kind, run_id, model, window = await db.pool.fetchrow(
            """
            SELECT jsonb_typeof(provenance -> 'assumptions' -> 'backfill'),
                   provenance -> 'assumptions' -> 'backfill' ->> 'run_id',
                   provenance -> 'assumptions' ->> 'model',
                   (provenance -> 'assumptions' ->> 'window')::int
            FROM prediction WHERE id = $1
            """,
            pid,
        )
        assert kind == "object"
        UUID(run_id)
        assert model == "trend_sma"
        assert window == WINDOW

        marker = (await _provenance(db, pid))["assumptions"]["backfill"]
        assert isinstance(marker, dict)


class TestLiveProducerPath:
    async def test_producer_path_carries_no_marker(self, db):
        """The same producer, called the way the scheduler calls it, must leave
        no marker anywhere in the envelope."""
        entity_id = await _entity(db)
        await _prices(db, entity_id)

        pid = await _live_prediction(db, entity_id)
        assert pid is not None, "fixture must produce a live call to test"

        prov = await _provenance(db, pid)
        assert "backfill" not in prov
        assert "backfill" not in prov["assumptions"]
        assert prov["assumptions"]["model"] == "trend_sma"


class TestPolicyPredicateAgreement:
    async def test_the_predicate_policy_reads_with_classifies_both_paths(self, db):
        """The headline. policy.py's own SQL, run against one row from each
        writer: the live one counts, the replayed ones do not."""
        assert NOT_BACKFILLED == policy._NOT_BACKFILLED, (
            "policy._NOT_BACKFILLED moved; the backfill's stamp must move with it"
        )

        entity_id = await _entity(db)
        await _prices(db, entity_id)
        live_id = await _live_prediction(db, entity_id)
        method, _ = await _backfill(db, entity_id)
        backfilled = await _ids(db, method)

        assert live_id is not None
        assert len(backfilled) == EXPECTED_STEPS

        rows = await db.pool.fetch(
            f"SELECT p.id FROM prediction p "
            f"WHERE p.id = ANY($1::uuid[]) AND {NOT_BACKFILLED}",
            [live_id, *backfilled],
        )
        counted_live = {r["id"] for r in rows}
        assert counted_live == {live_id}
        assert not counted_live & set(backfilled)

    async def test_live_count_is_the_number_the_scale_gate_reads(self, db):
        """The predicate as policy.py actually aggregates it: a FILTER over the
        method's rows. Thirty backfilled predictions must count zero toward the
        live total."""
        entity_id = await _entity(db)
        await _prices(db, entity_id)
        method, report = await _backfill(db, entity_id)

        total, live_n = await db.pool.fetchrow(
            f"SELECT count(*), count(*) FILTER (WHERE {NOT_BACKFILLED}) "
            f"FROM prediction p WHERE p.method = $1",
            method,
        )
        assert total == report.predictions_written
        assert live_n == 0


class TestCalibrationStillCounts:
    async def test_backfilled_predictions_remain_in_the_calibration_bucket(self, db):
        """The marker withholds capital, not evidence. A backfilled outcome is a
        real outcome; excluding it from calibration would reintroduce the
        cold-start problem the backfill exists to solve."""
        entity_id = await _entity(db)
        await _prices(db, entity_id)
        method, _ = await _backfill(db, entity_id)

        resolved = await db.pool.fetchval(
            "SELECT count(*) FROM prediction "
            "WHERE method = $1 AND outcome <> 'pending'",
            method,
        )
        assert resolved > 0, "fixture must resolve something for calibration to see"

        bucketed = await db.pool.fetchval(
            "SELECT COALESCE(sum(n), 0) FROM calibration_bucket "
            "WHERE method = $1 AND audience_user_id IS NULL",
            method,
        )
        assert bucketed == resolved


async def test_a_completed_backfill_is_skipped_when_data_begins_at_the_cutoff(
    db,
):
    """The Polygon-cap case: 730-day lookback against 2 years of data.

    Data begins at the cutoff, so the first producible prediction sits a
    window's worth of sessions AFTER it -- `oldest <= cutoff` was permanently
    false and every scheduler restart re-walked every entity (41k duplicate
    predictions per boot, measured 2026-08-20). With the window margin, an
    entity whose history reaches back to the earliest producible timestamp
    is skipped.
    """
    from datetime import UTC, datetime, timedelta

    entity_id = await _entity(db)
    method = "trend.sma.m" + uuid4().hex[:8]
    cutoff = datetime.now(UTC) - timedelta(days=730)
    # Data begins at the cutoff (Polygon 2y cap): first producible timestamp
    # is window=20 sessions (~28 calendar days) later. Seed the marker's
    # oldest-prediction read directly -- the producer itself would abstain
    # without price history, and the claim under test is the skip arithmetic.
    for offset in (28, 35, 42, 49):
        await db.pool.execute(
            "INSERT INTO prediction (entity_id, method, direction, confidence, "
            "entry_price, upper_barrier, lower_barrier, horizon_ends_at, "
            "provenance, created_at) "
            "VALUES ($1,$2,'up',0.6,100.0,110.0,90.0,$3,'{}'::jsonb,$4)",
            entity_id, method,
            cutoff + timedelta(days=offset + 7),
            cutoff + timedelta(days=offset),
        )

    report = await backfill_trend_predictions(
        db.pool, lookback_days=730, method_suffix=method.removeprefix("trend.sma"),
        entity_ids=[entity_id],
    )

    assert report.entities_skipped == 1
    assert report.predictions_written == 0
