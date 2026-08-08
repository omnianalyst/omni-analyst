"""Walk-forward validation: does the conviction edge survive out-of-sample?

The live conviction gate derives its threshold from calibration buckets
computed over ALL resolved predictions -- which is in-sample by construction.
The threshold is fit to the same data it is evaluated on, so a monotonic
calibration curve is necessary but not sufficient evidence of a real edge: any
deterministic noise will calibrate monotonically when the gate sees its own
outcomes.

This module answers the harder question. It splits predictions at a cutoff by
``resolved_at`` (when the outcome became knowable), derives the threshold from
the PRE-cutoff calibration only, applies that threshold to POST-cutoff
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
from omni.conviction.gate import MIN_RESOLVED_FOR_CALIBRATION

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


def _derive_threshold(
    train: Sequence[PredictionRow], *, target_hit_rate: float
) -> float | None:
    """The lowest decile whose training hit-rate clears the target.

    Mirrors gate.calibrated_threshold: a bucket below the sample floor is
    treated as no-information (rate None), never as a low rate.
    """
    n_by_bucket: dict[float, int] = {}
    hits_by_bucket: dict[float, int] = {}
    for row in train:
        b = _bucket_low(row.confidence)
        n_by_bucket[b] = n_by_bucket.get(b, 0) + 1
        if _is_hit(row):
            hits_by_bucket[b] = hits_by_bucket.get(b, 0) + 1

    qualifying = []
    for b, n in n_by_bucket.items():
        if n < MIN_RESOLVED_FOR_CALIBRATION:
            continue
        if hits_by_bucket.get(b, 0) / n >= target_hit_rate:
            qualifying.append(b)
    if not qualifying:
        return None
    return min(qualifying)


def walk_forward(
    predictions: Iterable[PredictionRow],
    *,
    cutoff: datetime,
    target_hit_rate: float = _DEFAULT_TARGET,
) -> list[WalkForwardResult]:
    """One result per method, each splitting its predictions at ``cutoff``.

    Training = resolved before cutoff; test = resolved at/after. The threshold
    is derived from training only and applied to test. Methods with no test
    predictions are omitted (nothing forward to score).
    """
    rows = [r for r in predictions if r.outcome not in ("pending", None)]
    methods: dict[str, list[PredictionRow]] = {}
    for r in rows:
        methods.setdefault(r.method, []).append(r)

    results: list[WalkForwardResult] = []
    for method, mrows in methods.items():
        train = [r for r in mrows if r.resolved_at < cutoff]
        test = [r for r in mrows if r.resolved_at >= cutoff]
        if not test:
            continue
        threshold = _derive_threshold(train, target_hit_rate=target_hit_rate)

        if threshold is None:
            results.append(WalkForwardResult(
                method=method, cutoff=cutoff, threshold=None,
                train_resolved=len(train), test_resolved=len(test),
                test_surfaced=0, test_hits=0, test_hit_rate=None,
                in_sample_hit_rate=None,
            ))
            continue

        test_surfaced = [r for r in test if r.confidence >= threshold]
        test_hits = sum(1 for r in test_surfaced if _is_hit(r))
        train_surfaced = [r for r in train if r.confidence >= threshold]
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
        SELECT method, confidence, direction, outcome, resolved_at
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
        )
        for r in rows
    ]
