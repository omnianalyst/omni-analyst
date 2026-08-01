"""The prediction ledger: writing directional calls and resolving them.

These are the tests D15 exists to produce. Before this module, `INSERT INTO
prediction` appeared only in tests: nothing in `src/` wrote a prediction and
nothing resolved one, so `calibration_bucket` was empty forever and `assess()`
returned `UNCALIBRATED` for every candidate. The end-to-end calibration test
below is the proof the conviction gate can open at all.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from omni.conviction.gate import (
    MIN_RESOLVED_FOR_CALIBRATION,
    Candidate,
    Refusal,
    assess,
)
from omni.conviction.ledger import (
    NonDirectionalResult,
    record_prediction,
    resolve_due_predictions,
)
from omni.conviction.publish import load_calibration

NOW = datetime.now(UTC)


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company',$1,$1) RETURNING id",
        symbol,
    )


async def _price_claim(
    db, entity_id, price, event_date, *, high=None, low=None, owner=None
):
    shared = owner is None
    value = {"price": price}
    if high is not None:
        value["high"] = high
    if low is not None:
        value["low"] = low
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1,'price_snapshot','seed',$2::jsonb,$3,$4,$5,1.0,$6,$7)
        RETURNING id
        """,
        entity_id,
        json.dumps(value),
        "seed" if shared else "polygon",
        event_date,
        event_date,
        "allowed" if shared else "byo_only",
        owner,
    )


async def _seed_prediction(
    db,
    entity_id,
    *,
    direction="up",
    entry=100.0,
    upper=110.0,
    lower=90.0,
    confidence=0.8,
    method="fundamentals.dcf_valuation",
    created_at,
    horizon_ends_at,
    claim_id=None,
    provenance=None,
):
    return await db.pool.fetchval(
        """
        INSERT INTO prediction (entity_id, claim_id, method, direction, confidence,
                                entry_price, upper_barrier, lower_barrier,
                                horizon_ends_at, provenance, created_at)
        VALUES ($1,$2,$3,$4::prediction_direction,$5,$6,$7,$8,$9,$10::jsonb,$11)
        RETURNING id
        """,
        entity_id,
        claim_id,
        method,
        direction,
        confidence,
        entry,
        upper,
        lower,
        horizon_ends_at,
        json.dumps(provenance or {}),
        created_at,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestRecordPrediction:
    async def test_a_directional_result_records_barriers_and_provenance(self, db):
        e = await _entity(db)
        c1 = await _price_claim(db, e, 100.0, NOW - timedelta(days=3))
        c2 = await _price_claim(db, e, 101.0, NOW - timedelta(days=2))

        pid = await record_prediction(
            db.pool,
            entity_id=e,
            capability="fundamentals.dcf_valuation",
            direction="up",
            confidence=0.82,
            entry_price=100.0,
            upper_barrier=115.0,
            lower_barrier=92.0,
            horizon_ends_at=NOW + timedelta(days=30),
            input_claim_ids=(str(c1), str(c2)),
            assumptions={
                "growth_rate": 0.12,
                "terminal_growth_rate": 0.03,
                "discount_rate": 0.09,
            },
        )

        row = await db.pool.fetchrow(
            "SELECT method, direction, entry_price, upper_barrier, lower_barrier, "
            "outcome, resolved_at, provenance FROM prediction WHERE id=$1",
            pid,
        )
        # method defaults to the capability -- the calibration grouping grain is
        # the analysis itself (decided and justified in the report).
        assert row["method"] == "fundamentals.dcf_valuation"
        assert row["direction"] == "up"
        assert float(row["entry_price"]) == pytest.approx(100.0)
        assert float(row["upper_barrier"]) == pytest.approx(115.0)
        assert float(row["lower_barrier"]) == pytest.approx(92.0)
        assert row["outcome"] == "pending"
        assert row["resolved_at"] is None
        prov = row["provenance"]
        if isinstance(prov, str):
            prov = json.loads(prov)
        assert prov["capability"] == "fundamentals.dcf_valuation"
        assert prov["input_claims"] == [str(c1), str(c2)]
        assert prov["assumptions"]["growth_rate"] == pytest.approx(0.12)
        assert prov["assumptions"]["discount_rate"] == pytest.approx(0.09)

    async def test_a_non_directional_result_is_refused_and_writes_nothing(self, db):
        """The fabrication guard. A result that asserts no price has no
        entry/upper/lower to give; record_prediction refuses it rather than
        manufacturing a barrier that would satisfy the schema and score
        nothing. No row is written."""
        e = await _entity(db)
        before = await db.pool.fetchval("SELECT count(*) FROM prediction")

        with pytest.raises(NonDirectionalResult):
            await record_prediction(
                db.pool,
                entity_id=e,
                capability="fundamentals.financial_ratios",
                direction="up",
                confidence=0.5,
                entry_price=None,
                upper_barrier=None,
                lower_barrier=None,
                horizon_ends_at=NOW + timedelta(days=5),
            )

        after = await db.pool.fetchval("SELECT count(*) FROM prediction")
        assert after == before

    async def test_non_straddling_barriers_are_refused_before_write(self, db):
        e = await _entity(db)
        with pytest.raises(ValueError):
            await record_prediction(
                db.pool,
                entity_id=e,
                capability="x",
                direction="up",
                confidence=0.5,
                entry_price=100.0,
                upper_barrier=95.0,  # above entry violated
                lower_barrier=90.0,
                horizon_ends_at=NOW + timedelta(days=5),
            )
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0


class TestResolution:
    async def test_upper_crossed_before_horizon_resolves_upper(self, db):
        e = await _entity(db)
        cross = NOW - timedelta(days=5)
        pid = await _seed_prediction(
            db, e, entry=100.0, upper=110.0, lower=90.0,
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW - timedelta(days=1),
        )
        await _price_claim(db, e, 112.0, cross)

        n = await resolve_due_predictions(db.pool)
        assert n == 1
        row = await db.pool.fetchrow(
            "SELECT outcome, resolved_at, horizon_ends_at FROM prediction WHERE id=$1",
            pid,
        )
        assert row["outcome"] == "upper"
        assert row["resolved_at"] is not None
        # resolved_at is the point in time the barrier was crossed, not when the
        # resolver happened to run, and it falls within the window.
        assert row["resolved_at"].date() == cross.date()
        assert row["resolved_at"] <= row["horizon_ends_at"]

    async def test_lower_crossed_before_horizon_resolves_lower(self, db):
        e = await _entity(db)
        pid = await _seed_prediction(
            db, e, direction="down", entry=100.0, upper=110.0, lower=90.0,
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW - timedelta(days=1),
        )
        await _price_claim(db, e, 88.0, NOW - timedelta(days=4))

        await resolve_due_predictions(db.pool)
        outcome = await db.pool.fetchval(
            "SELECT outcome FROM prediction WHERE id=$1", pid
        )
        assert outcome == "lower"

    async def test_horizon_passed_untouched_resolves_expiry(self, db):
        e = await _entity(db)
        horizon = NOW - timedelta(days=1)
        pid = await _seed_prediction(
            db, e, entry=100.0, upper=110.0, lower=90.0,
            created_at=NOW - timedelta(days=10), horizon_ends_at=horizon,
        )
        # Price stays strictly within the barriers across the whole window.
        await _price_claim(db, e, 100.0, NOW - timedelta(days=8))
        await _price_claim(db, e, 105.0, NOW - timedelta(days=4))

        await resolve_due_predictions(db.pool)
        row = await db.pool.fetchrow(
            "SELECT outcome, resolved_at, horizon_ends_at FROM prediction WHERE id=$1",
            pid,
        )
        assert row["outcome"] == "expiry"
        # resolved_at is when the horizon elapsed.
        assert row["resolved_at"] == row["horizon_ends_at"]

    async def test_horizon_not_yet_passed_stays_pending(self, db):
        """A prediction whose horizon has not elapsed is never swept to expiry,
        and resolved_at stays NULL -- the resolver only touches predictions the
        prediction_due index returns, i.e. those whose horizon has passed."""
        e = await _entity(db)
        pid = await _seed_prediction(
            db, e, entry=100.0, upper=110.0, lower=90.0,
            created_at=NOW - timedelta(days=1),
            horizon_ends_at=NOW + timedelta(days=5),
        )
        await _price_claim(db, e, 102.0, NOW)  # within barriers, untouched

        n = await resolve_due_predictions(db.pool)
        assert n == 0
        row = await db.pool.fetchrow(
            "SELECT outcome, resolved_at FROM prediction WHERE id=$1", pid
        )
        assert row["outcome"] == "pending"
        assert row["resolved_at"] is None

    async def test_a_prediction_with_no_visible_price_stays_pending(self, db):
        """No price path -> no fabrication. The resolver leaves the prediction
        pending rather than guessing an outcome from nothing."""
        e = await _entity(db)
        pid = await _seed_prediction(
            db, e, entry=100.0, upper=110.0, lower=90.0,
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW - timedelta(days=1),
        )
        n = await resolve_due_predictions(db.pool)
        assert n == 0
        row = await db.pool.fetchrow(
            "SELECT outcome, resolved_at FROM prediction WHERE id=$1", pid
        )
        assert row["outcome"] == "pending"
        assert row["resolved_at"] is None


class TestBothBarriersCrossed:
    async def test_the_first_observed_crossing_wins(self, db):
        """When both barriers are crossed, the one whose crossing is observed
        first in event_date order wins. Price snapshots are discrete, so this
        is the finest ordering the granularity supports -- a time order, not
        'whichever the code checks first'."""
        e = await _entity(db)
        pid = await _seed_prediction(
            db, e, direction="up", entry=100.0, upper=110.0, lower=90.0,
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW - timedelta(days=1),
        )
        await _price_claim(db, e, 111.0, NOW - timedelta(days=6))  # upper first
        await _price_claim(db, e, 89.0, NOW - timedelta(days=3))   # lower later

        await resolve_due_predictions(db.pool)
        outcome = await db.pool.fetchval(
            "SELECT outcome FROM prediction WHERE id=$1", pid
        )
        assert outcome == "upper"

    async def test_a_single_observation_spanning_both_is_a_conservative_miss(self, db):
        """When one observation's range touches both barriers (a Polygon bar
        whose high >= upper and low <= lower) the intra-bar sequence is
        genuinely unknowable. The conservative resolution is applied: the
        outcome that counts as a miss for the direction, never a gift hit."""
        e = await _entity(db)
        pid = await _seed_prediction(
            db, e, direction="up", entry=100.0, upper=110.0, lower=90.0,
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW - timedelta(days=1),
        )
        await _price_claim(
            db, e, 100.0, NOW - timedelta(days=5), high=115.0, low=85.0
        )

        await resolve_due_predictions(db.pool)
        outcome = await db.pool.fetchval(
            "SELECT outcome FROM prediction WHERE id=$1", pid
        )
        # 'lower' is the miss for an 'up' prediction.
        assert outcome == "lower"


class TestCalibrationEndToEnd:
    async def test_resolved_predictions_open_the_conviction_gate(self, db):
        """The proof the order exists to produce: enough resolved predictions
        of one method cross MIN_RESOLVED_FOR_CALIBRATION, calibration_bucket
        reports them, and assess() on a candidate of that method no longer
        returns UNCALIBRATED. Against the real assess(), not a reimplementation."""
        e = await _entity(db)
        method = "fundamentals.dcf_valuation"
        created = NOW - timedelta(days=20)
        horizon = NOW - timedelta(days=1)
        for _ in range(MIN_RESOLVED_FOR_CALIBRATION + 2):
            await _seed_prediction(
                db, e, direction="up", entry=100.0, upper=110.0, lower=90.0,
                confidence=0.82, method=method,
                created_at=created, horizon_ends_at=horizon,
            )
        # One shared price path crossing upper serves every prediction's window.
        await _price_claim(db, e, 111.0, NOW - timedelta(days=5))

        n = await resolve_due_predictions(db.pool)
        assert n == MIN_RESOLVED_FOR_CALIBRATION + 2

        buckets = await load_calibration(
            db.pool, claim_type="fundamental_metric", method=method
        )
        assert sum(b.n for b in buckets) == MIN_RESOLVED_FOR_CALIBRATION + 2
        calibrated = [b for b in buckets if b.hit_rate is not None]
        assert calibrated
        assert calibrated[0].n >= MIN_RESOLVED_FOR_CALIBRATION
        assert calibrated[0].hits == calibrated[0].n  # every up resolved upper

        candidate = Candidate(
            claim_type="fundamental_metric",
            method=method,
            confidence=0.85,
            searched_for_disconfirming=True,
            falsifiable=True,
        )
        verdict = assess(candidate, buckets)
        assert verdict.refusal is not Refusal.UNCALIBRATED
        assert verdict.surfaced
        assert verdict.calibrated_hit_rate == pytest.approx(1.0)


class TestConcurrentResolution:
    async def test_two_resolvers_never_double_resolve_the_same_prediction(self, db):
        """The guarantee: two workers reaching the same prediction do not both
        write. _resolve_one locks the row FOR UPDATE SKIP LOCKED inside its
        transaction, so a resolver that finds the row already locked skips it
        rather than blocking or double-writing.

        Proven deterministically by holding the lock open: while another
        transaction holds it, the resolver resolves nothing and does not block;
        once released, it resolves the one prediction. A plain FOR UPDATE here
        would block on the held lock and time out -- SKIP LOCKED is exactly what
        makes the skip immediate, so this test discriminates the mechanism."""
        e = await _entity(db)
        pid = await _seed_prediction(
            db, e, direction="up", entry=100.0, upper=110.0, lower=90.0,
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW - timedelta(days=1),
        )
        await _price_claim(db, e, 111.0, NOW - timedelta(days=5))

        # Hold an open transaction that has locked the prediction row, exactly as
        # another worker would mid-resolution.
        async with db.pool.acquire() as holder, holder.transaction():
            await holder.execute(
                "SELECT id FROM prediction "
                "WHERE id=$1 AND outcome='pending' FOR UPDATE",
                pid,
            )
            # While the lock is held the resolver must skip it -- not block, not
            # write. A 5s ceiling catches a plain FOR UPDATE that would hang.
            n = await asyncio.wait_for(resolve_due_predictions(db.pool), timeout=5)
            assert n == 0
            outcome = await db.pool.fetchval(
                "SELECT outcome FROM prediction WHERE id=$1", pid
            )
            assert outcome == "pending"

        # Lock released; the prediction now resolves exactly once.
        n = await resolve_due_predictions(db.pool)
        assert n == 1
        outcome = await db.pool.fetchval(
            "SELECT outcome FROM prediction WHERE id=$1", pid
        )
        assert outcome == "upper"
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 1


class TestSchedulerResolveLoop:
    async def test_the_resolve_loop_closes_predictions_unattended(self, db):
        """The third loop is wired: a prediction created after start() (so the
        initial resolve in start() found nothing) is closed by the loop."""
        from omni.scheduler.worker import Scheduler, SchedulerConfig, default_registry

        e = await _entity(db)
        scheduler = Scheduler(
            db.pool,
            default_registry(),
            SchedulerConfig(
                resolve_interval=0.05, sweep_interval=999,
                fill_interval=999, fill_workers=0,
            ),
        )
        await scheduler.start()
        outcome = "pending"
        try:
            pid = await _seed_prediction(
                db, e, entry=100.0, upper=110.0, lower=90.0,
                created_at=NOW - timedelta(days=10),
                horizon_ends_at=NOW - timedelta(days=1),
            )
            await _price_claim(db, e, 111.0, NOW - timedelta(days=5))

            loop = asyncio.get_event_loop()
            deadline = loop.time() + 30
            while loop.time() < deadline:
                outcome = await db.pool.fetchval(
                    "SELECT outcome FROM prediction WHERE id=$1", pid
                )
                if outcome != "pending":
                    break
                await asyncio.sleep(0.05)
        finally:
            await scheduler.stop()

        assert outcome == "upper"
        assert scheduler.stats.resolved >= 1
