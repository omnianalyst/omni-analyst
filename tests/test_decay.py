"""The edge decay monitor: forward judgement of the shadow book's edges."""

from __future__ import annotations

from datetime import UTC, date, timedelta
from decimal import Decimal

import pytest

from omni.research.decay import (
    DECAY_ALPHA,
    MIN_SCORED_OUTCOMES,
    Outcome,
    evaluate_edge,
    latest_edge_states,
    outcomes_for,
    record_edge_state,
)

DAY = date(2026, 8, 18)
BOOK = "etf_tsmom_252"


def _outcomes(excesses: list[float], *, sessions: int = 1) -> list[Outcome]:
    start = DAY - timedelta(days=len(excesses))
    return [
        Outcome(
            period_start=start + timedelta(days=i),
            period_end=start + timedelta(days=i),
            sessions=sessions,
            excess=Decimal(str(e)),
        )
        for i, e in enumerate(excesses)
    ]


def test_a_short_record_is_insufficient_and_refuses_numbers():
    evaluation = evaluate_edge(BOOK, DAY, _outcomes([0.001] * 5))

    assert evaluation.state == "insufficient"
    assert evaluation.scored_sessions == 5
    assert evaluation.mean_session_excess is None
    assert evaluation.decay_p is None
    assert evaluation.window_start is None
    assert str(MIN_SCORED_OUTCOMES) in evaluation.reason
    assert "5" in evaluation.reason


def test_a_positive_recent_third_holds():
    excesses = [0.0] * 20 + [0.004] * 10
    evaluation = evaluate_edge(BOOK, DAY, _outcomes(excesses))

    assert evaluation.state == "holding"
    assert evaluation.mean_session_excess == Decimal("0.004")
    assert evaluation.recent_sessions == 10
    assert evaluation.window_start is not None and evaluation.window_end is not None


def test_a_consistently_negative_recent_third_is_decayed():
    excesses = [0.0] * 20 + [-0.004] * 10
    evaluation = evaluate_edge(BOOK, DAY, _outcomes(excesses))

    assert evaluation.state == "decayed"
    assert evaluation.mean_session_excess == Decimal("-0.004")
    assert float(evaluation.decay_p) <= DECAY_ALPHA


def test_a_noisy_negative_mean_is_unconfirmed_not_decayed():
    rng_excesses = [0.0] * 20 + [
        -0.30, 0.29, -0.28, 0.27, -0.26, 0.25, -0.24, 0.23, -0.22, -0.01,
    ]
    evaluation = evaluate_edge(BOOK, DAY, _outcomes(rng_excesses))

    assert evaluation.mean_session_excess < 0
    assert evaluation.state == "unconfirmed"


def test_a_constant_positive_excess_does_not_fabricate_decay():
    evaluation = evaluate_edge(BOOK, DAY, _outcomes([0.0] * 20 + [0.002] * 10))

    assert evaluation.state == "holding"
    assert float(evaluation.decay_p) > DECAY_ALPHA


def test_the_judgement_is_reproducible_night_after_night():
    excesses = [0.0] * 20 + [-0.002, 0.001] * 5
    first = evaluate_edge(BOOK, DAY, _outcomes(excesses))
    second = evaluate_edge(BOOK, DAY, _outcomes(excesses))

    assert first.decay_p == second.decay_p
    assert first.state == second.state


def test_a_non_finite_excess_refuses_rather_than_judges():
    outcomes = _outcomes([0.001] * 29) + [
        Outcome(period_start=DAY, period_end=DAY, sessions=1, excess=Decimal("NaN"))
    ]
    with pytest.raises(Exception) as raised:
        evaluate_edge(BOOK, DAY, outcomes)
    assert "non-finite" in str(raised.value)


def test_only_the_declared_books_are_promoted():
    evaluation = evaluate_edge("etf_equal_weight_sectors", DAY, _outcomes([0.001] * 30))
    assert evaluation.promoted is False
    assert evaluate_edge(BOOK, DAY, _outcomes([0.001] * 30)).promoted is True


async def test_state_rows_are_idempotent_per_night_and_append_only(db):
    from asyncpg.exceptions import RaiseError

    evaluation = evaluate_edge(BOOK, DAY, _outcomes([0.001] * 30))
    assert await record_edge_state(db.pool, evaluation) is True
    assert await record_edge_state(db.pool, evaluation) is False

    with pytest.raises(RaiseError, match="append-only"):
        await db.pool.execute("UPDATE shadow_edge_state SET state = 'holding'")
    with pytest.raises(RaiseError, match="append-only"):
        await db.pool.execute("DELETE FROM shadow_edge_state")

    rows = await latest_edge_states(db.pool)
    assert [r.book for r in rows] == [BOOK]
    assert rows[0].state == evaluation.state
    assert rows[0].evaluated_at is not None


async def test_the_latest_night_wins_per_book(db):
    first = evaluate_edge(BOOK, DAY, _outcomes([0.001] * 30))
    await record_edge_state(db.pool, first)
    later = evaluate_edge(
        BOOK, DAY + timedelta(days=1), _outcomes([0.001] * 29 + [-0.004])
    )
    await record_edge_state(db.pool, later)

    rows = await latest_edge_states(db.pool)
    assert len(rows) == 1
    assert rows[0].as_of == later.as_of


async def test_outcomes_read_the_forward_excess_net_of_benchmark(db):
    from datetime import datetime

    from omni.research.shadow_book import record_decision

    decision = await record_decision(
        db.pool,
        book="etf_equal_weight_sectors",
        rule_version="equal_weight/v1",
        effective_from=DAY + timedelta(days=1),
        universe=["XLK"],
        inputs={},
        weights={"XLK": 1.0},
        cost_bps=Decimal(2),
        benchmark="SPY",
        note=None,
        now=datetime(2026, 8, 18, 22, tzinfo=UTC),
    )
    await db.pool.execute(
        """
        INSERT INTO shadow_outcome
            (decision_id, period_start, period_end, sessions,
             realised_return, benchmark_return, cost_charged, turnover)
        VALUES ($1, $2, $3, 1, 0.012, 0.004, 0.0002, 1.0)
        """,
        decision.id,
        DAY + timedelta(days=1),
        DAY + timedelta(days=2),
    )

    outcomes = await outcomes_for(db.pool, "etf_equal_weight_sectors")
    assert len(outcomes) == 1
    assert outcomes[0].excess == Decimal("0.008")
    assert outcomes[0].sessions == 1
