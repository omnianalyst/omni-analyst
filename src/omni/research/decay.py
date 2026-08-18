"""The edge decay monitor: does a promoted edge still hold, forward?

The shadow book records decisions and scores them; this module turns the
scored record into the judgement an operator actually needs -- is the edge
that justified promoting this rule still present in the recent window? The
research harness asks that question once, at promotion, on history. This asks
it every night, on the forward record, where the answer can only change by
accumulating sessions that have already happened.

Three honest states plus refusal, and the distinctions are the point:

``holding``       the recent third's mean session excess is positive.
``unconfirmed``   excess is non-positive but not significantly negative.
                  A quiet edge is not a dead edge, and conflating the two
                  burns operator trust the first time a real edge breathes.
``decayed``       excess is significantly negative under a sign-flip null.
                  The promoted claim has reversed, not merely paused.
``insufficient``  not enough scored outcomes to say anything; numbers are
                  refused rather than computed on a window that cannot
                  support them.

The statistic mirrors the harness's discipline on a single series: the most
recent third, never the full sample, because every rule this project has
retired was significant full-sample. The null is a sign-flip permutation of
the same window -- calibrated on the data it judges, the same principle that
makes crypto's own null run at 2.0-2.3 rather than 1.96 -- and the seed is
derived from (book, as_of) through SHA-256 so a night's judgement is
reproducible byte for byte. A monitor whose alert depends on when it happened
to run is not a monitor.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy as np

PROMOTED_BOOKS = frozenset({"etf_tsmom_252"})

MIN_SCORED_OUTCOMES = 30
PERMUTATIONS = 2_000
DECAY_ALPHA = 0.05


class DecayRefused(Exception):
    """The evaluation cannot be made honestly; no state row is written."""


@dataclass(frozen=True)
class Outcome:
    period_start: date
    period_end: date
    sessions: int
    excess: Decimal


@dataclass(frozen=True)
class EdgeEvaluation:
    book: str
    as_of: date
    promoted: bool
    state: str
    scored_sessions: int
    recent_sessions: int
    mean_session_excess: Decimal | None
    decay_p: Decimal | None
    window_start: date | None
    window_end: date | None
    reason: str | None
    evaluated_at: object | None = None


def _seed(book: str, as_of: date) -> int:
    digest = hashlib.sha256(f"{book}:{as_of.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def evaluate_edge(book: str, as_of: date, outcomes: list[Outcome]) -> EdgeEvaluation:
    """Judge one book's forward record as of one night.

    ``outcomes`` must be ordered oldest-first, as ``outcomes_for`` returns
    them. Raises ``DecayRefused`` -- writing nothing -- if any excess is
    non-finite: the outcome CHECKs refuse NaN at the database, so a
    non-finite value here means upstream arithmetic is broken, and a monitor
    that papered over it with a clipped number would flatter the book.
    """
    scored_sessions = sum(o.sessions for o in outcomes)
    promoted = book in PROMOTED_BOOKS

    if len(outcomes) < MIN_SCORED_OUTCOMES:
        return EdgeEvaluation(
            book=book,
            as_of=as_of,
            promoted=promoted,
            state="insufficient",
            scored_sessions=scored_sessions,
            recent_sessions=0,
            mean_session_excess=None,
            decay_p=None,
            window_start=None,
            window_end=None,
            reason=(
                f"{len(outcomes)} scored outcome(s) against the "
                f"{MIN_SCORED_OUTCOMES} this monitor needs; the recent third "
                f"of a shorter record is not evidence about an edge"
            ),
        )

    values = np.array([float(o.excess) for o in outcomes], dtype=float)
    if not np.all(np.isfinite(values)):
        bad = next(
            o for o in outcomes
            if not math.isfinite(float(o.excess))
        )
        raise DecayRefused(
            f"outcome for {bad.period_start} -> {bad.period_end} carries a "
            f"non-finite excess; the forward record is corrupt and this "
            f"monitor refuses to judge it"
        )

    recent_n = math.ceil(len(outcomes) / 3)
    recent = values[-recent_n:]
    observed = float(recent.mean())

    # The reported mean is exact Decimal arithmetic on the same inputs. The
    # permutation null below runs in float64 -- it must, to flip two thousand
    # resamples a night -- and a float mean of 0.004 comes back as
    # 0.004000000000000001. Storing that as the headline number is the
    # fabricated-precision trap this codebase documents elsewhere; the mean
    # of recorded Decimals has an exact answer and it is reported instead.
    recent_decimals = [o.excess for o in outcomes[-recent_n:]]
    exact_mean = sum(recent_decimals) / len(recent_decimals)

    rng = np.random.default_rng(_seed(book, as_of))
    flips = rng.choice(np.array([-1.0, 1.0]), size=(PERMUTATIONS, recent_n))
    permuted_means = (flips * recent).mean(axis=1)
    at_or_below = int(np.count_nonzero(permuted_means <= observed))
    p_lower = (at_or_below + 1) / (PERMUTATIONS + 1)

    if exact_mean < 0 and p_lower <= DECAY_ALPHA:
        state = "decayed"
    elif exact_mean > 0:
        state = "holding"
    else:
        state = "unconfirmed"

    window = outcomes[-recent_n:]
    return EdgeEvaluation(
        book=book,
        as_of=as_of,
        promoted=promoted,
        state=state,
        scored_sessions=scored_sessions,
        recent_sessions=sum(o.sessions for o in window),
        mean_session_excess=exact_mean,
        decay_p=Decimal(str(round(p_lower, 6))),
        window_start=window[0].period_start,
        window_end=window[-1].period_end,
        reason=None,
    )


_OUTCOMES = """
SELECT o.period_start, o.period_end, o.sessions,
       o.realised_return - o.benchmark_return AS excess
FROM shadow_outcome o
JOIN shadow_decision d ON d.id = o.decision_id
WHERE d.book = $1
ORDER BY o.period_start, o.scored_at
"""


async def outcomes_for(pool, book: str) -> list[Outcome]:
    rows = await pool.fetch(_OUTCOMES, book)
    return [
        Outcome(
            period_start=row["period_start"],
            period_end=row["period_end"],
            sessions=row["sessions"],
            excess=row["excess"],
        )
        for row in rows
    ]


async def record_edge_state(pool, evaluation: EdgeEvaluation) -> bool:
    """Insert one night's judgement. A re-run the same night is a no-op.

    Returns True when the row was written, False when the night already had
    its judgement -- which is the same answer the record already gave, not a
    conflict to resolve.
    """
    inserted = await pool.fetchval(
        """
        INSERT INTO shadow_edge_state
            (book, as_of, promoted, state, scored_sessions, recent_sessions,
             mean_session_excess, decay_p, window_start, window_end, reason)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (book, as_of) DO NOTHING
        RETURNING book
        """,
        evaluation.book,
        evaluation.as_of,
        evaluation.promoted,
        evaluation.state,
        evaluation.scored_sessions,
        evaluation.recent_sessions,
        evaluation.mean_session_excess,
        evaluation.decay_p,
        evaluation.window_start,
        evaluation.window_end,
        evaluation.reason,
    )
    return inserted is not None


_LATEST = """
SELECT DISTINCT ON (book)
       book, as_of, evaluated_at, promoted, state, scored_sessions,
       recent_sessions, mean_session_excess, decay_p,
       window_start, window_end, reason
FROM shadow_edge_state
ORDER BY book, as_of DESC
"""


async def latest_edge_states(pool) -> list[EdgeEvaluation]:
    rows = await pool.fetch(_LATEST)
    return [
        EdgeEvaluation(
            book=row["book"],
            as_of=row["as_of"],
            promoted=row["promoted"],
            state=row["state"],
            scored_sessions=row["scored_sessions"],
            recent_sessions=row["recent_sessions"],
            mean_session_excess=row["mean_session_excess"],
            decay_p=row["decay_p"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            reason=row["reason"],
            evaluated_at=row["evaluated_at"],
        )
        for row in rows
    ]
