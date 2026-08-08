"""Rolling walk-forward with a Wilson interval -- the GATE A evidence.

`walk_forward.py` splits a method's history once, at a single cutoff. That
answers "did the edge survive the second half", which is a real question and
also a question with only one draw: one cutoff is one experiment, and an
experiment that can be moved until it passes is not evidence. This module runs
the same idea over several windows and pools the result, so the number that
authorises capital is measured over a sample, not over a choice.

Three properties are load-bearing.

**Test windows must not overlap.** Two windows whose test ranges intersect score
the same resolved prediction twice. The pooled count then overstates the sample,
and because the confidence interval narrows with the sample, the interval
narrows on evidence that was counted twice -- the arithmetic reports more
certainty than the ledger contains. Overlap is therefore a `ValueError`, not a
warning: a report that quietly double-counted is worse than no report.

**`positive` is None, not False, when there is not enough data.** None means the
question has not been settled; False means it was settled against. They are
different facts about a strategy and `trading/policy.py` treats them
differently -- None is `NO_WALK_FORWARD` (never tested), False is
`NEGATIVE_EXPECTANCY` (tested and failed). Collapsing them would either let an
untested method read as a failed one, or -- the direction that costs money --
let a failed one read as merely untested and be retried under a different name.

**Wilson, not the normal approximation.** At the sample sizes a young strategy
actually has, `p +/- z*sqrt(p(1-p)/n)` is wrong in the two ways that matter: it
produces bounds outside [0, 1] (9 hits in 10 gives an upper bound of 1.086), and
it collapses to zero width at p=0 and p=1, stating perfect certainty from ten
observations. Wilson does neither. The verdict is taken from the interval's
lower bound rather than the point estimate, which is the same discipline the
plan's own headline result is quoted with -- "73%, Wilson 95% CI [62%, 82%]" is
stated that way precisely so the lower bound can be read against the target.

Backfilled predictions are admitted: their outcomes are real, and excluding them
would make a walk-forward impossible before a year of wall-clock has passed.
They are also free -- nothing was risked -- so the split is carried through to
the result and GATE B/C read the live counts, never the pooled ones.

Nothing here imports `trading/`, `portfolio/` or `venue/`. The one-way rule
means a fill can never influence this number, which is the only reason the
number is worth anything.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from uuid import UUID

from omni.conviction.gate import Calibration, calibrated_threshold

# Two-sided 95%. Stated to full double precision rather than as 1.96, because
# the tests assert hand-computed bounds and 1.96 would move them in the fourth
# decimal -- a difference small enough to be mistaken for a bug in the formula.
Z_95 = 1.959963984540054

# The pooled out-of-sample sample below which no verdict is issued, matching
# GATE B's requirement of thirty resolved predictions. Duplicated from
# `trading.policy.MIN_RESOLVED_FOR_PAPER` rather than imported: `conviction/`
# may not import `trading/` (the one-way rule, enforced by
# test_trading_isolation.py), and a walk-forward that could see the trading tier
# is a walk-forward that could be influenced by it.
#
# Without this floor the Wilson lower bound alone would pass ten-for-ten as a
# validated edge -- its lower bound is 0.72, comfortably above a 0.6 target,
# on ten observations.
MIN_POOLED_FOR_VERDICT = 30

# The rate the gate optimises for, matching `gate.assess`'s own default. A
# target is a policy choice rather than a measurement, so it is settable; it is
# not derived from the data being judged, which is the thing that would make it
# circular.
DEFAULT_TARGET_HIT_RATE = 0.6

# Copied verbatim from `trading.policy._NOT_BACKFILLED`. It cannot be imported
# -- see MIN_POOLED_FOR_VERDICT above -- so `test_walk_forward_report.py`
# imports the original and asserts this classification matches it row for row,
# which is what stops the two drifting apart silently.
_NOT_BACKFILLED = (
    "NOT (p.provenance ? 'backfill' "
    "OR COALESCE(p.provenance -> 'assumptions' ? 'backfill', false))"
)

# Same decile scheme and same hit definition as the `calibration_bucket` view
# (migration 019) and `trading.policy._BUCKETS`, with the entity kind joined in.
# The bucket index is returned as an integer so the grouping below never keys on
# a float.
_RESOLVED = f"""
SELECT p.resolved_at,
       p.confidence,
       width_bucket(p.confidence, 0, 1, 10)              AS bucket,
       (width_bucket(p.confidence, 0, 1, 10) - 1) / 10.0 AS bucket_low,
       width_bucket(p.confidence, 0, 1, 10) / 10.0       AS bucket_high,
       ((p.direction = 'up'      AND p.outcome = 'upper')
        OR (p.direction = 'down'    AND p.outcome = 'lower')
        OR (p.direction = 'neutral' AND p.outcome = 'expiry'))  AS is_hit,
       ({_NOT_BACKFILLED})                                      AS is_live
FROM prediction p
JOIN entity e ON e.id = p.entity_id
WHERE p.method = $1
  AND e.kind = $2
  AND p.outcome <> 'pending'
  AND p.resolved_at IS NOT NULL
  AND (p.audience_user_id IS NULL OR p.audience_user_id = $3)
  AND p.resolved_at >= $4
  AND p.resolved_at < $5
"""


def wilson_interval(
    hits: int, n: int, *, z: float = Z_95
) -> tuple[float, float] | None:
    """The Wilson score interval for `hits` successes in `n` trials.

    None at n=0: an interval over no observations is not a wide interval, it is
    no interval, and returning [0, 1] would let a caller read "somewhere between
    never and always" as a measurement.

    Clamped into [0, 1] only to absorb float error at the extremes -- the
    unclamped formula already lies inside the unit interval, unlike the normal
    approximation it replaces.
    """
    if n < 0 or hits < 0:
        raise ValueError(f"counts must not be negative: hits={hits} n={n}")
    if hits > n:
        raise ValueError(f"{hits} hits out of {n} trials is not a sample")
    if n == 0:
        return None

    p = hits / n
    z_sq = z * z
    denominator = 1.0 + z_sq / n
    centre = (p + z_sq / (2 * n)) / denominator
    half_width = (z / denominator) * math.sqrt(
        p * (1.0 - p) / n + z_sq / (4 * n * n)
    )
    return (
        max(0.0, centre - half_width),
        min(1.0, centre + half_width),
    )


@dataclass(frozen=True)
class WindowSpec:
    """One train/test split, half-open on both ranges: [start, end).

    Half-open because the alternative -- inclusive ends -- puts a prediction
    resolved exactly on a boundary into two windows, which is the double count
    the non-overlap rule exists to prevent, reintroduced one row at a time.
    """

    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime

    def __post_init__(self) -> None:
        if self.train_end <= self.train_start:
            raise ValueError(
                f"training range is empty or inverted: "
                f"{self.train_start} .. {self.train_end}"
            )
        if self.test_end <= self.test_start:
            raise ValueError(
                f"test range is empty or inverted: "
                f"{self.test_start} .. {self.test_end}"
            )
        if self.test_start < self.train_end:
            raise ValueError(
                f"test range starts at {self.test_start}, inside the training "
                f"range that ends at {self.train_end}; a threshold fitted to an "
                f"outcome it is then judged on is not out of sample"
            )


@dataclass(frozen=True)
class WalkForwardWindow:
    """One window's out-of-sample result.

    `n_test` counts the test predictions the *training* threshold would have
    surfaced, not every test prediction. A walk-forward that scored every
    forward prediction would be measuring the producer, not the gate, and the
    gate is what decides whether a position is opened.
    """

    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    n_train: int
    threshold: float | None
    n_test: int
    hits: int
    live_n_test: int
    live_hits: int
    min_per_window: int

    @property
    def hit_rate(self) -> float | None:
        """None below the sample floor. None is unknown, never zero."""
        if self.n_test < self.min_per_window:
            return None
        return self.hits / self.n_test

    @property
    def backfilled_n_test(self) -> int:
        return self.n_test - self.live_n_test


@dataclass(frozen=True)
class WalkForwardResult:
    method: str
    entity_kind: str
    windows: tuple[WalkForwardWindow, ...]
    target_hit_rate: float = DEFAULT_TARGET_HIT_RATE
    min_per_window: int = 0

    @property
    def qualifying_windows(self) -> tuple[WalkForwardWindow, ...]:
        """Windows whose test sample clears the floor.

        A window below the floor contributes outcomes to nothing -- not to the
        pooled numerator and not to the denominator -- for the same reason
        `gate.Calibration` suppresses a thin bucket: three forward outcomes have
        no rate, and folding their raw hits into the pool would let them move
        the number that authorises capital.
        """
        return tuple(w for w in self.windows if w.hit_rate is not None)

    @property
    def pooled_n(self) -> int:
        return sum(w.n_test for w in self.qualifying_windows)

    @property
    def pooled_hits(self) -> int:
        return sum(w.hits for w in self.qualifying_windows)

    @property
    def pooled_live_n(self) -> int:
        return sum(w.live_n_test for w in self.qualifying_windows)

    @property
    def pooled_live_hits(self) -> int:
        return sum(w.live_hits for w in self.qualifying_windows)

    @property
    def pooled_backfilled_n(self) -> int:
        return self.pooled_n - self.pooled_live_n

    @property
    def total_test_n(self) -> int:
        """Every surfaced test prediction, including the windows too thin to
        contribute a rate. Reported alongside `pooled_n` so the difference
        between them is visible rather than being a quietly discarded sample."""
        return sum(w.n_test for w in self.windows)

    @property
    def pooled_hit_rate(self) -> float | None:
        if self.pooled_n == 0:
            return None
        return self.pooled_hits / self.pooled_n

    @property
    def wilson_interval(self) -> tuple[float, float] | None:
        return wilson_interval(self.pooled_hits, self.pooled_n)

    @property
    def positive(self) -> bool | None:
        """Did the edge hold out of sample -- unknown, yes, or no.

        None below `MIN_POOLED_FOR_VERDICT`: not established. True only when the
        *lower* bound of the 95% interval clears the target, so a point estimate
        sitting above the target on a sample too thin to distinguish it from
        noise reads as False rather than as a pass. Costs are not in this
        number; they are per-venue and belong to the report that knows which
        venue is being considered.
        """
        interval = self.wilson_interval
        if interval is None or self.pooled_n < MIN_POOLED_FOR_VERDICT:
            return None
        return interval[0] > self.target_hit_rate


def rolling_windows(
    *, start: datetime, end: datetime, n_windows: int
) -> tuple[WindowSpec, ...]:
    """Split [start, end) into an initial training slice and `n_windows` tests.

    The span is cut into `n_windows + 1` equal slices. The first is the seed
    training set; each later slice is one test window, trained on everything
    before it. Test slices are contiguous and half-open, so they tile the span
    after the seed without overlapping -- which is the property `walk_forward`
    then re-asserts rather than assumes, because a caller may supply its own
    windows.
    """
    if n_windows < 1:
        raise ValueError(f"a walk-forward needs at least one window, got {n_windows}")
    if end <= start:
        raise ValueError(f"empty or inverted span: {start} .. {end}")

    span = end - start
    slices = n_windows + 1
    if span < timedelta(microseconds=slices):
        # Timestamps are stored at microsecond resolution, so a shorter span
        # would produce boundaries that collide and windows with empty training
        # ranges. Refusing beats emitting windows that cover nothing.
        raise ValueError(
            f"span {span} cannot be cut into {slices} distinct slices at "
            f"microsecond resolution"
        )
    boundaries = [start + span * i / slices for i in range(slices)]
    # Pinned rather than computed, so float division in the line above cannot
    # leave the final window ending a microsecond short of the last outcome.
    boundaries.append(end)

    return tuple(
        WindowSpec(
            train_start=start,
            train_end=boundaries[i],
            test_start=boundaries[i],
            test_end=boundaries[i + 1],
        )
        for i in range(1, slices)
    )


def _assert_test_windows_do_not_overlap(windows: Sequence[WindowSpec]) -> None:
    ordered = sorted(windows, key=lambda w: (w.test_start, w.test_end))
    for earlier, later in pairwise(ordered):
        if later.test_start < earlier.test_end:
            raise ValueError(
                f"test windows overlap: [{earlier.test_start}, {earlier.test_end}) "
                f"and [{later.test_start}, {later.test_end}). An outcome inside "
                f"both is scored twice, which inflates the pooled sample and "
                f"narrows the confidence interval on evidence counted once"
            )


def _threshold_from(rows: Sequence, *, method: str, entity_kind: str,
                    target_hit_rate: float) -> float | None:
    """The live gate's own threshold, derived from the training rows alone.

    Reuses `gate.calibrated_threshold` and `gate.Calibration` rather than
    reimplementing the decile logic, so the threshold this report validates is
    the threshold the product would actually have applied.
    """
    n_by_bucket: dict[int, int] = {}
    hits_by_bucket: dict[int, int] = {}
    edges: dict[int, tuple[float, float]] = {}
    for row in rows:
        bucket = int(row["bucket"])
        n_by_bucket[bucket] = n_by_bucket.get(bucket, 0) + 1
        if row["is_hit"]:
            hits_by_bucket[bucket] = hits_by_bucket.get(bucket, 0) + 1
        edges[bucket] = (float(row["bucket_low"]), float(row["bucket_high"]))

    buckets = [
        Calibration(
            claim_type=entity_kind,
            method=method,
            bucket_low=edges[bucket][0],
            bucket_high=edges[bucket][1],
            n=n,
            hits=hits_by_bucket.get(bucket, 0),
        )
        for bucket, n in n_by_bucket.items()
    ]
    return calibrated_threshold(buckets, target_hit_rate=target_hit_rate)


async def walk_forward(
    pool,
    *,
    method: str,
    entity_kind: str,
    audience_user_id: UUID | None,
    windows: Sequence[WindowSpec],
    min_per_window: int,
    target_hit_rate: float = DEFAULT_TARGET_HIT_RATE,
) -> WalkForwardResult:
    """Score `method` on `entity_kind` out of sample, window by window.

    Audience-scoped exactly as `trading.policy.eligible` and
    `publish.load_calibration` are: the shared network's resolved predictions
    plus this audience's own, never another audience's. An outcome decided
    against a byo_only price series belongs to the audience that licensed it,
    and letting it validate a second operator's strategy would redistribute that
    signal through the validation channel.

    Raises rather than returning an empty result when the inputs are incoherent
    -- overlapping test windows, an empty window list, a target outside [0, 1].
    A walk-forward that returns a verdict it could not compute is worse than one
    that refuses, because the caller cannot tell the two apart.
    """
    if not windows:
        raise ValueError("a walk-forward needs at least one window")
    if min_per_window < 1:
        raise ValueError(
            f"min_per_window must be at least 1, got {min_per_window}; a floor "
            f"of zero would report a hit rate over an empty test window"
        )
    if math.isnan(target_hit_rate):
        raise ValueError(
            "target_hit_rate is NaN; every comparison against it is false, "
            "which would make every window's threshold unreachable"
        )
    if not 0.0 <= target_hit_rate <= 1.0:
        raise ValueError(f"target_hit_rate out of range: {target_hit_rate}")

    _assert_test_windows_do_not_overlap(windows)

    span_start = min(w.train_start for w in windows)
    span_end = max(w.test_end for w in windows)
    rows = await pool.fetch(
        _RESOLVED, method, entity_kind, audience_user_id, span_start, span_end
    )

    scored: list[WalkForwardWindow] = []
    for spec in windows:
        train = [
            r for r in rows
            if spec.train_start <= r["resolved_at"] < spec.train_end
        ]
        test = [
            r for r in rows
            if spec.test_start <= r["resolved_at"] < spec.test_end
        ]
        threshold = _threshold_from(
            train,
            method=method,
            entity_kind=entity_kind,
            target_hit_rate=target_hit_rate,
        )
        # No threshold means the training window could not qualify any decile,
        # so the gate would have surfaced nothing and there is nothing forward
        # to score. An empty test sample, not a failed one.
        surfaced = (
            []
            if threshold is None
            else [r for r in test if float(r["confidence"]) >= threshold]
        )
        scored.append(
            WalkForwardWindow(
                train_start=spec.train_start,
                train_end=spec.train_end,
                test_start=spec.test_start,
                test_end=spec.test_end,
                n_train=len(train),
                threshold=threshold,
                n_test=len(surfaced),
                hits=sum(1 for r in surfaced if r["is_hit"]),
                live_n_test=sum(1 for r in surfaced if r["is_live"]),
                live_hits=sum(1 for r in surfaced if r["is_hit"] and r["is_live"]),
                min_per_window=min_per_window,
            )
        )

    return WalkForwardResult(
        method=method,
        entity_kind=entity_kind,
        windows=tuple(scored),
        target_hit_rate=target_hit_rate,
        min_per_window=min_per_window,
    )
