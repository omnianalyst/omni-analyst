"""The forward shadow book: allocation decisions written before they apply.

Every allocation result this project holds is a backtest, and
`docs/ETF_PORTFOLIO_EXPERIMENT.md` states why its own numbers are not
decision-grade. This module is the other kind of evidence, and the only kind
that cannot be manufactured later: a record of what a rule chose, on the day it
chose it, before the outcome existed.

Three properties do the work, and each exists because the obvious alternative
fails quietly:

**A decision is written before the session it applies to.** `record_decision`
refuses an `effective_from` that is not strictly after the day it is called, and
the database refuses it again. A decision written after the close it claims to
precede is a perfect forecast that looks like every other row.

**Nothing is ever revised.** The tables are append-only by trigger. A shadow
book that can be edited is a backtest wearing a costume: each revision looks
locally justified, and the accumulated record is indistinguishable from a rule
that was always right.

**Scoring is a separate pass and reads only what was recorded.** `score` takes
the weights off the stored row rather than recomputing them, so a rule change
cannot retroactively improve a decision the old rule made. It is the discipline
the prediction ledger already applies, for the same reason.

What this module deliberately does not do is decide anything. It receives
weights and the measurements behind them; choosing them is the caller's job, and
keeping that seam means a new allocation rule needs no change here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

# Weights are compared against 1.0 with a tolerance, never with `==`. A rule
# that divides 1.0 across three names produces 0.9999999999999999, and an
# equality check would refuse the correct answer while accepting a rule that
# happened to land on representable thirds. The tolerance is on the sum itself,
# which is scale-consistent because these are fractions of one book.
WEIGHT_SUM_TOLERANCE = 1e-9

# A book may hold cash, so weights need not sum to 1. They may not sum to more
# than 1: that is leverage, which this book does not model, and a rule that
# produced it would be silently levered rather than obviously wrong.
MAX_INVESTED = 1.0


class ShadowBookRefused(Exception):
    """The decision was not recorded, and the book is unchanged."""


@dataclass(frozen=True)
class Decision:
    id: UUID
    book: str
    rule_version: str
    decided_at: datetime
    effective_from: date
    universe: tuple[str, ...]
    inputs: dict[str, Any]
    weights: dict[str, float]
    cost_bps: Decimal
    benchmark: str
    note: str | None


@dataclass(frozen=True)
class Outcome:
    decision_id: UUID
    period_start: date
    period_end: date
    sessions: int
    realised_return: Decimal
    benchmark_return: Decimal
    cost_charged: Decimal
    turnover: Decimal
    limits: dict[str, Any]

    @property
    def excess_return(self) -> Decimal:
        return self.realised_return - self.benchmark_return


_INSERT_DECISION = """
INSERT INTO shadow_decision (
    book, rule_version, decided_at, effective_from,
    universe, inputs, weights, cost_bps, benchmark, note
) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,$10)
RETURNING id, decided_at
"""

_SELECT_DECISION = """
SELECT id, book, rule_version, decided_at, effective_from,
       universe, inputs, weights, cost_bps, benchmark, note
FROM shadow_decision
WHERE id = $1
"""

_SELECT_BOOK = """
SELECT id, book, rule_version, decided_at, effective_from,
       universe, inputs, weights, cost_bps, benchmark, note
FROM shadow_decision
WHERE book = $1
ORDER BY effective_from
"""

_INSERT_OUTCOME = """
INSERT INTO shadow_outcome (
    decision_id, period_start, period_end, sessions,
    realised_return, benchmark_return, cost_charged, turnover, limits
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
"""

_SELECT_OUTCOME = """
SELECT decision_id, period_start, period_end, sessions,
       realised_return, benchmark_return, cost_charged, turnover, limits
FROM shadow_outcome
WHERE decision_id = $1
"""

_UNSCORED = """
SELECT d.id, d.book, d.rule_version, d.decided_at, d.effective_from,
       d.universe, d.inputs, d.weights, d.cost_bps, d.benchmark, d.note
FROM shadow_decision d
LEFT JOIN shadow_outcome o ON o.decision_id = d.id
WHERE d.book = $1 AND o.decision_id IS NULL AND d.effective_from <= $2
ORDER BY d.effective_from
"""


def _json(value: Any) -> Any:
    """asyncpg hands `jsonb` back as a string, not a dict.

    Documented in `docs/OMNI_ANALYST.md` as the bug that made a populated entity
    render as "no data stored": the first reader handled only the dict case, so
    every value silently became None. Every new reader has to handle both.
    """
    if isinstance(value, str):
        return json.loads(value)
    return value


def _row_to_decision(row) -> Decision:
    return Decision(
        id=row["id"],
        book=row["book"],
        rule_version=row["rule_version"],
        decided_at=row["decided_at"],
        effective_from=row["effective_from"],
        universe=tuple(row["universe"]),
        inputs=_json(row["inputs"]),
        weights={k: float(v) for k, v in _json(row["weights"]).items()},
        cost_bps=row["cost_bps"],
        benchmark=row["benchmark"],
        note=row["note"],
    )


def validate_weights(weights: dict[str, float], universe: list[str]) -> None:
    """Refuse a weight vector that does not describe a holdable book.

    Each of these has a silent failure if it is not checked here: a negative
    weight is a short this book does not model and would be simulated as a
    positive contribution with the wrong sign; a name outside the universe means
    the rule chose something it was not offered, so the universe on the row is
    no longer the record of what it saw; weights summing above one is leverage
    reported as an unlevered return.
    """
    if not weights:
        raise ShadowBookRefused("a decision with no weights records nothing")

    outside = sorted(set(weights) - set(universe))
    if outside:
        raise ShadowBookRefused(
            f"weights name {', '.join(outside)}, which the universe does not "
            f"offer. The universe is the record of what the rule could choose "
            f"from; a weight outside it makes that record false"
        )

    negative = sorted(name for name, w in weights.items() if w < 0.0)
    if negative:
        raise ShadowBookRefused(
            f"negative weight on {', '.join(negative)}. This book is long-only "
            f"and a short leg would be simulated with the wrong sign rather "
            f"than refused"
        )

    for name, weight in weights.items():
        if not math.isfinite(weight):
            raise ShadowBookRefused(
                f"weight on {name} is {weight}; every comparison against NaN is "
                f"false, so an unguarded range check would pass it straight "
                f"through and the sum below would be NaN rather than too large"
            )

    invested = sum(weights.values())
    if invested > MAX_INVESTED + WEIGHT_SUM_TOLERANCE:
        raise ShadowBookRefused(
            f"weights sum to {invested:.6f}, above 1. That is leverage, which "
            f"this book does not model; its return would be reported as an "
            f"unlevered one"
        )


async def record_decision(
    pool,
    *,
    book: str,
    rule_version: str,
    effective_from: date,
    universe: list[str],
    inputs: dict[str, Any],
    weights: dict[str, float],
    cost_bps: Decimal,
    benchmark: str,
    note: str | None = None,
    now: datetime | None = None,
) -> Decision:
    """Write one allocation decision, before the session it applies to.

    `now` exists so a caller can state the instant rather than inherit a clock,
    which is the same reason `run_carry_cycle` refuses to default `as_of`. It
    does not let a caller backdate: the check below and the database constraint
    both compare `effective_from` against it, so a backdated `now` produces a
    decision that claims to precede a session it also claims to be written
    after, and the constraint refuses it.
    """
    at = now or datetime.now(UTC)
    if at.tzinfo is None:
        raise ShadowBookRefused(
            f"now is naive ({at}); a decision's precedence over the session it "
            f"applies to is not decidable without a timezone"
        )
    if effective_from <= at.astimezone(UTC).date():
        raise ShadowBookRefused(
            f"effective_from {effective_from} is not after {at.astimezone(UTC).date()}, "
            f"the day this decision is being written. A score at t executes no "
            f"earlier than the following session; a decision recorded for a "
            f"session already underway is a forecast of a known outcome"
        )
    validate_weights(weights, universe)

    row = await pool.fetchrow(
        _INSERT_DECISION,
        book,
        rule_version,
        at,
        effective_from,
        list(universe),
        json.dumps(inputs),
        json.dumps(weights),
        cost_bps,
        benchmark,
        note,
    )
    return Decision(
        id=row["id"],
        book=book,
        rule_version=rule_version,
        decided_at=row["decided_at"],
        effective_from=effective_from,
        universe=tuple(universe),
        inputs=inputs,
        weights=dict(weights),
        cost_bps=cost_bps,
        benchmark=benchmark,
        note=note,
    )


async def get_decision(pool, decision_id: UUID) -> Decision | None:
    row = await pool.fetchrow(_SELECT_DECISION, decision_id)
    return None if row is None else _row_to_decision(row)


async def decisions_for(pool, book: str) -> list[Decision]:
    rows = await pool.fetch(_SELECT_BOOK, book)
    return [_row_to_decision(row) for row in rows]


async def unscored_decisions(pool, book: str, *, through: date) -> list[Decision]:
    """Decisions whose period has begun and which carry no score yet.

    Bounded by `through` so a scoring pass cannot reach a decision that has not
    started, which would otherwise be scored over an empty window and recorded
    as a real zero.
    """
    rows = await pool.fetch(_UNSCORED, book, through)
    return [_row_to_decision(row) for row in rows]


async def record_outcome(
    pool,
    *,
    decision_id: UUID,
    period_start: date,
    period_end: date,
    sessions: int,
    realised_return: Decimal,
    benchmark_return: Decimal,
    cost_charged: Decimal,
    turnover: Decimal,
    limits: dict[str, Any] | None = None,
) -> Outcome:
    """Score a decision once.

    A second call for the same decision violates the primary key and is refused
    by the database rather than handled here. That is deliberate: the obvious
    handling -- upsert -- is exactly the revision the book exists to prevent,
    and it would arrive looking like a bug fix.
    """
    if sessions <= 0:
        raise ShadowBookRefused(
            f"a score over {sessions} sessions measures nothing; a decision "
            f"whose window has not opened is unscored, not flat"
        )
    stated = dict(limits or {})
    await pool.execute(
        _INSERT_OUTCOME,
        decision_id,
        period_start,
        period_end,
        sessions,
        realised_return,
        benchmark_return,
        cost_charged,
        turnover,
        json.dumps(stated),
    )
    return Outcome(
        decision_id=decision_id,
        period_start=period_start,
        period_end=period_end,
        sessions=sessions,
        realised_return=realised_return,
        benchmark_return=benchmark_return,
        cost_charged=cost_charged,
        turnover=turnover,
        limits=stated,
    )


@dataclass(frozen=True)
class Score:
    """What a decision earned over its window, and what the baseline earned.

    Returned rather than written so the caller decides whether the window is
    complete enough to commit. `record_outcome` is the only writer, and it can
    only be called once per decision.
    """

    period_start: date
    period_end: date
    sessions: int
    realised_return: float
    benchmark_return: float
    cost_charged: float
    turnover: float
    limits: dict[str, Any]


def score_decision(
    decision: Decision,
    prices,
    *,
    previous_weights: dict[str, float] | None = None,
    period_end: date | None = None,
) -> Score:
    """Measure one decision against its declared benchmark, net of its cost.

    `prices` is an adjusted-close panel indexed by session, supplied by the
    caller; this module never fetches. The window opens at `effective_from` --
    the first session the weights applied to -- and the cost is charged before
    the first return, matching `etf_replication`'s decision rule so the forward
    record and the backtest are measuring the same thing.

    **A missing mark on a held name refuses the score.** The alternative is to
    drop the name or forward-fill it, and both turn an unknown into a number: a
    dropped holding is a costless liquidation at an unobserved price, which is
    the single most flattering thing an incomplete panel can be made to say.
    The decision simply stays unscored until the panel is complete, which is
    recoverable; a wrong score written into an append-only table is not.
    """
    import pandas as pd

    from omni.research.etf_replication import turnover_between

    held = sorted(decision.weights)
    needed = [*held, decision.benchmark]
    missing_columns = [s for s in needed if s not in prices.columns]
    if missing_columns:
        raise ShadowBookRefused(
            f"the panel has no column for {', '.join(missing_columns)}; a held "
            f"name without a price is unscorable, and dropping it would price "
            f"a liquidation nobody observed"
        )

    panel = prices.loc[:, needed].sort_index()
    start = pd.Timestamp(decision.effective_from)
    window = panel.loc[panel.index >= start]
    if period_end is not None:
        window = window.loc[window.index <= pd.Timestamp(period_end)]
    if len(window) < 2:
        raise ShadowBookRefused(
            f"the panel holds {len(window)} session(s) from "
            f"{decision.effective_from}; a return needs two marks, and one "
            f"session would score as a flat period rather than as no period"
        )

    daily = window.pct_change(fill_method=None).iloc[1:]
    gaps = daily.isna().any()
    unmarked = sorted(gaps.index[gaps])
    if unmarked:
        raise ShadowBookRefused(
            f"missing marks for {', '.join(unmarked)} between "
            f"{window.index[0].date()} and {window.index[-1].date()}; the "
            f"decision stays unscored rather than being scored over a panel "
            f"with holes in it"
        )

    target = pd.Series(decision.weights, dtype=float)
    prior = pd.Series(previous_weights or {}, dtype=float)
    moved = turnover_between(prior, target)
    cost = moved * float(decision.cost_bps) / 10_000.0

    weights = target.copy()
    value = 1.0 - cost
    for _, row in daily.iterrows():
        step = row.reindex(weights.index)
        value *= 1.0 + float(weights @ step)
        grown = weights * (1.0 + step)
        weights = grown / float(grown.sum())

    benchmark_path = window[decision.benchmark]
    benchmark_return = float(benchmark_path.iloc[-1] / benchmark_path.iloc[0] - 1.0)

    limits: dict[str, Any] = {}
    if period_end is None:
        limits["window"] = (
            "scored to the end of the supplied panel; the period is open and a "
            "later score of the same decision is refused by design"
        )
    if previous_weights is None:
        limits["turnover_from"] = (
            "cash -- no prior decision was supplied, so this is charged as a "
            "full entry rather than as a rebalance"
        )

    return Score(
        period_start=window.index[0].date(),
        period_end=window.index[-1].date(),
        sessions=len(daily),
        realised_return=value - 1.0,
        benchmark_return=benchmark_return,
        cost_charged=cost,
        turnover=moved,
        limits=limits,
    )


async def get_outcome(pool, decision_id: UUID) -> Outcome | None:
    row = await pool.fetchrow(_SELECT_OUTCOME, decision_id)
    if row is None:
        return None
    return Outcome(
        decision_id=row["decision_id"],
        period_start=row["period_start"],
        period_end=row["period_end"],
        sessions=row["sessions"],
        realised_return=row["realised_return"],
        benchmark_return=row["benchmark_return"],
        cost_charged=row["cost_charged"],
        turnover=row["turnover"],
        limits=_json(row["limits"]),
    )


__all__ = [
    "Decision",
    "Outcome",
    "Score",
    "ShadowBookRefused",
    "decisions_for",
    "get_decision",
    "get_outcome",
    "record_decision",
    "record_outcome",
    "score_decision",
    "unscored_decisions",
    "validate_weights",
]
