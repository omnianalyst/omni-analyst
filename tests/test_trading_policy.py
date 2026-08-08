"""The gate between a calibrated method and capital."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.conviction.gate import MIN_RESOLVED_FOR_CALIBRATION
from omni.trading.policy import (
    MIN_LIVE_RESOLVED_FOR_SCALE,
    MIN_RESOLVED_FOR_PAPER,
    Ineligible,
    TradingPhase,
    eligible,
)

NOW = datetime(2026, 8, 7, tzinfo=UTC)


def _method() -> str:
    # Every count in this module is filtered by method, so a per-test method
    # name isolates a test from every other row in the shared test database
    # without truncating tables other agents' suites are using.
    return f"policy.test.{uuid4().hex[:12]}"


async def _entity(db, kind="company"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ($1,$2,$2) RETURNING id",
        kind,
        uuid4().hex[:12],
    )


async def _resolved(
    db,
    entity_id,
    *,
    method,
    hit=True,
    confidence=0.85,
    audience=None,
    backfilled=False,
    pending=False,
):
    provenance = {"capability": method, "input_claims": [], "assumptions": {}}
    if backfilled:
        provenance["assumptions"]["backfill"] = True
    pid = await db.pool.fetchval(
        """
        INSERT INTO prediction (entity_id, method, direction, confidence,
                                entry_price, upper_barrier, lower_barrier,
                                horizon_ends_at, provenance, audience_user_id)
        VALUES ($1,$2,'up',$3,100,110,90,$4,$5::jsonb,$6) RETURNING id
        """,
        entity_id,
        method,
        confidence,
        NOW + timedelta(days=5),
        json.dumps(provenance),
        audience,
    )
    if not pending:
        await db.pool.execute(
            "UPDATE prediction SET outcome=$1::prediction_outcome, resolved_at=now() "
            "WHERE id=$2",
            "upper" if hit else "lower",
            pid,
        )
    return pid


async def _record(db, entity_id, *, method, n, hits=None, **kw):
    hits = n if hits is None else hits
    for i in range(n):
        await _resolved(db, entity_id, method=method, hit=i < hits, **kw)


class TestCalibrationFloor:
    async def test_below_the_bucket_floor_the_hit_rate_is_unknown_not_zero(self, db):
        method, entity = _method(), await _entity(db)
        await _record(db, entity, method=method, n=MIN_RESOLVED_FOR_CALIBRATION - 1)

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
        )

        assert verdict.eligible is False
        assert verdict.reason is Ineligible.UNCALIBRATED
        assert verdict.hit_rate is None
        assert verdict.resolved_n == MIN_RESOLVED_FOR_CALIBRATION - 1

    async def test_a_bucket_below_the_floor_does_not_move_the_pooled_hit_rate(self, db):
        method, entity = _method(), await _entity(db)
        # 20 hits in the 0.8-0.9 bucket (above the floor), 5 misses in the
        # 0.1-0.2 bucket (below it). Pooling all 25 would report 0.80.
        await _record(db, entity, method=method, n=20, confidence=0.85)
        await _record(db, entity, method=method, n=5, hits=0, confidence=0.15)

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
        )

        assert verdict.hit_rate == pytest.approx(1.0)
        assert verdict.resolved_n == 25

    async def test_pending_predictions_are_not_a_record(self, db):
        method, entity = _method(), await _entity(db)
        await _record(db, entity, method=method, n=40, pending=True)
        await _record(db, entity, method=method, n=5)

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
        )

        assert verdict.resolved_n == 5
        assert verdict.reason is Ineligible.UNCALIBRATED


class TestResolvedCountForPaper:
    async def test_twenty_nine_resolved_is_ineligible_and_thirty_is_eligible(self, db):
        method, entity = _method(), await _entity(db)
        await _record(db, entity, method=method, n=MIN_RESOLVED_FOR_PAPER - 1)

        short = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
        )
        assert short.eligible is False
        assert short.reason is Ineligible.INSUFFICIENT_RESOLVED
        assert short.resolved_n == MIN_RESOLVED_FOR_PAPER - 1

        await _record(db, entity, method=method, n=1)

        now_enough = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
        )
        assert now_enough.eligible is True
        assert now_enough.reason is None
        assert now_enough.resolved_n == MIN_RESOLVED_FOR_PAPER
        assert now_enough.hit_rate == pytest.approx(1.0)

    async def test_hit_rate_below_target_refuses_and_at_target_passes(self, db):
        method, entity = _method(), await _entity(db)
        await _record(db, entity, method=method, n=30, hits=15)

        refused = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
        )
        assert refused.eligible is False
        assert refused.reason is Ineligible.BELOW_HIT_RATE
        assert refused.hit_rate == pytest.approx(0.5)

        at_target = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.5,
            walk_forward_positive=True,
        )
        assert at_target.eligible is True


class TestWalkForward:
    async def test_paper_requires_a_walk_forward_too(self, db):
        """GATE A admits a strategy to paper only once it has held out of sample.

        This previously asserted the opposite, and the implementation obliged by
        gating the check behind MICRO with a permissive default. The reason it
        has to bind at PAPER: an in-sample hit rate is fitted to the outcomes it
        is judged on, so a paper record built on one inherits the overfit rather
        than testing it -- and GATE B then reads that paper record.
        """
        method, entity = _method(), await _entity(db)
        await _record(db, entity, method=method, n=30)

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=None,
        )
        assert verdict.eligible is False
        assert verdict.reason is Ineligible.NO_WALK_FORWARD

    async def test_the_argument_cannot_be_omitted(self):
        """No default may supply the permissive value.

        A caller that never wired walk-forward must fail loudly at the call
        site rather than receive an eligibility that assumed the check passed.
        """
        with pytest.raises(TypeError, match="walk_forward_positive"):
            await eligible(
                object(),
                method="trend.sma",
                entity_kind="company",
                audience_user_id=None,
                phase=TradingPhase.PAPER,
                target_hit_rate=0.6,
            )

    async def test_micro_treats_never_run_as_a_refusal_not_as_a_pass(self, db):
        method, entity = _method(), await _entity(db)
        await _record(db, entity, method=method, n=30)

        kw = {
            "method": method,
            "entity_kind": "company",
            "audience_user_id": None,
            "phase": TradingPhase.MICRO,
            "target_hit_rate": 0.6,
        }

        never_run = await eligible(db.pool, walk_forward_positive=None, **kw)
        assert never_run.eligible is False
        assert never_run.reason is Ineligible.NO_WALK_FORWARD

        failed = await eligible(db.pool, walk_forward_positive=False, **kw)
        assert failed.eligible is False
        assert failed.reason is Ineligible.NEGATIVE_EXPECTANCY

        held = await eligible(db.pool, walk_forward_positive=True, **kw)
        assert held.eligible is True
        assert held.reason is None


class TestBackfillDoesNotBuyLiveHistory:
    async def test_backfill_calibrates_but_does_not_open_the_scale_phase(self, db):
        method, entity = _method(), await _entity(db)
        await _record(db, entity, method=method, n=35, backfilled=True)
        await _record(db, entity, method=method, n=5)

        kw = {
            "method": method,
            "entity_kind": "company",
            "audience_user_id": None,
            "target_hit_rate": 0.6,
            "walk_forward_positive": True,
        }

        paper = await eligible(db.pool, phase=TradingPhase.PAPER, **kw)
        assert paper.eligible is True
        assert paper.resolved_n == 40
        assert paper.live_resolved_n == 5
        assert paper.hit_rate == pytest.approx(1.0)

        scale = await eligible(db.pool, phase=TradingPhase.SCALE, **kw)
        assert scale.eligible is False
        assert scale.reason is Ineligible.BACKFILL_ONLY
        assert scale.live_resolved_n == 5

        await _record(db, entity, method=method, n=MIN_LIVE_RESOLVED_FOR_SCALE - 5)

        scaled = await eligible(db.pool, phase=TradingPhase.SCALE, **kw)
        assert scaled.eligible is True
        assert scaled.live_resolved_n == MIN_LIVE_RESOLVED_FOR_SCALE
        assert scaled.resolved_n == 35 + MIN_LIVE_RESOLVED_FOR_SCALE

    async def test_a_prediction_without_an_assumptions_key_still_counts_as_live(self, db):
        method, entity = _method(), await _entity(db)
        for _ in range(30):
            await db.pool.execute(
                """
                INSERT INTO prediction (entity_id, method, direction, confidence,
                                        entry_price, upper_barrier, lower_barrier,
                                        horizon_ends_at, provenance, outcome,
                                        resolved_at)
                VALUES ($1,$2,'up',0.85,100,110,90,$3,'{}'::jsonb,'upper',now())
                """,
                entity,
                method,
                NOW + timedelta(days=5),
            )

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.SCALE,
            target_hit_rate=0.6,
            walk_forward_positive=True,
        )
        assert verdict.live_resolved_n == 30
        assert verdict.eligible is True


class TestAudienceScoping:
    async def test_one_audiences_record_does_not_open_anothers_gate(self, db):
        method, entity = _method(), await _entity(db)
        owner, stranger = uuid4(), uuid4()
        await _record(db, entity, method=method, n=30, audience=owner)

        kw = {
            "method": method,
            "entity_kind": "company",
            "phase": TradingPhase.PAPER,
            "target_hit_rate": 0.6,
            "walk_forward_positive": True,
        }

        theirs = await eligible(db.pool, audience_user_id=owner, **kw)
        assert theirs.eligible is True
        assert theirs.resolved_n == 30

        other = await eligible(db.pool, audience_user_id=stranger, **kw)
        assert other.eligible is False
        assert other.reason is Ineligible.UNCALIBRATED
        assert other.resolved_n == 0

        shared = await eligible(db.pool, audience_user_id=None, **kw)
        assert shared.resolved_n == 0
        assert shared.eligible is False

    async def test_the_shared_record_counts_for_every_audience(self, db):
        method, entity = _method(), await _entity(db)
        await _record(db, entity, method=method, n=30)

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=uuid4(),
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
        )
        assert verdict.resolved_n == 30
        assert verdict.eligible is True


class TestEntityKindScoping:
    async def test_a_record_on_equities_does_not_authorise_crypto(self, db):
        method = _method()
        await _record(db, await _entity(db, "company"), method=method, n=30)

        kw = {
            "method": method,
            "audience_user_id": None,
            "phase": TradingPhase.PAPER,
            "target_hit_rate": 0.6,
            "walk_forward_positive": True,
        }

        equities = await eligible(db.pool, entity_kind="company", **kw)
        assert equities.eligible is True

        crypto = await eligible(db.pool, entity_kind="crypto_asset", **kw)
        assert crypto.eligible is False
        assert crypto.reason is Ineligible.UNCALIBRATED
        assert crypto.resolved_n == 0


class TestHalted:
    async def test_a_flawless_record_is_still_ineligible_when_halted(self, db):
        method, entity = _method(), await _entity(db)
        await _record(db, entity, method=method, n=100)

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.HALTED,
            target_hit_rate=0.6,
            walk_forward_positive=True,
        )
        assert verdict.eligible is False
        assert verdict.reason is Ineligible.PHASE_FORBIDS
        assert verdict.hit_rate == pytest.approx(1.0)
        assert verdict.resolved_n == 100


class TestRefusesToAnswerWithoutItsInputs:
    async def test_an_unreadable_ledger_raises_rather_than_returning_a_verdict(self):
        class DeadPool:
            async def fetch(self, *args):
                raise ConnectionError("pool is closed")

        with pytest.raises(ConnectionError):
            await eligible(
                DeadPool(),
                method="trend.sma",
                entity_kind="company",
                audience_user_id=None,
                phase=TradingPhase.PAPER,
                target_hit_rate=0.6,
                walk_forward_positive=True,
            )

    async def test_a_nan_target_raises_instead_of_passing_every_comparison(self, db):
        with pytest.raises(ValueError, match="NaN"):
            await eligible(
                db.pool,
                method="trend.sma",
                entity_kind="company",
                audience_user_id=None,
                phase=TradingPhase.PAPER,
                target_hit_rate=float("nan"),
                walk_forward_positive=True,
            )

    async def test_an_out_of_range_target_raises(self, db):
        with pytest.raises(ValueError, match="out of range"):
            await eligible(
                db.pool,
                method="trend.sma",
                entity_kind="company",
                audience_user_id=None,
                phase=TradingPhase.PAPER,
                target_hit_rate=1.4,
                walk_forward_positive=True,
            )

    async def test_an_unknown_phase_raises_rather_than_defaulting(self, db):
        with pytest.raises(ValueError):
            await eligible(
                db.pool,
                method="trend.sma",
                entity_kind="company",
                audience_user_id=None,
                phase="live",
                target_hit_rate=0.6,
                walk_forward_positive=True,
            )
