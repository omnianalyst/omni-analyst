"""Walk-forward validation: does the conviction edge survive out-of-sample?

The live conviction gate derives its threshold from calibration buckets
computed over ALL resolved predictions -- which is in-sample by construction.
The threshold is fit to the same data it is evaluated on, so a monotonic
calibration curve is necessary but not sufficient evidence of a real edge: any
deterministic noise will calibrate monotonically when the gate sees its own
outcomes.

This module answers the harder question. It splits predictions at a cutoff by
the decision date, purging training calls whose label window straddles the
cutoff, derives the qualifying deciles from the PRE-cutoff calibration only, applies that threshold to POST-cutoff
predictions, and reports the forward hit-rate. If the forward rate holds near
the in-sample rate (and above the target), the edge is real; if it collapses,
the gate has been selecting on its own overfitting.

Pure functions over plain records so the split logic is unit-testable without a
database. ``fetch_for_walk_forward`` is the single DB touchpoint.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

# Must match gate.MIN_RESOLVED_FOR_CALIBRATION: a bucket below this floor has
# no statistically meaningful rate, so it must not move the threshold.
from omni.conviction.gate import MIN_RESOLVED_FOR_CALIBRATION, wilson_lower_bound

# 0.0..1.0 in tenths, matching the live calibration_bucket scheme.
_BUCKET_WIDTH = 0.1
_NUM_BUCKETS = 10


@dataclass(frozen=True)
class PredictionRow:
    method: str
    confidence: float
    direction: str  # "up" | "down"
    outcome: str  # "upper" | "lower" | "hit" (pending filtered upstream)
    resolved_at: datetime
    # When the call was made and when its label window closes. Optional only
    # for hand-built rows; `fetch_for_walk_forward` always supplies both, and
    # with them the split is on the decision date and purged (see
    # `walk_forward`).
    created_at: datetime | None = None
    horizon_ends_at: datetime | None = None


@dataclass(frozen=True)
class WalkForwardResult:
    method: str
    cutoff: datetime
    threshold: float | None
    train_resolved: int
    test_resolved: int
    test_surfaced: int  # confidence >= threshold (0 when threshold is None)
    test_hits: int
    test_hit_rate: float | None  # surfaced hits / surfaced; None if none surfaced
    in_sample_hit_rate: float | None  # same threshold applied to training, for contrast

    @property
    def holds_out_of_sample(self) -> bool | None:
        """True when the forward rate clears the target the gate optimises for.

        None (not False) when there is too little forward data to judge -- a
        quiet forward window is not evidence the edge is gone, just that it has
        not been tested yet.
        """
        if self.test_hit_rate is None:
            return None
        return self.test_hit_rate >= _DEFAULT_TARGET


_DEFAULT_TARGET = 0.6


def _is_hit(row: PredictionRow) -> bool:
    """Correct per the canonical definition, which lives in SQL.

    `calibration_bucket` (migration 019) and `policy._BUCKETS` both score a
    `neutral` call that expired without touching a barrier as a HIT: asserting
    the price would go nowhere, and it going nowhere, is the assertion coming
    true. This function omitted that case, so it scored every correct neutral
    as a miss and understated the out-of-sample hit rate of any method that
    emits them -- judging a producer as having failed out of sample when it had
    not.

    No producer writes `neutral` today, so nothing has been mis-scored yet.
    That is why this was latent rather than a live defect, and it is also why
    it had to be fixed before one does: the divergence is invisible until the
    first neutral-emitting producer ships, at which point its walk-forward
    would silently disagree with its own calibration bucket.

    The SQL is authoritative because it is what the live gate reads. Any Python
    restatement of it is a copy that can drift, which is exactly what happened.
    """
    return (
        (row.direction == "up" and row.outcome == "upper")
        or (row.direction == "down" and row.outcome == "lower")
        or (row.direction == "neutral" and row.outcome == "expiry")
    )


def _bucket_low(confidence: float) -> float:
    # The lower edge of the decile confidence falls into. Matches
    # width_bucket(0.0, 1.0, 10) on the live side.
    idx = min(int(confidence / _BUCKET_WIDTH), _NUM_BUCKETS - 1)
    return idx * _BUCKET_WIDTH


def _qualifying_buckets(
    train: Sequence[PredictionRow], *, target_hit_rate: float
) -> set[float]:
    """The deciles whose training record shows an edge.

    Mirrors `gate.Calibration.shows_edge`: at least the sample floor, and a
    Wilson lower bound above both the target and the bucket's own mean
    confidence -- the hit rate its barrier geometry gives with no edge.
    """
    n_by_bucket: dict[float, int] = {}
    hits_by_bucket: dict[float, int] = {}
    conf_by_bucket: dict[float, float] = {}
    for row in train:
        b = _bucket_low(row.confidence)
        n_by_bucket[b] = n_by_bucket.get(b, 0) + 1
        conf_by_bucket[b] = conf_by_bucket.get(b, 0.0) + row.confidence
        if _is_hit(row):
            hits_by_bucket[b] = hits_by_bucket.get(b, 0) + 1

    qualifying: set[float] = set()
    for b, n in n_by_bucket.items():
        if n < MIN_RESOLVED_FOR_CALIBRATION:
            continue
        lower = wilson_lower_bound(hits_by_bucket.get(b, 0), n)
        if lower is None:
            continue
        if lower > target_hit_rate and lower > conf_by_bucket[b] / n:
            qualifying.add(b)
    return qualifying


def _derive_threshold(
    train: Sequence[PredictionRow], *, target_hit_rate: float
) -> float | None:
    """The lowest decile whose training record shows an edge, or None."""
    qualifying = _qualifying_buckets(train, target_hit_rate=target_hit_rate)
    if not qualifying:
        return None
    return min(qualifying)


def _in_training(row: PredictionRow, cutoff: datetime) -> bool:
    if row.created_at is None or row.horizon_ends_at is None:
        return row.resolved_at < cutoff
    return (
        row.created_at < cutoff
        and row.horizon_ends_at <= cutoff
        and row.resolved_at <= cutoff
    )


def _in_test(row: PredictionRow, cutoff: datetime) -> bool:
    if row.created_at is None:
        return row.resolved_at >= cutoff
    return row.created_at >= cutoff


def walk_forward(
    predictions: Iterable[PredictionRow],
    *,
    cutoff: datetime,
    target_hit_rate: float = _DEFAULT_TARGET,
) -> list[WalkForwardResult]:
    """One result per method, each splitting its predictions at ``cutoff``.

    Rows carrying `created_at` and `horizon_ends_at` are split on the decision
    date and purged: training = called before the cutoff with the whole label
    window closed by it; test = called at/after. A row missing either falls
    back to `resolved_at`, which is only safe for label windows that do not
    straddle the cutoff. The qualifying deciles are derived from training only
    and a test call is surfaced when its OWN decile qualified, exactly as the
    live gate decides. Methods with no test predictions are omitted.
    """
    rows = [r for r in predictions if r.outcome not in ("pending", None)]
    methods: dict[str, list[PredictionRow]] = {}
    for r in rows:
        methods.setdefault(r.method, []).append(r)

    results: list[WalkForwardResult] = []
    for method, mrows in methods.items():
        train = [r for r in mrows if _in_training(r, cutoff)]
        test = [r for r in mrows if _in_test(r, cutoff)]
        if not test:
            continue
        qualifying = _qualifying_buckets(train, target_hit_rate=target_hit_rate)
        threshold = min(qualifying) if qualifying else None

        if threshold is None:
            results.append(WalkForwardResult(
                method=method, cutoff=cutoff, threshold=None,
                train_resolved=len(train), test_resolved=len(test),
                test_surfaced=0, test_hits=0, test_hit_rate=None,
                in_sample_hit_rate=None,
            ))
            continue

        test_surfaced = [r for r in test if _bucket_low(r.confidence) in qualifying]
        test_hits = sum(1 for r in test_surfaced if _is_hit(r))
        train_surfaced = [r for r in train if _bucket_low(r.confidence) in qualifying]
        train_hits = sum(1 for r in train_surfaced if _is_hit(r))

        results.append(WalkForwardResult(
            method=method, cutoff=cutoff, threshold=threshold,
            train_resolved=len(train), test_resolved=len(test),
            test_surfaced=len(test_surfaced), test_hits=test_hits,
            test_hit_rate=(test_hits / len(test_surfaced)) if test_surfaced else None,
            in_sample_hit_rate=(
                train_hits / len(train_surfaced) if train_surfaced else None
            ),
        ))
    return results


async def fetch_for_walk_forward(pool) -> list[PredictionRow]:
    """Every resolved prediction with its resolve timestamp. The single DB
    touchpoint; everything above is pure and unit-tested."""
    rows = await pool.fetch(
        """
        SELECT method, confidence, direction, outcome, resolved_at,
               created_at, horizon_ends_at
        FROM prediction
        WHERE outcome IS NOT NULL AND outcome <> 'pending'
          AND resolved_at IS NOT NULL
        """
    )
    return [
        PredictionRow(
            method=r["method"],
            confidence=float(r["confidence"]),
            direction=r["direction"],
            outcome=r["outcome"],
            resolved_at=r["resolved_at"],
            created_at=r["created_at"],
            horizon_ends_at=r["horizon_ends_at"],
        )
        for r in rows
    ]
