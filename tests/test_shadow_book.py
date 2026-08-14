"""The forward shadow book, against a real database.

Every allocation number this project holds is a backtest. This is the apparatus
for the other kind of evidence, and it has exactly one job that matters: make it
impossible to improve the record after the outcome arrives. So the tests that
carry weight here are the refusals -- a decision dated into a session already
underway, an UPDATE, a DELETE, a second score -- because each of them is a way
the book could quietly become a backtest while every row still looked honest.

The arithmetic tests are built so the strategy and its benchmark cannot
coincide: a scorer that returned the benchmark, or ignored cost, or ignored
weights entirely would pass a panel where every asset moves together, and this
project has already found an optimiser no test could distinguish from equal
weight.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import asyncpg
import pandas as pd
import pytest

from omni.research.shadow_book import (
    ShadowBookRefused,
    decisions_for,
    get_decision,
    get_outcome,
    record_decision,
    record_outcome,
    score_decision,
    unscored_decisions,
    validate_weights,
)

BOOK = "etf_allocation_equal_weight"
RULE = "equal_weight/v1"
BENCH = "SPY"
UNIVERSE = ["SPY", "XLK", "XLE", "XLV"]
COST_BPS = Decimal(20)

NOW = datetime(2026, 3, 2, 22, 0, tzinfo=UTC)
EFFECTIVE = date(2026, 3, 3)


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE shadow_outcome, shadow_decision")
    yield


async def _record(db, **overrides):
    kwargs = {
        "book": BOOK,
        "rule_version": RULE,
        "effective_from": EFFECTIVE,
        "universe": UNIVERSE,
        "inputs": {"momentum_126": {"XLK": 0.21, "XLE": -0.04}},
        "weights": {"XLK": 0.5, "XLE": 0.5},
        "cost_bps": COST_BPS,
        "benchmark": BENCH,
        "now": NOW,
    }
    kwargs.update(overrides)
    return await record_decision(db.pool, **kwargs)


def _panel(rows: dict[str, list[float]], *, start=EFFECTIVE, days=4) -> pd.DataFrame:
    index = pd.to_datetime([pd.Timestamp(start) + timedelta(days=i) for i in range(days)])
    return pd.DataFrame(rows, index=index)


class TestADecisionPrecedesTheSessionItAppliesTo:
    """The point-in-time rule, in the one place it cannot be forgotten.

    A row written after the close it claims to precede is a perfect forecast and
    is indistinguishable from an honest one by inspection.
    """

    async def test_a_decision_for_the_next_session_is_recorded(self, db):
        decision = await _record(db)

        assert decision.effective_from == EFFECTIVE
        assert decision.decided_at == NOW
        assert decision.effective_from > decision.decided_at.date()

    @pytest.mark.parametrize("offset", [0, -1, -30])
    async def test_a_decision_dated_into_the_present_or_past_is_refused(
        self, db, offset
    ):
        with pytest.raises(ShadowBookRefused, match="not after"):
            await _record(db, effective_from=NOW.date() + timedelta(days=offset))

        assert await decisions_for(db.pool, BOOK) == []

    async def test_the_database_refuses_it_too_when_python_is_bypassed(self, db):
        """The check exists twice on purpose.

        The Python guard is the readable one; the constraint is the one that
        still holds when a future writer inserts directly, which is how every
        other invariant in this repository has eventually been reached.
        """
        with pytest.raises(asyncpg.IntegrityConstraintViolationError):
            await db.pool.execute(
                """
                INSERT INTO shadow_decision (
                    book, rule_version, decided_at, effective_from,
                    universe, inputs, weights, cost_bps, benchmark
                ) VALUES ($1,$2,$3,$4,$5,'{}'::jsonb,'{"XLK":1.0}'::jsonb,$6,$7)
                """,
                BOOK, RULE, NOW, NOW.date(), UNIVERSE, COST_BPS, BENCH,
            )

    async def test_a_naive_instant_is_refused_rather_than_assumed_utc(self, db):
        with pytest.raises(ShadowBookRefused, match="naive"):
            await _record(db, now=datetime(2026, 3, 2, 22, 0))  # noqa: DTZ001


class TestTheBookIsAppendOnly:
    """The property the whole exercise depends on.

    A shadow book that can be edited is a backtest wearing a costume: every
    revision looks locally justified, and the accumulated record is
    indistinguishable from a rule that was always right.
    """

    async def test_a_recorded_decision_cannot_be_updated(self, db):
        decision = await _record(db)

        with pytest.raises(asyncpg.RestrictViolationError, match="append-only"):
            await db.pool.execute(
                "UPDATE shadow_decision SET weights = '{\"XLK\":1.0}'::jsonb "
                "WHERE id = $1",
                decision.id,
            )

        stored = await get_decision(db.pool, decision.id)
        assert stored.weights == {"XLK": 0.5, "XLE": 0.5}

    async def test_a_recorded_decision_cannot_be_deleted(self, db):
        """Delete-then-insert is an update with an extra step, and it is the
        first shape anyone reaches for when the unique key conflicts."""
        decision = await _record(db)

        with pytest.raises(asyncpg.RestrictViolationError, match="append-only"):
            await db.pool.execute(
                "DELETE FROM shadow_decision WHERE id = $1", decision.id
            )

        assert await get_decision(db.pool, decision.id) is not None

    async def test_a_score_cannot_be_updated(self, db):
        decision = await _record(db)
        await record_outcome(
            db.pool,
            decision_id=decision.id,
            period_start=EFFECTIVE,
            period_end=EFFECTIVE + timedelta(days=3),
            sessions=3,
            realised_return=Decimal("-0.01"),
            benchmark_return=Decimal("0.02"),
            cost_charged=Decimal("0.002"),
            turnover=Decimal("1.0"),
        )

        with pytest.raises(asyncpg.RestrictViolationError, match="append-only"):
            await db.pool.execute(
                "UPDATE shadow_outcome SET realised_return = 0.99 "
                "WHERE decision_id = $1",
                decision.id,
            )

        stored = await get_outcome(db.pool, decision.id)
        assert stored.realised_return == Decimal("-0.01")

    async def test_scoring_the_same_decision_twice_is_refused(self, db):
        """The obvious handling of the second call -- upsert -- is exactly the
        revision the book exists to prevent, and would arrive as a bug fix."""
        decision = await _record(db)
        args = {
            "decision_id": decision.id,
            "period_start": EFFECTIVE,
            "period_end": EFFECTIVE + timedelta(days=3),
            "sessions": 3,
            "realised_return": Decimal("-0.01"),
            "benchmark_return": Decimal("0.02"),
            "cost_charged": Decimal("0.002"),
            "turnover": Decimal("1.0"),
        }
        await record_outcome(db.pool, **args)

        with pytest.raises(asyncpg.UniqueViolationError):
            await record_outcome(db.pool, **{**args, "realised_return": Decimal("0.5")})

        assert (await get_outcome(db.pool, decision.id)).realised_return == Decimal(
            "-0.01"
        )

    async def test_two_decisions_cannot_claim_the_same_session(self, db):
        await _record(db)

        with pytest.raises(asyncpg.UniqueViolationError):
            await _record(db, weights={"XLV": 1.0})

        (only,) = await decisions_for(db.pool, BOOK)
        assert only.weights == {"XLK": 0.5, "XLE": 0.5}


class TestWhatIsRecorded:
    async def test_the_row_carries_the_inputs_the_rule_saw(self, db):
        """Without them a later reader cannot tell a rule that changed from a
        market that changed."""
        decision = await _record(db)

        stored = await get_decision(db.pool, decision.id)
        assert stored.inputs == {"momentum_126": {"XLK": 0.21, "XLE": -0.04}}
        assert stored.universe == tuple(UNIVERSE)
        assert stored.rule_version == RULE
        assert stored.benchmark == BENCH
        assert stored.cost_bps == COST_BPS

    async def test_jsonb_comes_back_usable_rather_than_as_a_string(self, db):
        """asyncpg returns jsonb as a string. The first reader in this codebase
        that handled only the dict case made a populated entity render as "no
        data stored" -- honest-looking and wrong."""
        decision = await _record(db)

        stored = await get_decision(db.pool, decision.id)
        assert isinstance(stored.weights, dict)
        assert stored.weights["XLK"] == pytest.approx(0.5)
        assert isinstance(stored.inputs["momentum_126"], dict)


class TestWeightsAreHoldable:
    def test_weights_summing_to_one_across_thirds_are_accepted(self):
        """1/3 three times sums to 0.9999999999999999. An equality check against
        1.0 would refuse the correct answer."""
        validate_weights({"A": 1 / 3, "B": 1 / 3, "C": 1 / 3}, ["A", "B", "C"])

    def test_cash_is_allowed_because_a_rule_may_hold_some(self):
        validate_weights({"A": 0.4}, ["A", "B"])

    def test_leverage_is_refused(self):
        with pytest.raises(ShadowBookRefused, match="leverage"):
            validate_weights({"A": 0.7, "B": 0.7}, ["A", "B"])

    def test_a_short_leg_is_refused(self):
        with pytest.raises(ShadowBookRefused, match="long-only"):
            validate_weights({"A": 1.2, "B": -0.2}, ["A", "B"])

    def test_a_name_outside_the_universe_is_refused(self):
        with pytest.raises(ShadowBookRefused, match="universe does not"):
            validate_weights({"C": 1.0}, ["A", "B"])

    def test_nan_is_refused_rather_than_compared(self):
        """Every comparison against NaN is false, so the range checks above pass
        it through and the sum becomes NaN rather than too large."""
        with pytest.raises(ShadowBookRefused, match="nan"):
            validate_weights({"A": float("nan")}, ["A"])


class TestScoring:
    """Measured against the benchmark, net of the cost recorded at decision time."""

    async def test_a_decision_is_scored_on_its_own_weights_not_the_benchmarks(self, db):
        """The panel moves the holdings and the benchmark in opposite
        directions, so a scorer that returned the benchmark, or ignored the
        weights, cannot produce this answer."""
        decision = await _record(db, weights={"XLK": 1.0}, cost_bps=Decimal(0))
        panel = _panel({
            "XLK": [100.0, 110.0, 121.0, 133.1],
            "SPY": [100.0, 95.0, 90.25, 85.7375],
            "XLE": [100.0, 100.0, 100.0, 100.0],
            "XLV": [100.0, 100.0, 100.0, 100.0],
        })

        score = score_decision(decision, panel)

        assert score.realised_return == pytest.approx(0.331, abs=1e-6)
        assert score.benchmark_return == pytest.approx(-0.142625, abs=1e-6)
        assert score.sessions == 3

    async def test_cost_scales_the_book_rather_than_being_subtracted_from_it(
        self, db
    ):
        """A cost is a fraction of capital, not a fraction of the return.

        Charging it *before* or *after* a single rebalance is the same number --
        multiplication commutes -- so the ordering is not what needs pinning.
        What does is multiplicative against additive: subtracting 1% from a
        33.1% return gives 32.1%, while paying 1% of the book and compounding
        the rest gives 31.769%. The additive form flatters, and it grows more
        flattering the better the period was.
        """
        decision = await _record(db, weights={"XLK": 1.0}, cost_bps=Decimal(100))
        rising = _panel({
            "XLK": [100.0, 110.0, 121.0, 133.1],
            "SPY": [100.0] * 4,
            "XLE": [100.0] * 4,
            "XLV": [100.0] * 4,
        })

        score = score_decision(decision, rising)

        # One full entry from cash at 100 bps: 0.99 * 1.331 - 1.
        assert score.turnover == pytest.approx(1.0)
        assert score.cost_charged == pytest.approx(0.01)
        assert score.realised_return == pytest.approx(0.31769, abs=1e-9)
        assert score.realised_return != pytest.approx(0.321, abs=1e-9)

    async def test_a_flat_period_returns_exactly_the_cost(self, db):
        decision = await _record(db, weights={"XLK": 1.0}, cost_bps=Decimal(100))
        flat = _panel({
            "XLK": [100.0] * 4, "SPY": [100.0] * 4,
            "XLE": [100.0] * 4, "XLV": [100.0] * 4,
        })

        score = score_decision(decision, flat)

        assert score.realised_return == pytest.approx(-0.01)
        assert score.benchmark_return == pytest.approx(0.0)

    async def test_turnover_from_a_prior_decision_is_cheaper_than_from_cash(self, db):
        decision = await _record(db, weights={"XLK": 1.0}, cost_bps=Decimal(100))
        flat = _panel({
            "XLK": [100.0] * 4, "SPY": [100.0] * 4,
            "XLE": [100.0] * 4, "XLV": [100.0] * 4,
        })

        from_cash = score_decision(decision, flat)
        held = score_decision(decision, flat, previous_weights={"XLK": 1.0})

        assert from_cash.turnover == pytest.approx(1.0)
        assert held.turnover == pytest.approx(0.0)
        assert held.cost_charged == pytest.approx(0.0)
        assert "turnover_from" in from_cash.limits
        assert "turnover_from" not in held.limits

    async def test_a_missing_mark_refuses_the_score(self, db):
        """Dropping the name would price a liquidation nobody observed, which is
        the most flattering thing an incomplete panel can be made to say."""
        decision = await _record(db, weights={"XLK": 0.5, "XLE": 0.5})
        holed = _panel({
            "XLK": [100.0, 101.0, float("nan"), 103.0],
            "XLE": [100.0, 100.0, 100.0, 100.0],
            "SPY": [100.0, 100.0, 100.0, 100.0],
            "XLV": [100.0] * 4,
        })

        with pytest.raises(ShadowBookRefused, match="missing marks"):
            score_decision(decision, holed)

    async def test_a_held_name_absent_from_the_panel_refuses_the_score(self, db):
        decision = await _record(db, weights={"XLK": 1.0})
        without = _panel({"SPY": [100.0] * 4, "XLE": [100.0] * 4})

        with pytest.raises(ShadowBookRefused, match="no column for XLK"):
            score_decision(decision, without)

    async def test_a_single_session_is_not_scored_as_a_flat_period(self, db):
        decision = await _record(db, weights={"XLK": 1.0})
        one = _panel({"XLK": [100.0], "SPY": [100.0]}, days=1)

        with pytest.raises(ShadowBookRefused, match="needs two marks"):
            score_decision(decision, one)

    async def test_sessions_before_the_decision_are_not_scored(self, db):
        """The window opens at `effective_from`. Including earlier sessions
        would credit the book with returns it was not holding for."""
        decision = await _record(db, weights={"XLK": 1.0}, cost_bps=Decimal(0))
        panel = _panel(
            {"XLK": [1.0, 100.0, 110.0, 121.0], "SPY": [100.0] * 4},
            start=EFFECTIVE - timedelta(days=1),
        )

        score = score_decision(decision, panel)

        assert score.period_start == EFFECTIVE
        assert score.sessions == 2
        assert score.realised_return == pytest.approx(0.21, abs=1e-9)


class TestTheScoringPass:
    async def test_a_decision_whose_window_has_not_opened_is_not_offered(self, db):
        """Scored over an empty window it would record a real zero."""
        await _record(db)

        assert await unscored_decisions(
            db.pool, BOOK, through=EFFECTIVE - timedelta(days=1)
        ) == []
        assert len(await unscored_decisions(db.pool, BOOK, through=EFFECTIVE)) == 1

    async def test_a_scored_decision_is_not_offered_again(self, db):
        decision = await _record(db)
        await record_outcome(
            db.pool,
            decision_id=decision.id,
            period_start=EFFECTIVE,
            period_end=EFFECTIVE + timedelta(days=3),
            sessions=3,
            realised_return=Decimal("0.01"),
            benchmark_return=Decimal("0.02"),
            cost_charged=Decimal("0.002"),
            turnover=Decimal("1.0"),
        )

        assert await unscored_decisions(db.pool, BOOK, through=EFFECTIVE) == []

    async def test_a_zero_session_score_is_refused(self, db):
        decision = await _record(db)

        with pytest.raises(ShadowBookRefused, match="measures nothing"):
            await record_outcome(
                db.pool,
                decision_id=decision.id,
                period_start=EFFECTIVE,
                period_end=EFFECTIVE,
                sessions=0,
                realised_return=Decimal(0),
                benchmark_return=Decimal(0),
                cost_charged=Decimal(0),
                turnover=Decimal(0),
            )

    async def test_excess_return_is_the_number_the_book_exists_to_produce(self, db):
        decision = await _record(db)
        outcome = await record_outcome(
            db.pool,
            decision_id=decision.id,
            period_start=EFFECTIVE,
            period_end=EFFECTIVE + timedelta(days=3),
            sessions=3,
            realised_return=Decimal("0.0140"),
            benchmark_return=Decimal("0.0200"),
            cost_charged=Decimal("0.0020"),
            turnover=Decimal("1.0"),
        )

        assert outcome.excess_return == Decimal("-0.0060")

    async def test_one_books_decisions_are_not_read_from_another(self, db):
        await _record(db)
        await _record(db, book="etf_allocation_risk_balanced")

        assert len(await decisions_for(db.pool, BOOK)) == 1
        assert len(await decisions_for(db.pool, "etf_allocation_risk_balanced")) == 1
