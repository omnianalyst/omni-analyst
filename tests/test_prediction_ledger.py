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
from uuid import uuid4

import pytest

from omni.capabilities.fundamentals import dcf_directional, dcf_valuation
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
from omni.conviction.predict import (
    _first_passage_confidence,
    produce_dcf_prediction,
    produce_dcf_prediction_from_coverage,
)
from omni.conviction.publish import load_calibration, record, scorecard
from omni.coverage.fundamentals import assemble_fundamentals

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
    audience_user_id=None,
):
    return await db.pool.fetchval(
        """
        INSERT INTO prediction (entity_id, claim_id, method, direction, confidence,
                                entry_price, upper_barrier, lower_barrier,
                                horizon_ends_at, provenance, created_at,
                                audience_user_id)
        VALUES ($1,$2,$3,$4::prediction_direction,$5,$6,$7,$8,$9,$10::jsonb,$11,$12)
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
        audience_user_id,
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


class TestCalibrationAudiencePartition:
    """The Phase 1.1 acceptance test: a private (audience-owned) outcome cannot
    move a shared finding's conviction threshold.

    The defect (HANDOFF 6.5): calibration_bucket had no audience dimension, so a
    prediction resolved on a byo_only price series -- a deterministic function of
    audience-private data -- would land in the one global aggregate that
    publish.load_calibration read and gate.assess used for every audience. The
    fix (019) partitions the view by audience_user_id; this test proves the
    partition holds end to end, through record -> resolve -> load_calibration."""

    async def test_a_private_outcome_does_not_leak_into_shared_calibration(self, db):
        from uuid import uuid4

        owner = uuid4()
        other = uuid4()
        method = "fundamentals.dcf_valuation"
        created = NOW - timedelta(days=20)
        horizon = NOW - timedelta(days=1)

        # Shared layer: shared predictions on a shared entity, resolved by a
        # shared price crossing. These feed ONLY the shared bucket.
        e_shared = await _entity(db, "AAPL")
        for _ in range(MIN_RESOLVED_FOR_CALIBRATION + 1):
            await _seed_prediction(
                db, e_shared, direction="up", entry=100.0, upper=110.0,
                lower=90.0, confidence=0.82, method=method,
                created_at=created, horizon_ends_at=horizon,
            )
        await _price_claim(db, e_shared, 111.0, NOW - timedelta(days=5))

        # Private layer: a SEPARATE entity whose only price in the window is the
        # owner's byo_only series. Shared-network resolution finds no price for
        # this entity, so the outcome is decided purely by audience-private
        # data -- exactly the vector that must not reach the shared bucket.
        e_priv = await _entity(db, "MSFT")
        for _ in range(MIN_RESOLVED_FOR_CALIBRATION + 1):
            await _seed_prediction(
                db, e_priv, direction="up", entry=100.0, upper=110.0,
                lower=90.0, confidence=0.82, method=method,
                created_at=created, horizon_ends_at=horizon,
                audience_user_id=owner,
            )
        await _price_claim(
            db, e_priv, 111.0, NOW - timedelta(days=5), owner=owner
        )

        resolved = await resolve_due_predictions(db.pool)
        assert resolved == 2 * (MIN_RESOLVED_FOR_CALIBRATION + 1)

        per_audience = MIN_RESOLVED_FOR_CALIBRATION + 1

        # Shared calibration sees ONLY the shared outcomes. Pre-019 the
        # un-partitioned view would have aggregated the owner's private
        # resolutions into this same bucket and reported 2x; that is the leak.
        shared = await load_calibration(
            db.pool, claim_type="fundamental_metric", method=method
        )
        assert sum(b.n for b in shared) == per_audience
        assert sum(b.hits for b in shared) == per_audience

        # The owner sees the shared network PLUS their own private outcomes,
        # pooled -- the same 'shared + own' rule visibility.py enforces on
        # claims. This is the only path that may read the private bucket.
        pooled = await load_calibration(
            db.pool, claim_type="fundamental_metric", method=method, audience=owner
        )
        assert sum(b.n for b in pooled) == 2 * per_audience
        assert sum(b.hits for b in pooled) == 2 * per_audience

        # A different audience sees the shared bucket alone; the owner's private
        # resolutions are invisible to them, never pooled with their own.
        stranger = await load_calibration(
            db.pool, claim_type="fundamental_metric", method=method, audience=other
        )
        assert sum(b.n for b in stranger) == per_audience

    async def test_a_private_prediction_resolves_on_private_prices_alone(self, db):
        """The resolver is self-scoped: a prediction tagged to an audience
        resolves on that audience's visible prices, and a shared prediction on
        the same entity stays pending when the shared network has no price.

        Proves the audience_user_id on the prediction row drives the resolution
        scope (read back in _resolve_one), not a global parameter. A shared
        resolver (the old default) would have left the private prediction
        pending; the owner's prediction resolves, the shared one does not."""
        from uuid import uuid4

        owner = uuid4()
        e = await _entity(db)
        cross = NOW - timedelta(days=5)

        shared_pid = await _seed_prediction(
            db, e, direction="up", entry=100.0, upper=110.0, lower=90.0,
            created_at=NOW - timedelta(days=10), horizon_ends_at=NOW - timedelta(days=1),
        )
        priv_pid = await _seed_prediction(
            db, e, direction="up", entry=100.0, upper=110.0, lower=90.0,
            created_at=NOW - timedelta(days=10), horizon_ends_at=NOW - timedelta(days=1),
            audience_user_id=owner,
        )
        # The ONLY price in the window is the owner's byo series.
        await _price_claim(db, e, 111.0, cross, owner=owner)

        await resolve_due_predictions(db.pool)
        assert await db.pool.fetchval(
            "SELECT outcome FROM prediction WHERE id=$1", priv_pid
        ) == "upper"
        assert await db.pool.fetchval(
            "SELECT outcome FROM prediction WHERE id=$1", shared_pid
        ) == "pending"


class TestEndToEndConviction:
    """Phase 1.3 acceptance: the conviction gate closes end to end.

    The chain: (seeded coverage) -> dcf_directional -> record_prediction ->
    horizon elapses -> resolve -> calibration -> gate surfaces a finding with
    its own calibrated hit rate -> that finding's prediction resolves ->
    finding_hit_rate records it. The thesis -- 'show your own hit rate on the
    things you chose to surface' -- produces a number.

    Coverage assembly is seeded directly: fundamentals as a dict (the
    fundamentals claim-assembler is the deferred follow-up, orthogonal to
    proving the gate), and price_snapshot as REAL claims so resolution runs
    through the real resolver and the visibility rule. The conviction half --
    the producer, the ledger, the gate, the scorecard -- is the live machinery.
    """

    METHOD = "fundamentals.dcf_valuation"

    def _fundamentals(self) -> dict:
        return {
            "income_statement": {
                "eps": 10.0,
                "earnings_growth_rate": 0.20,
                "net_income": 1_000_000,
                "revenue": 5_000_000,
                "cost_of_revenue": 2_000_000,
                "operating_income": 1_500_000,
                "dividends_per_share": 2.0,
                "revenue_growth_rate": 0.20,
            },
            "balance_sheet": {
                "book_value_per_share": 50.0,
                "total_equity": 4_000_000,
                "total_assets": 10_000_000,
                "total_debt": 2_000_000,
                "current_assets": 3_000_000,
                "current_liabilities": 1_500_000,
                "inventory": 500_000,
                "market_cap": 8_000_000,
                "cash_and_equivalents": 500_000,
                "shares_outstanding": 100_000,
            },
            "cash_flow": {
                "operating_cash_flow": 1_200_000,
                "capital_expenditures": 200_000,
            },
            "beta": 1.2,
        }

    async def test_resolved_dcf_predictions_open_the_gate_and_score_a_finding(self, db):
        e = await _entity(db, "AAPL")
        fundamentals = self._fundamentals()
        created = NOW - timedelta(days=20)
        horizon = NOW - timedelta(days=1)

        # fair_value is price-independent, so compute base + bear to place an
        # entry that yields a genuine up-call straddle (bear < entry < base).
        # A current_price of 100 sits below even the bear case for this growth
        # fixture, so dcf_directional would honestly refuse there; the midpoint
        # guarantees the straddle the gate needs to calibrate.
        base_out = await dcf_valuation(fundamentals, 100.0)
        base_fv = float(base_out["fair_value_per_share"])
        b_growth = base_out["assumptions"]["growth_rate"]
        b_disc = base_out["assumptions"]["discount_rate"]
        b_term = base_out["assumptions"]["terminal_growth_rate"]
        bear_out = await dcf_valuation(
            fundamentals, 100.0,
            growth_rate=b_growth * 0.5, discount_rate=b_disc + 0.02,
            terminal_growth_rate=b_term,
        )
        current_price = (float(bear_out["fair_value_per_share"]) + base_fv) / 2

        # Learn the deterministic call so the test can place the resolving price
        # on the target and use the producer's own confidence.
        call = await dcf_directional(fundamentals, current_price)
        assert call["direction"] == "up"  # pins the scenario
        upper = call["upper_barrier"]
        confidence = _first_passage_confidence(
            "up", current_price, call["upper_barrier"], call["lower_barrier"]
        )

        # 1. Accrue resolved predictions for the method on the shared network.
        for _ in range(MIN_RESOLVED_FOR_CALIBRATION + 1):
            pid = await produce_dcf_prediction(
                db.pool,
                entity_id=e,
                audience_user_id=None,
                fundamentals=fundamentals,
                current_price=current_price,
                horizon_ends_at=horizon,
                created_at=created,
            )
            assert pid is not None

        # 2. A shared price path crossing the target resolves every up-call.
        await _price_claim(db, e, upper * 1.01, NOW - timedelta(days=5))
        resolved = await resolve_due_predictions(db.pool)
        assert resolved == MIN_RESOLVED_FOR_CALIBRATION + 1

        # 3. Calibration accrues; every resolved prediction is a hit.
        buckets = await load_calibration(
            db.pool, claim_type="fundamental_metric", method=self.METHOD
        )
        n_total = sum(b.n for b in buckets)
        hits_total = sum(b.hits for b in buckets)
        assert n_total == MIN_RESOLVED_FOR_CALIBRATION + 1
        assert hits_total == n_total
        assert any(b.n >= MIN_RESOLVED_FOR_CALIBRATION for b in buckets)

        # 4. The gate derives a threshold from that calibration and surfaces a
        #    finding at the producer's own confidence. The thesis made concrete:
        #    the system says something only because its past self earned it.
        candidate = Candidate(
            claim_type="fundamental_metric",
            method=self.METHOD,
            confidence=confidence,
            claim_id=None,
            supporting=("dcf fair_value above the market price",),
            searched_for_disconfirming=True,
            falsifiable=True,
        )
        verdict = assess(candidate, buckets)
        assert verdict.refusal is not Refusal.UNCALIBRATED
        assert verdict.surfaced
        assert verdict.threshold is not None
        assert verdict.threshold <= confidence

        # 5. Record the surfaced finding with its own falsifiable prediction,
        #    resolve that prediction as a hit, and read the scorecard. A surfaced
        #    finding without a resolvable prediction would let the published hit
        #    rate drift from what was actually claimed -- the view prevents that
        #    by joining finding -> prediction.
        finding_pid = await _seed_prediction(
            db, e, direction="up", entry=current_price,
            upper=call["upper_barrier"], lower=call["lower_barrier"],
            confidence=confidence, method=self.METHOD,
            created_at=created, horizon_ends_at=horizon,
        )
        await record(
            db.pool, verdict, entity_id=e, audience_user_id=None,
            prediction_id=finding_pid,
        )
        await resolve_due_predictions(db.pool)

        row = await db.pool.fetchrow(
            "SELECT surfaced, resolved, hits FROM finding_hit_rate WHERE method=$1",
            self.METHOD,
        )
        assert row is not None
        assert row["surfaced"] == 1
        assert row["resolved"] == 1
        assert row["hits"] == 1

        # 6. The scorecard stays honestly silent below its 10-resolved floor:
        #    one surfaced finding is not enough to claim a hit rate. None, not
        #    zero -- 'unknown', never a fake percentage.
        score = next(
            s for s in await scorecard(db.pool) if s["method"] == self.METHOD
        )
        assert score["surfaced"] == 1
        assert score["resolved"] == 1
        assert score["hit_rate"] is None


class TestEndToEndFromCoverage:
    """The live entry point: claims alone -> a prediction. No hand-fed dict.

    Proves the seam the scheduler loop will call: read price + fundamentals from
    coverage, assemble, produce. The fundamentals claim-assembler and the
    producer are exercised together; the gate-surfacing half is already covered
    by TestEndToEndConviction. Abstention (None) on missing price or incomplete
    fundamentals is the honest outcome, not a failure.
    """

    METHOD = "fundamentals.dcf_valuation"

    async def _seed_fundamentals(self, db, e) -> None:
        from datetime import UTC, datetime, timedelta

        end = datetime(2024, 12, 31, tzinfo=UTC)
        filed = end + timedelta(days=46)
        prior_end = datetime(2023, 12, 31, tzinfo=UTC)
        prior_filed = prior_end + timedelta(days=46)
        year_start = (end - timedelta(days=365)).date().isoformat()
        prior_year_start = (prior_end - timedelta(days=365)).date().isoformat()
        pairs = [
            ("NetCashProvidedByUsedInOperatingActivities", 1_200_000, end, filed, year_start),
            ("PaymentsToAcquirePropertyPlantAndEquipment", 200_000, end, filed, year_start),
            ("CommonStockSharesOutstanding", 100_000, end, filed, None),
            ("CashAndCashEquivalentsAtCarryingValue", 500_000, end, filed, None),
            ("StockholdersEquity", 4_000_000, end, filed, None),
            ("LongTermDebt", 2_000_000, end, filed, None),
            ("LongTermDebtCurrent", 100_000, end, filed, None),
            ("Revenues", 5_000_000, end, filed, year_start),
            ("Revenues", 4_000_000, prior_end, prior_filed, prior_year_start),
        ]
        for concept, val, ev, kf, start in pairs:
            await db.pool.execute(
                """
                INSERT INTO claim (entity_id, claim_type, key, value, source,
                                   event_date, knowledge_date, confidence,
                                   redistributable, audience_user_id, evidence)
                VALUES ($1,'fundamental_metric',$2,$3::jsonb,'sec_edgar',
                        $4,$5,1.0,'allowed',NULL,$6::jsonb)
                """,
                e, concept, json.dumps({"value": val}), ev, kf,
                json.dumps({"cik": "0000320193", "form": "10-K", "fp": "FY", "start": start}),
            )

    async def test_complete_coverage_produces_a_resolvable_prediction(self, db):
        e = await _entity(db, "AAPL")
        await self._seed_fundamentals(db, e)
        # Place the entry between the bear and base fair values (price-independent
        # of current_price) so the up-call straddles. Computed from the assembled
        # dict, not hard-coded, so a fixture change moves the entry with it.
        fundamentals = await assemble_fundamentals(
            db.pool, entity_id=e, as_of=NOW, current_price=100.0
        )
        base_fv = float((await dcf_valuation(fundamentals, 100.0))["fair_value_per_share"])
        b_growth = 0.20
        b_disc = (await dcf_valuation(fundamentals, 100.0))["assumptions"]["discount_rate"]
        bear_fv = float((await dcf_valuation(
            fundamentals, 100.0,
            growth_rate=b_growth * 0.5, discount_rate=b_disc + 0.02,
            terminal_growth_rate=0.03,
        ))["fair_value_per_share"])
        entry = (bear_fv + base_fv) / 2

        await _price_claim(db, e, entry, NOW - timedelta(days=1))

        pid = await produce_dcf_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            as_of=NOW, horizon_ends_at=NOW + timedelta(days=30),
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT method, direction, entry_price, upper_barrier, lower_barrier, "
            "outcome FROM prediction WHERE id=$1",
            pid,
        )
        assert row["method"] == self.METHOD
        assert row["direction"] == "up"
        assert float(row["entry_price"]) == pytest.approx(entry)
        assert float(row["upper_barrier"]) > float(row["entry_price"]) > float(row["lower_barrier"])
        assert row["outcome"] == "pending"

    async def test_no_visible_price_abstains(self, db):
        e = await _entity(db, "AAPL")
        await self._seed_fundamentals(db, e)
        # No price_snapshot claim at all.
        pid = await produce_dcf_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            as_of=NOW, horizon_ends_at=NOW + timedelta(days=30),
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_incomplete_fundamentals_abstains(self, db):
        e = await _entity(db, "AAPL")
        await self._seed_fundamentals(db, e)
        await _price_claim(db, e, 100.0, NOW - timedelta(days=1))
        # Remove an essential; coverage is now insufficient for an honest DCF.
        await db.pool.execute(
            "DELETE FROM claim WHERE entity_id=$1 "
            "AND key='NetCashProvidedByUsedInOperatingActivities'",
            e,
        )
        pid = await produce_dcf_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            as_of=NOW, horizon_ends_at=NOW + timedelta(days=30),
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0


class TestSchedulerPredictLoop:
    """The fourth loop is wired: a demanded company entity with coverage gets a
    directional call, deduped so the loop cannot flood the ledger. BYO by
    design -- the price the call resolves against is the audience's own."""

    async def test_the_predict_loop_writes_one_deduped_call(self, db):
        from omni.scheduler.worker import Scheduler, SchedulerConfig, default_registry

        owner = uuid4()
        e = await _entity(db, "AAPL")
        # Active demand from a specific audience is what makes the entity a
        # prediction target (demand-driven, not whole-universe).
        await db.pool.execute(
            "INSERT INTO demand (entity_id, claim_type, channel, requested_by, active) "
            "VALUES ($1,'fundamental_metric','test',$2,true)",
            e, owner,
        )
        # Seed the same fundamentals the assembler test uses, then a BYO price
        # owned by the audience, placed between bear and base for a straddle.
        await self._seed_fundamentals(db, e)
        fundamentals = await assemble_fundamentals(
            db.pool, entity_id=e, as_of=NOW, current_price=100.0, audience=owner
        )
        base_out = await dcf_valuation(fundamentals, 100.0)
        base_fv = float(base_out["fair_value_per_share"])
        b_disc = base_out["assumptions"]["discount_rate"]
        bear_fv = float((await dcf_valuation(
            fundamentals, 100.0,
            growth_rate=0.20 * 0.5, discount_rate=b_disc + 0.02,
            terminal_growth_rate=0.03,
        ))["fair_value_per_share"])
        entry = (bear_fv + base_fv) / 2
        await _price_claim(db, e, entry, NOW - timedelta(days=1), owner=owner)

        scheduler = Scheduler(
            db.pool,
            default_registry(),
            SchedulerConfig(
                predict_interval=0.05, sweep_interval=999,
                fill_interval=999, fill_workers=0, resolve_interval=999,
            ),
        )
        await scheduler.start()
        count = 0
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 30
            while loop.time() < deadline:
                count = await db.pool.fetchval(
                    "SELECT count(*) FROM prediction "
                    "WHERE entity_id=$1 AND method='fundamentals.dcf_valuation'",
                    e,
                )
                if count >= 1:
                    # Let the loop fire again to prove dedup holds: a second
                    # cycle must NOT add a second pending call.
                    await asyncio.sleep(0.15)
                    break
                await asyncio.sleep(0.05)
        finally:
            await scheduler.stop()

        assert count >= 1
        assert scheduler.stats.predicted >= 1
        # Dedup: one pending DCF call per (entity, method, audience). The loop
        # fires every 0.05s; after the first write it must skip, not stack rows.
        final = await db.pool.fetchval(
            "SELECT count(*) FROM prediction "
            "WHERE entity_id=$1 AND method='fundamentals.dcf_valuation'",
            e,
        )
        assert final == 1

    async def _seed_fundamentals(self, db, e) -> None:
        from datetime import UTC, datetime, timedelta

        end = datetime(2024, 12, 31, tzinfo=UTC)
        filed = end + timedelta(days=46)
        prior_end = datetime(2023, 12, 31, tzinfo=UTC)
        prior_filed = prior_end + timedelta(days=46)
        year_start = (end - timedelta(days=365)).date().isoformat()
        prior_year_start = (prior_end - timedelta(days=365)).date().isoformat()
        pairs = [
            ("NetCashProvidedByUsedInOperatingActivities", 1_200_000, end, filed, year_start),
            ("PaymentsToAcquirePropertyPlantAndEquipment", 200_000, end, filed, year_start),
            ("CommonStockSharesOutstanding", 100_000, end, filed, None),
            ("CashAndCashEquivalentsAtCarryingValue", 500_000, end, filed, None),
            ("StockholdersEquity", 4_000_000, end, filed, None),
            ("LongTermDebt", 2_000_000, end, filed, None),
            ("LongTermDebtCurrent", 100_000, end, filed, None),
            ("Revenues", 5_000_000, end, filed, year_start),
            ("Revenues", 4_000_000, prior_end, prior_filed, prior_year_start),
        ]
        for concept, val, ev, kf, start in pairs:
            await db.pool.execute(
                """
                INSERT INTO claim (entity_id, claim_type, key, value, source,
                                   event_date, knowledge_date, confidence,
                                   redistributable, audience_user_id, evidence)
                VALUES ($1,'fundamental_metric',$2,$3::jsonb,'sec_edgar',
                        $4,$5,1.0,'allowed',NULL,$6::jsonb)
                """,
                e, concept, json.dumps({"value": val}), ev, kf,
                json.dumps({"cik": "0000320193", "form": "10-K", "fp": "FY", "start": start}),
            )
