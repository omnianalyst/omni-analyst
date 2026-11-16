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

import math
from dataclasses import dataclass
from enum import Enum
from statistics import NormalDist
from uuid import UUID

# Matches the suppression rule ported from v1's calibration report. Defined
# once, here, because v1 had it in two places and they could drift.
MIN_RESOLVED_FOR_CALIBRATION = 10

# Two-sided 95%. The lower bound is what a bucket is judged on: a point
# estimate of 6 hits in 10 clears a 0.6 target and is indistinguishable from a
# coin, and with several buckets per method one of them clears by chance almost
# every time (P(>=6/10 | p=0.5) = 0.377; across five buckets, 91%).
Z_95 = 1.959963984540054


def z_for_trials(trials: int) -> float:
    """The two-sided 95% critical value, Bonferroni-corrected over `trials`.

    A parameter sweep backfills several variants of one method and lets the
    gate surface whichever calibrates best. Picking the best of k is k tests,
    and the winner's lower bound has to be judged at 0.05 / k or a sweep wide
    enough always finds a variant that "has an edge". One trial is plain 95%.
    """
    if trials < 1:
        raise ValueError(f"trials must be at least 1, got {trials}")
    if trials == 1:
        return Z_95
    return NormalDist().inv_cdf(1.0 - 0.025 / trials)


def wilson_lower_bound(hits: int, n: int, *, z: float = Z_95) -> float | None:
    """The lower end of the Wilson score interval, or None with no sample."""
    if n < 0 or hits < 0 or hits > n:
        raise ValueError(f"not a sample: hits={hits} n={n}")
    if n == 0:
        return None
    p = hits / n
    z_sq = z * z
    denominator = 1.0 + z_sq / n
    centre = (p + z_sq / (2 * n)) / denominator
    half = (z / denominator) * math.sqrt(p * (1.0 - p) / n + z_sq / (4 * n * n))
    return max(0.0, centre - half)


class Refusal(str, Enum):
    """Why a candidate was not surfaced. Each is a normal outcome."""

    UNCALIBRATED = "class_has_too_few_resolved_predictions"
    NO_DEMONSTRATED_EDGE = "no_bucket_beats_its_own_barrier_geometry"
    BELOW_THRESHOLD = "confidence_below_the_calibrated_threshold"
    NO_DISCONFIRMING_SEARCH = "no_disconfirming_evidence_was_gathered"
    NO_SEARCH_FOR_METHOD = "no_disconfirming_search_exists_for_this_method"
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
    # The mean stated confidence of the bucket's resolved predictions. Every
    # producer's confidence is the DRIFTLESS first-passage probability of its
    # own barriers (`predict._first_passage_confidence`), so this is the hit
    # rate the bucket would post with no edge at all: a target two units away
    # against a stop eight away is hit 80% of the time by a random walk. None
    # when the reader did not supply it -- and then the bucket cannot qualify,
    # because "beat the zero-edge rate" has nothing to be compared against.
    mean_confidence: float | None = None
    # How many sibling variants of this method were tried alongside it (a
    # parameter sweep's `trend.sma.w20/.w50/.w100` is three). The lower bound
    # is deflated for the selection: see `z_for_trials`.
    trials: int = 1

    @property
    def hit_rate(self) -> float | None:
        """None below the sample floor. None is not zero — it is 'unknown',
        and surfacing on an unknown is exactly the failure to avoid."""
        if self.n < MIN_RESOLVED_FOR_CALIBRATION:
            return None
        return self.hits / self.n

    @property
    def hit_rate_lower_bound(self) -> float | None:
        """Wilson 95% lower bound on the hit rate; None below the floor."""
        if self.n < MIN_RESOLVED_FOR_CALIBRATION:
            return None
        return wilson_lower_bound(self.hits, self.n, z=z_for_trials(self.trials))

    def shows_edge(self, *, target_hit_rate: float) -> bool:
        """Has this bucket demonstrated skill, rather than barrier geometry.

        The lower bound must clear BOTH the target and the bucket's own mean
        confidence. The raw rate clearing the target says nothing: under zero
        edge the hit rate equals the confidence, so every bucket at or above
        the target would qualify by construction.
        """
        lower = self.hit_rate_lower_bound
        if lower is None or self.mean_confidence is None:
            return False
        if not math.isfinite(self.mean_confidence):
            return False
        return lower > target_hit_rate and lower > self.mean_confidence


@dataclass(frozen=True)
class Candidate:
    """A claim considered for surfacing, with the evidence gathered about it.

    claim_id is optional: a finding need not be anchored to a single claim. An
    analysis-sourced finding (a risk score, a correlation) points at no one
    claim, so it carries None here and names its evidence in `supporting`. A
    claim-backed finding keeps a real claim_id as before.
    """

    claim_type: str
    method: str
    confidence: float
    claim_id: UUID | None = None
    supporting: tuple[str, ...] = ()
    disconfirming: tuple[str, ...] = ()
    searched_for_disconfirming: bool = False
    # Whether a disconfirming search is implemented for this method at all.
    # Distinct from `searched_for_disconfirming`, which says whether the search
    # ran on this candidate: a method with no search written for it is a gap in
    # the product, while a search that could not run is a gap in the data. Both
    # refuse, and reporting them as one hides the first behind the second.
    search_supported: bool = True
    falsifiable: bool = False


@dataclass(frozen=True)
class Verdict:
    surfaced: bool
    candidate: Candidate
    refusal: Refusal | None = None
    calibrated_hit_rate: float | None = None
    threshold: float | None = None
    detail: str = ""
    # What the surfaced bucket is ranked and judged on, and what it had to
    # beat. Carried so a reader can check the decision, not just its outcome.
    hit_rate_lower_bound: float | None = None
    zero_edge_rate: float | None = None


def calibrated_threshold(
    buckets: list[Calibration], *, target_hit_rate: float
) -> float | None:
    """The lowest confidence bucket that has demonstrated an edge.

    Derived from outcomes rather than chosen. Returns None when no bucket
    qualifies -- too few resolved predictions, a lower bound that does not
    clear the target, or a hit rate indistinguishable from what the barriers
    alone imply (`Calibration.shows_edge`) -- in which case nothing of this
    class may be surfaced, however exciting it looks.

    This is a REPORTED number, not the gate. `assess` judges a candidate on its
    own bucket: a qualifying bucket says nothing about the ones above it, which
    may have failed or never been measured.
    """
    qualifying = [
        b for b in buckets if b.shows_edge(target_hit_rate=target_hit_rate)
    ]
    if not qualifying:
        return None
    return min(b.bucket_low for b in qualifying)


def _own_bucket(candidate: Candidate, buckets: list[Calibration]) -> Calibration | None:
    """The bucket this candidate's confidence falls in, or None.

    Buckets are half-open `[low, high)` except the top decile, which is closed
    at 1.0: a confidence of exactly 1.0 belongs to the bucket ending there
    rather than to none. A confidence above every SUPPLIED bucket has no bucket
    -- it has never been observed resolving, which is not the same as belonging
    to the highest one that has.
    """
    for b in buckets:
        if b.bucket_low <= candidate.confidence < b.bucket_high:
            return b
        if candidate.confidence >= 1.0 and b.bucket_high >= 1.0:
            return b
    return None


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
        if not any(b.hit_rate is not None for b in relevant):
            return Verdict(
                False, candidate, Refusal.UNCALIBRATED,
                detail=(
                    f"{candidate.claim_type}/{candidate.method} has no confidence "
                    f"bucket with {MIN_RESOLVED_FOR_CALIBRATION} resolved "
                    "predictions, so no threshold can be derived"
                ),
            )
        # Measured, and found wanting -- a different fact from never measured,
        # and reporting it as "too few predictions" would send someone off to
        # gather more of a record that has already answered.
        return Verdict(
            False, candidate, Refusal.NO_DEMONSTRATED_EDGE,
            detail=(
                f"{candidate.claim_type}/{candidate.method} has resolved "
                f"predictions, but no confidence bucket's hit rate is "
                f"distinguishable (95% lower bound) from both the "
                f"{target_hit_rate:.0%} target and the rate its own barrier "
                "distances would give with no edge"
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

    if not candidate.search_supported:
        return Verdict(
            False, candidate, Refusal.NO_SEARCH_FOR_METHOD, threshold=threshold,
            detail=f"no disconfirming search is implemented for "
                   f"{candidate.method}; until one is, calls from it cannot be "
                   "surfaced however well calibrated",
        )

    if not candidate.searched_for_disconfirming:
        return Verdict(
            False, candidate, Refusal.NO_DISCONFIRMING_SEARCH, threshold=threshold,
            detail="no disconfirming evidence was gathered; a one-sided finding "
                   "is advocacy",
        )

    # Gated on the candidate's OWN bucket. "Everything above the lowest
    # qualifying bucket" let confidences through whose own bucket had failed
    # or had never been measured.
    own = _own_bucket(candidate, relevant)
    if own is None or not own.shows_edge(target_hit_rate=target_hit_rate):
        return Verdict(
            False, candidate, Refusal.BELOW_THRESHOLD, threshold=threshold,
            detail=(
                f"confidence {candidate.confidence:.2f} falls in a bucket that "
                f"has not shown an edge over its own barrier geometry (lowest "
                f"qualifying bucket starts at {threshold:.2f})"
            ),
        )

    hit_rate = own.hit_rate
    lower = own.hit_rate_lower_bound
    return Verdict(
        True, candidate, None, calibrated_hit_rate=hit_rate, threshold=threshold,
        hit_rate_lower_bound=lower, zero_edge_rate=own.mean_confidence,
        detail=(
            f"claims of this class at this confidence resolved correctly "
            f"{hit_rate:.0%} of {own.n} times (95% lower bound {lower:.0%}), "
            f"against the {own.mean_confidence:.0%} their barrier distances "
            f"alone would give with no edge"
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
    # Ranked on the lower bound, never the raw rate: 10 of 10 is a weaker
    # record than 700 of 1000 and must not outrank it.
    surfaced.sort(
        key=lambda v: (-(v.hit_rate_lower_bound or 0.0), -v.candidate.confidence)
    )
    return surfaced[:max_surfaced]
