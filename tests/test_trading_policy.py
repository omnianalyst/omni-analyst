"""The gate between a calibrated method and capital.

The bar used to be hit rate. That was the wrong statistic, not a badly tuned
one, and the two headline tests here are the proof in both directions: a 33%
method at 4.32:1 earns money and must be admitted, a 67% method at 1:4 loses
money and must be refused. The old gate got both backwards.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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

# `eligible` refuses to default any of these, so every call site states them.
# 20bps is the CEX taker round trip the reference profiles price: 10bps a leg,
# both legs taken, because a barrier exit is a taker exit.
GATE = {
    "round_trip_cost_bps": Decimal(20),
    "min_expectancy_bps": Decimal(5),
    "min_effective_n": MIN_RESOLVED_FOR_PAPER,
    "max_assumed_share": Decimal("0.5"),
    "max_concentration": Decimal("0.5"),
}

# The non-risk arguments, so the tests below vary only the parameter under test.
_BASE = {
    "method": "trend.sma",
    "entity_kind": "company",
    "audience_user_id": None,
    "phase": TradingPhase.PAPER,
    "target_hit_rate": 0.6,
    "walk_forward_positive": True,
}

# The concentration bar, lifted. A fixture holding one name is 100% concentrated
# by definition, so leaving the bar on would turn every single-entity test in
# this module into a concentration test wearing someone else's name.
SINGLE_ENTITY = {**GATE, "max_concentration": Decimal(1)}


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


async def _entities(db, n, kind="company"):
    return [await _entity(db, kind) for _ in range(n)]


async def _resolved(
    db,
    entity_id,
    *,
    method,
    hit=True,
    outcome=None,
    confidence=0.85,
    audience=None,
    backfilled=False,
    pending=False,
    upper="110",
    lower="90",
    horizon_days=5,
):
    provenance = {"capability": method, "input_claims": [], "assumptions": {}}
    if backfilled:
        provenance["assumptions"]["backfill"] = True
    resolution = "pending" if pending else (outcome or ("upper" if hit else "lower"))
    return await db.pool.fetchval(
        """
        INSERT INTO prediction (entity_id, method, direction, confidence,
                                entry_price, upper_barrier, lower_barrier,
                                horizon_ends_at, provenance, audience_user_id,
                                outcome, resolved_at)
        VALUES ($1,$2,'up',$3,100,$4,$5,$6,$7::jsonb,$8,
                $9::prediction_outcome,$10)
        RETURNING id
        """,
        entity_id,
        method,
        confidence,
        Decimal(upper),
        Decimal(lower),
        NOW + timedelta(days=horizon_days),
        json.dumps(provenance),
        audience,
        resolution,
        None if pending else NOW,
    )


async def _record(db, entity_id, *, method, n, hits=None, horizon_start=5, **kw):
    """`n` resolved predictions on `n` distinct horizon dates.

    Distinct dates because the floor applies to `effective_n`: correlated names
    resolving on one day are close to one observation, and a fixture that put
    thirty predictions on a single date would be asserting the gate admits three
    independent outcomes rather than thirty.
    """
    hits = n if hits is None else hits
    for i in range(n):
        await _resolved(
            db,
            entity_id,
            method=method,
            hit=i < hits,
            horizon_days=horizon_start + i,
            **kw,
        )


async def _spread(db, entities, *, method, outcomes, upper, lower, horizon_start=5):
    """The same sequence of outcomes on every entity, one per horizon date.

    Identical per entity so each name carries the same mean: the concentration
    bar is then not silently the thing under test in a payoff test.
    """
    for entity in entities:
        for i, outcome in enumerate(outcomes):
            await _resolved(
                db,
                entity,
                method=method,
                outcome=outcome,
                upper=upper,
                lower=lower,
                horizon_days=horizon_start + i,
            )


class TestTheBarIsExpectancyNotHitRate:
    """The defect, in both directions.

    Hit rate is a proxy for edge only when the payoffs are symmetric. Both
    fixtures below are asymmetric, and a gate reading hit rate gets both of them
    exactly wrong -- it refuses the one that makes money and admits the one that
    loses it.
    """

    async def test_a_low_hit_rate_high_payoff_method_is_eligible(self, db):
        """34% at 4.32:1, the shape `trend.sma` actually has on crypto.

            entry 100, target 104.32 (+432bps), stop 99 (-100bps)
            10 wins and 20 losses per name, three names, 30 horizon dates

            gross = (10 * 432 - 20 * 100) / 30 = +77.33 bps per trade
            net   = 77.33 - 20 (CEX taker round trip) = +57.33 bps

        The hit rate is 0.333, far below the 0.6 target that used to bar. The
        strategy makes money on every hundred trades and the old gate refused it
        with BELOW_HIT_RATE.
        """
        method, entities = _method(), await _entities(db, 3)
        await _spread(
            db,
            entities,
            method=method,
            outcomes=["upper"] * 10 + ["lower"] * 20,
            upper="104.32",
            lower="99",
        )

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
            **GATE,
        )

        assert verdict.hit_rate == pytest.approx(1 / 3)
        assert verdict.hit_rate < 0.6, "the fixture must be one the old bar refused"
        assert verdict.expectancy_n == 90
        assert verdict.effective_n == 30
        assert round(verdict.gross_expectancy_bps, 3) == Decimal("77.333")
        assert round(verdict.net_expectancy_bps, 3) == Decimal("57.333")
        assert verdict.reason is None
        assert verdict.eligible is True

    async def test_a_high_hit_rate_low_payoff_method_is_refused(self, db):
        """67% at 1:4, which the old gate would have funded.

            entry 100, target 101 (+100bps), stop 96 (-400bps)
            20 wins and 10 losses per name

            gross = (20 * 100 - 10 * 400) / 30 = -66.67 bps per trade
            net   = -86.67 bps

        Two thirds of these trades win. Every hundred of them loses money, and a
        gate reading only the hit rate would have called this the better of the
        two strategies on this page.
        """
        method, entities = _method(), await _entities(db, 3)
        await _spread(
            db,
            entities,
            method=method,
            outcomes=["upper"] * 20 + ["lower"] * 10,
            upper="101",
            lower="96",
        )

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
            **GATE,
        )

        assert verdict.hit_rate == pytest.approx(2 / 3)
        assert verdict.hit_rate >= 0.6, "the fixture must be one the old bar passed"
        assert round(verdict.gross_expectancy_bps, 3) == Decimal("-66.667")
        assert round(verdict.net_expectancy_bps, 3) == Decimal("-86.667")
        assert verdict.eligible is False
        assert verdict.reason is Ineligible.BELOW_EXPECTANCY
        assert "-86.7 bps net" in verdict.detail

    async def test_the_hit_rate_is_still_reported_it_just_does_not_bar(self, db):
        """It remains the most legible single number about a method.

        Removing it from the payload would trade one blind spot for another: a
        67%-at-1:4 strategy and a 33%-at-4:1 strategy have the same expectancy
        sign only by coincidence, and the operator needs both figures to see
        which kind of thing they are looking at.
        """
        method, entities = _method(), await _entities(db, 3)
        await _spread(
            db,
            entities,
            method=method,
            outcomes=["upper"] * 10 + ["lower"] * 20,
            upper="104.32",
            lower="99",
        )

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.99,
            walk_forward_positive=True,
            **GATE,
        )

        assert verdict.hit_rate == pytest.approx(1 / 3)
        assert verdict.measured_n == 90
        # A 0.99 target would once have refused every method ever written.
        assert verdict.eligible is True
        assert verdict.reason is None

    async def test_the_expectancy_threshold_is_the_parameter_not_a_constant(self, db):
        """The same record, admitted at 5bps and refused at 60bps."""
        method, entities = _method(), await _entities(db, 3)
        await _spread(
            db,
            entities,
            method=method,
            outcomes=["upper"] * 10 + ["lower"] * 20,
            upper="104.32",
            lower="99",
        )

        kw = {
            "method": method,
            "entity_kind": "company",
            "audience_user_id": None,
            "phase": TradingPhase.PAPER,
            "target_hit_rate": 0.6,
            "walk_forward_positive": True,
        }

        admitted = await eligible(db.pool, **kw, **GATE)
        assert admitted.eligible is True

        refused = await eligible(
            db.pool, **kw, **{**GATE, "min_expectancy_bps": Decimal(60)}
        )
        assert refused.eligible is False
        assert refused.reason is Ineligible.BELOW_EXPECTANCY

    async def test_the_round_trip_cost_can_eat_the_whole_edge(self, db):
        """+77bps gross is a 500bps venue's loss, and the gate must say so."""
        method, entities = _method(), await _entities(db, 3)
        await _spread(
            db,
            entities,
            method=method,
            outcomes=["upper"] * 10 + ["lower"] * 20,
            upper="104.32",
            lower="99",
        )

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
            **{**GATE, "round_trip_cost_bps": Decimal(500)},
        )

        assert verdict.gross_expectancy_bps > 0
        assert verdict.net_expectancy_bps < 0
        assert verdict.eligible is False
        assert verdict.reason is Ineligible.BELOW_EXPECTANCY


class TestTheFloorAppliesToTheEffectiveSample:
    async def test_four_hundred_predictions_on_four_dates_is_four_observations(
        self, db
    ):
        """400 raw outcomes, every one a winner, and still refused.

        The first real run resolved 424 predictions across 44 horizon dates:
        nine correlated crypto assets resolving on one day are close to one
        observation, not nine. A floor of thirty on the raw count was cleared by
        roughly three independent outcomes.
        """
        method, entities = _method(), await _entities(db, 4)
        for entity in entities:
            for i in range(100):
                await _resolved(
                    db,
                    entity,
                    method=method,
                    outcome="upper",
                    upper="104.32",
                    lower="99",
                    horizon_days=i % 4,
                )

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
            **GATE,
        )

        assert verdict.expectancy_n == 400
        assert verdict.effective_n == 4
        assert verdict.gross_expectancy_bps == Decimal(432)
        assert verdict.eligible is False
        assert verdict.reason is Ineligible.INSUFFICIENT_RESOLVED
        assert "4 distinct horizon dates" in verdict.detail

    async def test_forty_predictions_on_forty_dates_clears_the_floor(self, db):
        """A tenth of the sample above, and admitted, because it is 40
        observations rather than 4."""
        method, entities = _method(), await _entities(db, 4)
        await _spread(
            db,
            entities,
            method=method,
            outcomes=["upper"] * 10,
            upper="104.32",
            lower="99",
        )

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
            **{**GATE, "min_effective_n": 10},
        )

        assert verdict.expectancy_n == 40
        assert verdict.effective_n == 10
        assert verdict.eligible is True


class TestAssumedPnlIsRefusedNotAveraged:
    async def test_a_sample_mostly_expired_is_mostly_unmeasured(self, db):
        """20 of every 30 expired without touching a barrier.

        The ledger records that they expired; it does not record the price they
        expired at, so their P&L is scored as zero because nothing else is
        available. Two thirds of this result is therefore a number nobody
        observed, and the pooled +144bps is a statement about the other third.
        """
        method, entities = _method(), await _entities(db, 3)
        await _spread(
            db,
            entities,
            method=method,
            outcomes=["upper"] * 10 + ["expiry"] * 20,
            upper="104.32",
            lower="99",
        )

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
            **GATE,
        )

        assert round(verdict.assumed_share, 6) == Decimal("0.666667")
        assert verdict.gross_expectancy_bps == Decimal(144)
        assert verdict.net_expectancy_bps > 0
        assert verdict.eligible is False
        assert verdict.reason is Ineligible.TOO_MUCH_ASSUMED
        assert "60 of 90" in verdict.detail
        assert "assumed rather than measured" in verdict.detail

    async def test_the_limit_is_the_parameter_and_the_same_record_can_clear_it(
        self, db
    ):
        method, entities = _method(), await _entities(db, 3)
        await _spread(
            db,
            entities,
            method=method,
            outcomes=["upper"] * 10 + ["expiry"] * 20,
            upper="104.32",
            lower="99",
        )

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
            **{**GATE, "max_assumed_share": Decimal("0.7")},
        )
        assert verdict.eligible is True


class TestConcentrationIsRefused:
    async def test_one_name_carrying_the_edge_is_a_position_not_a_strategy(self, db):
        """One flawless name against two ordinary ones.

            carrier: 30 wins            -> mean +432, |contribution| 12,960
            other two: 15 wins, 15 losses -> mean +166, |contribution|  4,980 each

        56.5% of the absolute P&L comes from one name. Pooled, the method reads
        +254bps and looks like an edge; it is one asset's record with a method
        name on it.
        """
        method = _method()
        carrier, *rest = await _entities(db, 3)
        await _spread(
            db,
            [carrier],
            method=method,
            outcomes=["upper"] * 30,
            upper="104.32",
            lower="99",
        )
        await _spread(
            db,
            rest,
            method=method,
            outcomes=["upper"] * 15 + ["lower"] * 15,
            upper="104.32",
            lower="99",
        )
        symbol = await db.pool.fetchval(
            "SELECT symbol FROM entity WHERE id = $1", carrier
        )

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
            **GATE,
        )

        assert round(verdict.concentration, 4) == Decimal("0.5654")
        assert verdict.positive_entities == 3
        assert verdict.net_expectancy_bps > 0
        assert verdict.eligible is False
        assert verdict.reason is Ineligible.CONCENTRATED
        assert symbol in verdict.detail

    async def test_the_same_book_clears_a_looser_limit(self, db):
        method = _method()
        carrier, *rest = await _entities(db, 3)
        await _spread(
            db,
            [carrier],
            method=method,
            outcomes=["upper"] * 30,
            upper="104.32",
            lower="99",
        )
        await _spread(
            db,
            rest,
            method=method,
            outcomes=["upper"] * 15 + ["lower"] * 15,
            upper="104.32",
            lower="99",
        )

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
            **{**GATE, "max_concentration": Decimal("0.6")},
        )
        assert verdict.eligible is True


class TestTheRiskParametersHaveNoDefaults:
    """A default here is an invented risk parameter every caller inherits.

    The one that matters most is `round_trip_cost_bps`: a caller who never
    thought about the cost of a round trip is exactly the caller whose edge does
    not survive one, and a default of zero would hand them a verdict computed on
    a gross figure while the payload said net.
    """

    @pytest.mark.parametrize("omitted", sorted(GATE))
    async def test_each_one_omitted_raises(self, omitted):
        supplied = {k: v for k, v in GATE.items() if k != omitted}
        with pytest.raises(TypeError, match=omitted):
            await eligible(object(), **_BASE, **supplied)

    async def test_a_nan_threshold_raises_rather_than_barring_nothing(self, db):
        for name in ("round_trip_cost_bps", "min_expectancy_bps",
                     "max_assumed_share", "max_concentration"):
            with pytest.raises(ValueError, match=f"{name} must be finite"):
                await eligible(
                    db.pool,
                    **_BASE,
                    **{**GATE, name: Decimal("NaN")},
                )

    async def test_a_negative_round_trip_cost_is_not_a_credit(self, db):
        with pytest.raises(ValueError, match="must not be a credit"):
            await eligible(
                db.pool,
                **_BASE,
                **{**GATE, "round_trip_cost_bps": Decimal(-5)},
            )

    async def test_a_non_positive_minimum_expectancy_is_refused(self, db):
        with pytest.raises(ValueError, match="min_expectancy_bps must be positive"):
            await eligible(
                db.pool,
                **_BASE,
                **{**GATE, "min_expectancy_bps": Decimal(0)},
            )

    async def test_a_zero_effective_floor_is_refused(self, db):
        with pytest.raises(ValueError, match="min_effective_n must be at least 1"):
            await eligible(db.pool, **_BASE, **{**GATE, "min_effective_n": 0})

    async def test_an_out_of_range_share_is_refused(self, db):
        with pytest.raises(ValueError, match="max_assumed_share out of range"):
            await eligible(
                db.pool,
                **_BASE,
                **{**GATE, "max_assumed_share": Decimal("1.5")},
            )
        with pytest.raises(ValueError, match="max_concentration out of range"):
            await eligible(
                db.pool,
                **_BASE,
                **{**GATE, "max_concentration": Decimal("1.5")},
            )


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
            **SINGLE_ENTITY,
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
            **SINGLE_ENTITY,
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
            **SINGLE_ENTITY,
        )

        assert verdict.resolved_n == 5
        assert verdict.reason is Ineligible.UNCALIBRATED

    async def test_a_method_with_nothing_resolved_is_uncalibrated(self, db):
        method, entity = _method(), await _entity(db)
        await _record(db, entity, method=method, n=5, pending=True)

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
            **SINGLE_ENTITY,
        )

        assert verdict.resolved_n == 0
        assert verdict.expectancy_n == 0
        assert verdict.gross_expectancy_bps is None
        assert verdict.net_expectancy_bps is None
        assert verdict.assumed_share is None
        assert verdict.concentration is None
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
            **SINGLE_ENTITY,
        )
        assert short.eligible is False
        assert short.reason is Ineligible.INSUFFICIENT_RESOLVED
        assert short.resolved_n == MIN_RESOLVED_FOR_PAPER - 1
        assert short.effective_n == MIN_RESOLVED_FOR_PAPER - 1

        await _record(
            db, entity, method=method, n=1, horizon_start=5 + MIN_RESOLVED_FOR_PAPER
        )

        now_enough = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.PAPER,
            target_hit_rate=0.6,
            walk_forward_positive=True,
            **SINGLE_ENTITY,
        )
        assert now_enough.eligible is True
        assert now_enough.reason is None
        assert now_enough.resolved_n == MIN_RESOLVED_FOR_PAPER
        assert now_enough.effective_n == MIN_RESOLVED_FOR_PAPER
        assert now_enough.hit_rate == pytest.approx(1.0)


class TestWalkForward:
    async def test_paper_requires_a_walk_forward_too(self, db):
        """GATE A admits a strategy to paper only once it has held out of sample.

        This previously asserted the opposite, and the implementation obliged by
        gating the check behind MICRO with a permissive default. The reason it
        has to bind at PAPER: an in-sample expectancy is fitted to the outcomes
        it is judged on, so a paper record built on one inherits the overfit
        rather than testing it -- and GATE B then reads that paper record.
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
            **SINGLE_ENTITY,
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
                **GATE,
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
            **SINGLE_ENTITY,
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
            **SINGLE_ENTITY,
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
        for i in range(30):
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
                NOW + timedelta(days=5 + i),
            )

        verdict = await eligible(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            phase=TradingPhase.SCALE,
            target_hit_rate=0.6,
            walk_forward_positive=True,
            **SINGLE_ENTITY,
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
            **SINGLE_ENTITY,
        }

        theirs = await eligible(db.pool, audience_user_id=owner, **kw)
        assert theirs.eligible is True
        assert theirs.resolved_n == 30

        other = await eligible(db.pool, audience_user_id=stranger, **kw)
        assert other.eligible is False
        assert other.reason is Ineligible.UNCALIBRATED
        assert other.resolved_n == 0
        assert other.expectancy_n == 0

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
            **SINGLE_ENTITY,
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
            **SINGLE_ENTITY,
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
            **SINGLE_ENTITY,
        )
        assert verdict.eligible is False
        assert verdict.reason is Ineligible.PHASE_FORBIDS
        assert verdict.hit_rate == pytest.approx(1.0)
        assert verdict.resolved_n == 100
        # The record is still reported. A halt is a decision about the operator,
        # not a reason to stop measuring the method.
        assert verdict.gross_expectancy_bps == Decimal(1000)


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
                **GATE,
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
                **GATE,
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
                **GATE,
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
                **GATE,
            )
