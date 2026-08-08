"""The rolling walk-forward: the number that decides whether capital moves.

Every assertion here is chosen so that a plausible wrong implementation fails
it. The headline is the overlap refusal -- an implementation that accepts
overlapping test windows returns a *narrower* confidence interval on the same
evidence, which is the failure mode that looks like success.
"""

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from uuid import uuid4

import pytest

from omni.conviction.gate import MIN_RESOLVED_FOR_CALIBRATION
from omni.conviction.walk_forward_report import (
    MIN_POOLED_FOR_VERDICT,
    WindowSpec,
    rolling_windows,
    walk_forward,
    wilson_interval,
)

# Imported from the trading tier on purpose. `walk_forward_report` may not
# import it (the one-way rule), so it carries a copy; this import is what makes
# a drift between the copy and the original a test failure rather than a silent
# disagreement about which predictions were free.
from omni.trading.policy import _NOT_BACKFILLED

T0 = datetime(2026, 1, 1, tzinfo=UTC)
PERIOD = timedelta(days=30)


def _method() -> str:
    # Every query in this module filters by method, so a per-test method name
    # isolates a test from every other row in the database without truncating
    # tables other suites are using.
    return f"wfreport.test.{uuid4().hex[:12]}"


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
    resolved_at,
    hit=True,
    confidence=0.85,
    audience=None,
    backfilled=False,
):
    provenance = {"capability": method, "input_claims": [], "assumptions": {}}
    if backfilled:
        provenance["assumptions"]["backfill"] = True
    await db.pool.execute(
        """
        INSERT INTO prediction (entity_id, method, direction, confidence,
                                entry_price, upper_barrier, lower_barrier,
                                horizon_ends_at, provenance, audience_user_id,
                                outcome, resolved_at)
        VALUES ($1,$2,'up',$3,100,103,98,$4,$5::jsonb,$6,
                $7::prediction_outcome,$8)
        """,
        entity_id,
        method,
        confidence,
        resolved_at + timedelta(days=1),
        json.dumps(provenance),
        audience,
        "upper" if hit else "lower",
        resolved_at,
    )


async def _period(db, entity, *, method, index, n, hits, **kw):
    """`n` predictions resolved inside period `index`, `hits` of them correct."""
    start = T0 + PERIOD * index
    for i in range(n):
        await _resolved(
            db,
            entity,
            method=method,
            resolved_at=start + timedelta(hours=i),
            hit=i < hits,
            **kw,
        )


def _windows_after_seed(n_windows: int) -> tuple[WindowSpec, ...]:
    """Train on period 0 throughout; test on periods 1..n, one per window.

    The training range is deliberately identical across windows: only the *test*
    ranges must not overlap, and holding the training set fixed keeps the
    threshold constant so a change in the pooled rate is a change in the forward
    outcomes rather than in the gate being applied to them.
    """
    return tuple(
        WindowSpec(
            train_start=T0,
            train_end=T0 + PERIOD,
            test_start=T0 + PERIOD * i,
            test_end=T0 + PERIOD * (i + 1),
        )
        for i in range(1, n_windows + 1)
    )


class TestOverlappingTestWindowsAreRefused:
    """The headline. Overlap double-counts outcomes and narrows the interval."""

    async def test_overlapping_test_ranges_raise(self, db):
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=12)

        overlapping = (
            WindowSpec(
                train_start=T0,
                train_end=T0 + PERIOD,
                test_start=T0 + PERIOD,
                test_end=T0 + PERIOD * 3,
            ),
            WindowSpec(
                train_start=T0,
                train_end=T0 + PERIOD,
                test_start=T0 + PERIOD * 2,
                test_end=T0 + PERIOD * 4,
            ),
        )

        with pytest.raises(ValueError, match="overlap"):
            await walk_forward(
                db.pool,
                method=method,
                entity_kind="company",
                audience_user_id=None,
                windows=overlapping,
                min_per_window=10,
            )

    async def test_the_refusal_is_not_order_dependent(self, db):
        """Passing the later window first must refuse just as loudly."""
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=12)

        late = WindowSpec(
            train_start=T0,
            train_end=T0 + PERIOD,
            test_start=T0 + PERIOD * 2,
            test_end=T0 + PERIOD * 4,
        )
        early = WindowSpec(
            train_start=T0,
            train_end=T0 + PERIOD,
            test_start=T0 + PERIOD,
            test_end=T0 + PERIOD * 3,
        )

        with pytest.raises(ValueError, match="overlap"):
            await walk_forward(
                db.pool,
                method=method,
                entity_kind="company",
                audience_user_id=None,
                windows=(late, early),
                min_per_window=10,
            )

    async def test_touching_windows_are_not_overlapping(self, db):
        """[a, b) then [b, c) share the boundary and no outcome. Must not raise.

        Without this, a refusal written as `test_start <= earlier.test_end`
        would reject every contiguous walk-forward -- which is every walk-forward
        `rolling_windows` produces.
        """
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=12)
        await _period(db, entity, method=method, index=1, n=15, hits=12)
        await _period(db, entity, method=method, index=2, n=15, hits=12)

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(2),
            min_per_window=10,
        )
        assert result.pooled_n == 30

    async def test_rolling_windows_never_produce_an_overlap(self):
        windows = rolling_windows(start=T0, end=T0 + PERIOD * 10, n_windows=4)
        assert len(windows) == 4
        for earlier, later in pairwise(windows):
            assert earlier.test_end <= later.test_start
        assert windows[-1].test_end == T0 + PERIOD * 10


class TestWilsonInterval:
    """Hand-computed, and distinguishable from the normal approximation."""

    def test_thirty_of_forty_matches_the_hand_computation(self):
        """p=0.75, n=40, z=1.959963984540054 (z^2 = 3.8414588206941254).

            z^2/n            = 3.8414588206941254 / 40   = 0.09603647051735313
            denominator      = 1 + 0.09603647051735313   = 1.0960364705173532
            centre           = (0.75 + 0.04801823525867657) / 1.0960364705173532
                             = 0.7980182352586766 / 1.0960364705173532
                             = 0.7280945996506783
            p(1-p)/n         = 0.1875 / 40                = 0.0046875
            z^2/(4n^2)       = 3.8414588206941254 / 6400  = 0.0006002279407334571
            sqrt(sum)        = sqrt(0.005287727940733457) = 0.07271676385115...
            half             = (1.959963984540054 / 1.0960364705173532) * 0.0727167...
                             = 1.7882349... * 0.0727167...   = 0.1300342138583586

            lower = 0.7280945996506783 - 0.1300342138583586 = 0.5980603857923197
            upper = 0.7280945996506783 + 0.1300342138583586 = 0.8581288136090370

        Cross-checked against the other derivation of the same interval -- the
        roots of (1 + z^2/n)p^2 - (2p_hat + z^2/n)p + p_hat^2 = 0 -- which agree
        to 1e-15.
        """
        low, high = wilson_interval(30, 40)
        assert low == pytest.approx(0.5980603857923197, abs=1e-12)
        assert high == pytest.approx(0.8581288136090370, abs=1e-12)

    def test_it_is_not_the_normal_approximation(self):
        """The normal approximation gives [0.6158, 0.8842] for the same sample.

        Both are centred near 0.75 and both are about a quarter wide, so an
        assertion on the width or the centre alone would not tell them apart.
        The bounds differ in the second decimal, and Wilson's lower bound falls
        below 0.6 where the normal approximation's does not -- which is the
        difference between a strategy passing a 60% target and failing it.
        """
        low, high = wilson_interval(30, 40)
        assert low != pytest.approx(0.6158104392, abs=1e-4)
        assert high != pytest.approx(0.8841895608, abs=1e-4)
        assert low < 0.6

    def test_small_samples_stay_inside_the_unit_interval(self):
        """9 of 10: the normal approximation gives an upper bound of 1.0859."""
        low, high = wilson_interval(9, 10)
        assert high == pytest.approx(0.9821237869049271, abs=1e-12)
        assert high <= 1.0
        assert low == pytest.approx(0.5958499732047615, abs=1e-12)

    def test_a_perfect_record_is_not_certainty(self):
        """10 of 10. The normal approximation collapses to [1.0, 1.0]."""
        low, high = wilson_interval(10, 10)
        assert low == pytest.approx(0.7224672001371107, abs=1e-12)
        assert high == pytest.approx(1.0, abs=1e-12)
        assert low < 1.0

    def test_no_trials_is_no_interval(self):
        assert wilson_interval(0, 0) is None

    def test_more_hits_than_trials_raises(self):
        with pytest.raises(ValueError, match="not a sample"):
            wilson_interval(11, 10)


class TestPooledResultAcrossWindows:
    async def test_adequate_data_reports_a_pooled_rate_and_a_hand_computed_interval(
        self, db
    ):
        """4 windows x 15 forward outcomes, 12 hits each: 48 of 60.

            p = 48/60 = 0.8, n = 60, z^2 = 3.8414588206941254
            z^2/n       = 0.06402431367823542
            denominator = 1.0640243136782354
            centre      = (0.8 + 0.03201215683911771) / 1.0640243136782354
                        = 0.8320121568391177 / 1.0640243136782354
                        = 0.7819484443573731
            p(1-p)/n    = 0.16 / 60      = 0.0026666666666666666
            z^2/(4n^2)  = 3.8414588206941254 / 14400 = 0.000266768
            sqrt(sum)   = sqrt(0.0029334346...) = 0.05416119...
            half        = (1.959963984540054 / 1.0640243136782354) * 0.05416119...
                        = 1.8420115... * 0.05416119...  = 0.099766502413652

            lower = 0.7819484443573731 - 0.099766502413652 = 0.6821819419437211
            upper = 0.7819484443573731 + 0.099766502413652 = 0.8817149467710251
        """
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=12)
        for i in range(1, 5):
            await _period(db, entity, method=method, index=i, n=15, hits=12)

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(4),
            min_per_window=15,
        )

        assert len(result.windows) == 4
        assert [w.n_test for w in result.windows] == [15, 15, 15, 15]
        assert [w.hits for w in result.windows] == [12, 12, 12, 12]
        # The training decile is 0.8-0.9 at a 0.8 hit rate, so the derived
        # threshold is the bucket's lower edge and every forward prediction at
        # 0.85 clears it.
        assert [w.threshold for w in result.windows] == [0.8, 0.8, 0.8, 0.8]

        assert result.pooled_n == 60
        assert result.pooled_hits == 48
        assert result.pooled_hit_rate == pytest.approx(0.8)

        low, high = result.wilson_interval
        assert low == pytest.approx(0.6821819419437211, abs=1e-12)
        assert high == pytest.approx(0.8817149467710251, abs=1e-12)

        # The lower bound clears the 0.6 target, so the edge held.
        assert result.positive is True

    async def test_a_thin_window_contributes_to_nothing(self, db):
        """A window below the floor is not a low rate; it is no rate.

        Pooling its outcomes anyway would let five forward predictions move the
        number that authorises capital, which is the same failure
        `gate.Calibration` suppresses at the bucket level.
        """
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=12)
        await _period(db, entity, method=method, index=1, n=15, hits=12)
        await _period(db, entity, method=method, index=2, n=5, hits=0)

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(2),
            min_per_window=15,
        )

        assert [w.n_test for w in result.windows] == [15, 5]
        assert result.windows[1].hit_rate is None
        assert result.pooled_n == 15
        assert result.pooled_hits == 12
        assert result.total_test_n == 20
        assert result.pooled_hit_rate == pytest.approx(0.8)


class TestNotEnoughDataIsNotAFailure:
    async def test_below_the_floor_hit_rate_and_positive_are_both_none(self, db):
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=12)
        for i in range(1, 5):
            await _period(db, entity, method=method, index=i, n=5, hits=4)

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(4),
            min_per_window=10,
        )

        for window in result.windows:
            assert window.n_test == 5
            assert window.hit_rate is None

        assert result.pooled_hit_rate is None
        assert result.wilson_interval is None
        # `is None`, not falsy: False here would read as "tested and failed",
        # and `policy.eligible` would answer NEGATIVE_EXPECTANCY instead of
        # NO_WALK_FORWARD -- a strategy retired on evidence that was never
        # gathered.
        assert result.positive is None
        assert result.positive is not False

    async def test_a_pooled_sample_below_the_gate_floor_yields_no_verdict(self, db):
        """Ten forward outcomes, all correct: Wilson's lower bound is 0.72.

        The bound alone would pass a 0.6 target. It must not: ten outcomes are
        not thirty, and GATE B's floor exists because a small perfect run is the
        most persuasive thing an overfitted strategy produces.
        """
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=12)
        await _period(db, entity, method=method, index=1, n=10, hits=10)

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(1),
            min_per_window=10,
        )

        assert result.pooled_n == 10
        assert result.pooled_hit_rate == pytest.approx(1.0)
        assert result.wilson_interval[0] == pytest.approx(0.7224672001371107, abs=1e-12)
        assert result.pooled_n < MIN_POOLED_FOR_VERDICT
        assert result.positive is None

    async def test_a_method_with_no_history_is_unknown_not_failed(self, db):
        method = _method()
        await _entity(db)

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(4),
            min_per_window=10,
        )

        assert result.pooled_n == 0
        assert result.pooled_hit_rate is None
        assert result.positive is None


class TestAFailureOutOfSampleIsDistinctFromSilence:
    async def test_a_method_that_collapses_forward_reports_positive_false(self, db):
        """In-sample 12/15; forward 6/15 in each of four windows.

        This is the exact failure the module exists to catch: the training
        decile calibrates at 80%, the gate would have surfaced every forward
        call, and the forward calls resolved at 40%.
        """
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=12)
        for i in range(1, 5):
            await _period(db, entity, method=method, index=i, n=15, hits=6)

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(4),
            min_per_window=15,
        )

        assert result.pooled_n == 60
        assert result.pooled_hits == 24
        assert result.pooled_hit_rate == pytest.approx(0.4)
        # [0.2857, 0.5263] -- the whole interval is below the 0.6 target.
        assert result.wilson_interval[1] == pytest.approx(0.5263394650795924, abs=1e-12)
        assert result.positive is False
        assert result.positive is not None

    async def test_a_point_estimate_above_target_on_a_thin_sample_is_not_a_pass(
        self, db
    ):
        """30 forward outcomes at 70%: above the 0.6 target, inside the noise.

        Wilson's lower bound is 0.5212, so the sample cannot distinguish a 70%
        edge from a 55% one. A verdict taken from the point estimate would read
        this as validated; a verdict taken from the lower bound does not.
        """
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=12)
        await _period(db, entity, method=method, index=1, n=15, hits=11)
        await _period(db, entity, method=method, index=2, n=15, hits=10)

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(2),
            min_per_window=15,
        )

        assert result.pooled_n == 30
        assert result.pooled_hits == 21
        assert result.pooled_hit_rate == pytest.approx(0.7)
        assert result.pooled_hit_rate > 0.6
        assert result.wilson_interval[0] == pytest.approx(0.5212421254128503, abs=1e-12)
        assert result.positive is False


class TestLiveVersusBackfilledSplit:
    async def test_the_split_matches_the_policy_predicate_applied_directly(self, db):
        """The report's copy of `_NOT_BACKFILLED` must agree with the original.

        Both counts are taken over the same rows: this module's, from the
        pooled window results, and the trading tier's, by running its own
        predicate against the ledger. A drift between the copy and the original
        shows up here as a mismatch rather than as a scale phase opened on
        outcomes that risked nothing.
        """
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=12)
        # Forward windows: 10 backfilled and 5 live in each.
        for i in (1, 2):
            await _period(
                db, entity, method=method, index=i, n=10, hits=8, backfilled=True
            )
            for j in range(5):
                await _resolved(
                    db,
                    entity,
                    method=method,
                    resolved_at=T0 + PERIOD * i + timedelta(hours=12 + j),
                    hit=j < 4,
                )

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(2),
            min_per_window=15,
        )

        direct = await db.pool.fetchrow(
            f"""
            SELECT count(*) FILTER (WHERE {_NOT_BACKFILLED}) AS live_n,
                   count(*) AS total_n
            FROM prediction p
            JOIN entity e ON e.id = p.entity_id
            WHERE p.method = $1
              AND e.kind = 'company'
              AND p.outcome <> 'pending'
              AND p.resolved_at >= $2
              AND p.resolved_at < $3
            """,
            method,
            T0 + PERIOD,
            T0 + PERIOD * 3,
        )

        assert result.pooled_n == direct["total_n"] == 30
        assert result.pooled_live_n == direct["live_n"] == 10
        assert result.pooled_backfilled_n == 20
        assert result.pooled_live_hits == 8

    async def test_a_backfill_marker_at_the_top_level_also_counts_as_backfilled(
        self, db
    ):
        """`record_prediction` builds the envelope; a marker may arrive at
        either level, and the predicate checks both."""
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=12)
        for i in range(15):
            await db.pool.execute(
                """
                INSERT INTO prediction (entity_id, method, direction, confidence,
                                        entry_price, upper_barrier, lower_barrier,
                                        horizon_ends_at, provenance, outcome,
                                        resolved_at)
                VALUES ($1,$2,'up',0.85,100,103,98,$3,
                        '{"backfill": true}'::jsonb,'upper',$4)
                """,
                entity,
                method,
                T0 + PERIOD * 3,
                T0 + PERIOD + timedelta(hours=i),
            )

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(1),
            min_per_window=15,
        )

        assert result.pooled_n == 15
        assert result.pooled_live_n == 0
        assert result.pooled_backfilled_n == 15


class TestScoping:
    async def test_one_audiences_outcomes_do_not_validate_anothers_strategy(self, db):
        method, entity = _method(), await _entity(db)
        owner, stranger = uuid4(), uuid4()
        await _period(db, entity, method=method, index=0, n=15, hits=12, audience=owner)
        await _period(db, entity, method=method, index=1, n=15, hits=12, audience=owner)

        kw = {
            "method": method,
            "entity_kind": "company",
            "windows": _windows_after_seed(1),
            "min_per_window": 15,
        }

        theirs = await walk_forward(db.pool, audience_user_id=owner, **kw)
        assert theirs.pooled_n == 15

        other = await walk_forward(db.pool, audience_user_id=stranger, **kw)
        assert other.pooled_n == 0
        assert other.pooled_hit_rate is None

    async def test_a_record_on_equities_does_not_validate_crypto(self, db):
        method = _method()
        equity = await _entity(db, "company")
        await _period(db, equity, method=method, index=0, n=15, hits=12)
        await _period(db, equity, method=method, index=1, n=15, hits=12)

        kw = {
            "method": method,
            "audience_user_id": None,
            "windows": _windows_after_seed(1),
            "min_per_window": 15,
        }

        equities = await walk_forward(db.pool, entity_kind="company", **kw)
        assert equities.pooled_n == 15

        crypto = await walk_forward(db.pool, entity_kind="crypto_asset", **kw)
        assert crypto.pooled_n == 0


class TestTheThresholdComesFromTrainingOnly:
    async def test_forward_predictions_below_the_training_threshold_are_not_scored(
        self, db
    ):
        """The walk-forward measures the gate, not the producer.

        Training calibrates the 0.8-0.9 decile; the 0.3-0.4 decile never clears
        the target, so a forward prediction at 0.35 would not have been
        surfaced and must not be scored as though it had.
        """
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=12)
        await _period(db, entity, method=method, index=0, n=15, hits=3, confidence=0.35)
        await _period(db, entity, method=method, index=1, n=15, hits=15)
        await _period(db, entity, method=method, index=1, n=20, hits=0, confidence=0.35)

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(1),
            min_per_window=15,
        )

        assert result.windows[0].threshold == 0.8
        assert result.windows[0].n_train == 30
        assert result.pooled_n == 15
        assert result.pooled_hit_rate == pytest.approx(1.0)

    async def test_a_training_window_that_qualifies_nothing_scores_nothing(self, db):
        """No threshold means the gate would have surfaced nothing forward.

        An empty forward sample, not a failed one -- so `positive` stays None.
        """
        method, entity = _method(), await _entity(db)
        await _period(db, entity, method=method, index=0, n=15, hits=3)
        await _period(db, entity, method=method, index=1, n=15, hits=15)

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(1),
            min_per_window=10,
        )

        assert result.windows[0].threshold is None
        assert result.windows[0].n_test == 0
        assert result.pooled_hit_rate is None
        assert result.positive is None

    async def test_a_training_bucket_below_the_calibration_floor_sets_no_threshold(
        self, db
    ):
        method, entity = _method(), await _entity(db)
        await _period(
            db,
            entity,
            method=method,
            index=0,
            n=MIN_RESOLVED_FOR_CALIBRATION - 1,
            hits=MIN_RESOLVED_FOR_CALIBRATION - 1,
        )
        await _period(db, entity, method=method, index=1, n=15, hits=15)

        result = await walk_forward(
            db.pool,
            method=method,
            entity_kind="company",
            audience_user_id=None,
            windows=_windows_after_seed(1),
            min_per_window=10,
        )

        assert result.windows[0].n_train == MIN_RESOLVED_FOR_CALIBRATION - 1
        assert result.windows[0].threshold is None
        assert result.pooled_n == 0


class TestIncoherentInputsRefuse:
    async def test_no_windows_raises(self, db):
        with pytest.raises(ValueError, match="at least one window"):
            await walk_forward(
                db.pool,
                method=_method(),
                entity_kind="company",
                audience_user_id=None,
                windows=(),
                min_per_window=10,
            )

    async def test_a_zero_floor_raises(self, db):
        with pytest.raises(ValueError, match="min_per_window"):
            await walk_forward(
                db.pool,
                method=_method(),
                entity_kind="company",
                audience_user_id=None,
                windows=_windows_after_seed(1),
                min_per_window=0,
            )

    async def test_a_nan_target_raises_instead_of_passing_every_comparison(self, db):
        with pytest.raises(ValueError, match="NaN"):
            await walk_forward(
                db.pool,
                method=_method(),
                entity_kind="company",
                audience_user_id=None,
                windows=_windows_after_seed(1),
                min_per_window=10,
                target_hit_rate=float("nan"),
            )

    async def test_an_unreadable_ledger_raises_rather_than_reporting_zero(self):
        class DeadPool:
            async def fetch(self, *args):
                raise ConnectionError("pool is closed")

        with pytest.raises(ConnectionError):
            await walk_forward(
                DeadPool(),
                method="trend.sma",
                entity_kind="company",
                audience_user_id=None,
                windows=_windows_after_seed(1),
                min_per_window=10,
            )

    def test_a_test_range_inside_the_training_range_is_refused(self):
        with pytest.raises(ValueError, match="not out of sample"):
            WindowSpec(
                train_start=T0,
                train_end=T0 + PERIOD * 2,
                test_start=T0 + PERIOD,
                test_end=T0 + PERIOD * 3,
            )

    def test_an_inverted_test_range_is_refused(self):
        with pytest.raises(ValueError, match="test range"):
            WindowSpec(
                train_start=T0,
                train_end=T0 + PERIOD,
                test_start=T0 + PERIOD * 3,
                test_end=T0 + PERIOD * 2,
            )

    def test_rolling_windows_refuses_an_empty_span(self):
        with pytest.raises(ValueError, match="span"):
            rolling_windows(start=T0, end=T0, n_windows=4)

    def test_rolling_windows_refuses_zero_windows(self):
        with pytest.raises(ValueError, match="at least one window"):
            rolling_windows(start=T0, end=T0 + PERIOD, n_windows=0)


# --- Gate: the two hit definitions must not drift ---------------------------


class TestHitDefinitionAgreesWithTheCanonicalSql:
    """`calibration_bucket` is authoritative; every Python copy can drift.

    The older single-cutoff module `conviction/walk_forward.py` restated the
    hit rule in Python and omitted the `neutral`/`expiry` case that both
    migration 019's view and `policy._BUCKETS` include. A `neutral` call that
    expired without touching a barrier IS a hit -- asserting the price would go
    nowhere, and it going nowhere, is the assertion coming true.

    Nothing emitted `neutral` when it was found, so nothing had been mis-scored.
    It would have become a live defect the moment a neutral-emitting producer
    shipped, and it would have shown up as that producer mysteriously failing
    out of sample while its calibration bucket said otherwise.
    """

    @staticmethod
    def _row(direction: str, outcome: str):
        from types import SimpleNamespace

        return SimpleNamespace(direction=direction, outcome=outcome, confidence=0.8)

    def test_a_neutral_call_that_expired_is_a_hit(self):
        from omni.conviction.walk_forward import _is_hit

        assert _is_hit(self._row("neutral", "expiry")) is True

    def test_a_neutral_call_that_touched_a_barrier_is_not(self):
        from omni.conviction.walk_forward import _is_hit

        assert _is_hit(self._row("neutral", "upper")) is False
        assert _is_hit(self._row("neutral", "lower")) is False

    def test_the_directional_cases_are_unchanged(self):
        from omni.conviction.walk_forward import _is_hit

        assert _is_hit(self._row("up", "upper")) is True
        assert _is_hit(self._row("down", "lower")) is True
        assert _is_hit(self._row("up", "lower")) is False
        assert _is_hit(self._row("down", "upper")) is False
        assert _is_hit(self._row("up", "expiry")) is False

    def test_the_python_rule_enumerates_exactly_what_the_sql_filter_does(self):
        """Cross-check every (direction, outcome) pair against the SQL text.

        Parsed from `policy._BUCKETS` rather than restated here, so a third
        copy of the rule is not introduced by the test that exists to stop
        copies drifting.
        """
        import re

        from omni.conviction.walk_forward import _is_hit
        from omni.trading.policy import _BUCKETS

        pairs = set(
            re.findall(
                r"direction = '(\w+)'\s+AND p?\.?outcome = '(\w+)'", _BUCKETS
            )
        )
        assert pairs, "could not parse the hit rule out of policy._BUCKETS"

        for direction in ("up", "down", "neutral"):
            for outcome in ("upper", "lower", "expiry"):
                expected = (direction, outcome) in pairs
                assert _is_hit(self._row(direction, outcome)) is expected, (
                    f"{direction}/{outcome}: Python says "
                    f"{_is_hit(self._row(direction, outcome))}, SQL says {expected}"
                )
