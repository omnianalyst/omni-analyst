"""The conviction gate — deciding what is worth interrupting someone for.

The most dangerous feature in the product, because it is the one that speaks
unprompted. Every constraint here exists to stop it becoming a machine for
manufacturing excitement.

**The threshold is derived, never chosen.** "High conviction" means claims of
this type at this confidence have historically resolved correctly N% of the
time. A number someone picked is an opinion wearing a statistic's clothes.

**Silence is a valid output.** A class with too few resolved predictions cannot
be surfaced at all, and a quiet week is a healthy outcome. A system that always
has something interesting to say is lying, because the world is not always
interesting.

**Disconfirming evidence travels with the finding.** A finding carrying only
supporting reasons is advocacy. If nothing disconfirming was looked for, that
is itself worth stating.

**Every surfaced finding writes a falsifiable prediction.** Which is what makes
the hit rate real: the product can show its own accuracy on the things it chose
to surface, and that number is the only reason to believe the next one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

# Matches the suppression rule ported from v1's calibration report. Defined
# once, here, because v1 had it in two places and they could drift.
MIN_RESOLVED_FOR_CALIBRATION = 10


class Refusal(str, Enum):
    """Why a candidate was not surfaced. Each is a normal outcome."""

    UNCALIBRATED = "class_has_too_few_resolved_predictions"
    BELOW_THRESHOLD = "confidence_below_the_calibrated_threshold"
    NO_DISCONFIRMING_SEARCH = "no_disconfirming_evidence_was_gathered"
    NOT_FALSIFIABLE = "no_falsifiable_prediction_could_be_written"


@dataclass(frozen=True)
class Calibration:
    """What a claim class has historically been worth, per confidence bucket."""

    claim_type: str
    method: str
    bucket_low: float
    bucket_high: float
    n: int
    hits: int

    @property
    def hit_rate(self) -> float | None:
        """None below the sample floor. None is not zero — it is 'unknown',
        and surfacing on an unknown is exactly the failure to avoid."""
        if self.n < MIN_RESOLVED_FOR_CALIBRATION:
            return None
        return self.hits / self.n


@dataclass(frozen=True)
class Candidate:
    """A claim considered for surfacing, with the evidence gathered about it."""

    claim_id: UUID
    claim_type: str
    method: str
    confidence: float
    supporting: tuple[str, ...] = ()
    disconfirming: tuple[str, ...] = ()
    searched_for_disconfirming: bool = False
    falsifiable: bool = False


@dataclass(frozen=True)
class Verdict:
    surfaced: bool
    candidate: Candidate
    refusal: Refusal | None = None
    calibrated_hit_rate: float | None = None
    threshold: float | None = None
    detail: str = ""


def calibrated_threshold(
    buckets: list[Calibration], *, target_hit_rate: float
) -> float | None:
    """The lowest confidence whose bucket historically hit `target_hit_rate`.

    Derived from outcomes rather than chosen. Returns None when no bucket has
    enough resolved predictions to say anything — in which case nothing of this
    class may be surfaced, however exciting it looks.
    """
    qualifying = [
        b for b in buckets
        if b.hit_rate is not None and b.hit_rate >= target_hit_rate
    ]
    if not qualifying:
        return None
    return min(b.bucket_low for b in qualifying)


def assess(
    candidate: Candidate,
    buckets: list[Calibration],
    *,
    target_hit_rate: float = 0.6,
) -> Verdict:
    """Decide whether this is worth saying out loud."""
    relevant = [
        b for b in buckets
        if b.claim_type == candidate.claim_type and b.method == candidate.method
    ]
    threshold = calibrated_threshold(relevant, target_hit_rate=target_hit_rate)

    if threshold is None:
        return Verdict(
            False, candidate, Refusal.UNCALIBRATED,
            detail=(
                f"{candidate.claim_type}/{candidate.method} has no confidence "
                f"bucket with {MIN_RESOLVED_FOR_CALIBRATION} resolved "
                "predictions, so no threshold can be derived"
            ),
        )

    if not candidate.falsifiable:
        # Surfacing something unscoreable would let the hit rate drift away
        # from what was actually claimed, which is how the credibility number
        # quietly stops meaning anything.
        return Verdict(
            False, candidate, Refusal.NOT_FALSIFIABLE, threshold=threshold,
            detail="a surfaced finding must write a prediction that can be scored",
        )

    if not candidate.searched_for_disconfirming:
        return Verdict(
            False, candidate, Refusal.NO_DISCONFIRMING_SEARCH, threshold=threshold,
            detail="no disconfirming evidence was gathered; a one-sided finding "
                   "is advocacy",
        )

    if candidate.confidence < threshold:
        return Verdict(
            False, candidate, Refusal.BELOW_THRESHOLD, threshold=threshold,
            detail=f"confidence {candidate.confidence:.2f} is below the "
                   f"calibrated threshold {threshold:.2f}",
        )

    hit_rate = next(
        (b.hit_rate for b in relevant
         if b.bucket_low <= candidate.confidence < b.bucket_high
         or (candidate.confidence == 1.0 and b.bucket_high == 1.0)),
        None,
    )
    return Verdict(
        True, candidate, None, calibrated_hit_rate=hit_rate, threshold=threshold,
        detail=(
            f"claims of this class at this confidence have resolved correctly "
            f"{hit_rate:.0%} of the time" if hit_rate is not None else ""
        ),
    )


def rate_limit_by_conviction(
    verdicts: list[Verdict], *, max_surfaced: int
) -> list[Verdict]:
    """Keep the strongest findings, drop the rest.

    Limited by conviction, never by schedule. Nothing here manufactures a
    finding to fill a quota — if fewer than `max_surfaced` qualify, fewer are
    returned, and if none qualify the answer is silence.
    """
    surfaced = [v for v in verdicts if v.surfaced]
    surfaced.sort(
        key=lambda v: (-(v.calibrated_hit_rate or 0.0), -v.candidate.confidence)
    )
    return surfaced[:max_surfaced]
