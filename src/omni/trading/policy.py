"""Which prediction methods may hold capital at all.

The conviction gate decides what is worth *saying*. This decides what is worth
*funding*, and the two are not the same question: a method calibrated well
enough to publish a finding is not thereby calibrated well enough to size a
position in it. So the bar here is strictly higher than the gate's -- the same
calibration source, read at a stricter floor, with two dimensions the gate does
not have.

**Entity kind is a dimension of the class.** `calibration_bucket` groups by
(audience, method, confidence decile) and nothing else, because a finding's
credibility is a property of the method. A position's is not: `trend.sma` at 73%
on sixteen large-caps says nothing about `trend.sma` on a perpetual, and a gate
that pooled them would let equity history authorise crypto capital. This module
therefore aggregates the prediction ledger with the entity joined in, using the
view's own bucket expression and its own hit definition so the two cannot drift.

**Backfilled predictions calibrate but do not qualify.** A backfill replays the
producer at historical timestamps; the outcomes are real, which is why they feed
calibration and why the cold-start problem is solved by them. They are also
free: nothing was risked, no order was routed, no fill slipped. GATE C asks for
thirty *live* resolved predictions precisely because a backfill can manufacture
thirty of anything overnight. So `live_resolved_n` excludes any prediction whose
provenance carries a backfill marker, and only that count opens the scale phase.

**None is not zero.** An uncalibrated method has an unknown hit rate, and
trading on unknown is the failure this module exists to prevent. Likewise a
walk-forward that has never been run is not a walk-forward that passed:
`walk_forward_positive=None` refuses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from omni.conviction.gate import MIN_RESOLVED_FOR_CALIBRATION, Calibration

# The conviction gate surfaces a class at ten resolved predictions. Capital
# waits for thirty, per AUTOTRADE_PLAN.md GATE B -- ten resolved outcomes pin a
# hit rate to roughly +/-30 points at 95%, which is a band wide enough to
# contain both a real edge and no edge at all.
MIN_RESOLVED_FOR_PAPER = 30

# GATE C. Counted over live predictions only; see the module docstring.
MIN_LIVE_RESOLVED_FOR_SCALE = 30


class TradingPhase(str, Enum):
    PAPER = "paper"
    MICRO = "micro"
    SCALE = "scale"
    HALTED = "halted"


class Ineligible(str, Enum):
    """Why a method may not hold capital. Each is a normal outcome."""

    UNCALIBRATED = "no_confidence_bucket_has_enough_resolved_predictions"
    BELOW_HIT_RATE = "calibrated_hit_rate_is_below_the_target"
    INSUFFICIENT_RESOLVED = "too_few_resolved_predictions_for_this_method_and_kind"
    NO_WALK_FORWARD = "no_walk_forward_validation_has_been_run"
    NEGATIVE_EXPECTANCY = "walk_forward_did_not_hold_out_of_sample"
    BACKFILL_ONLY = "too_few_live_resolved_predictions_backfill_does_not_count"
    PHASE_FORBIDS = "the_current_trading_phase_forbids_holding_capital"


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    phase: TradingPhase
    method: str
    entity_kind: str
    resolved_n: int
    live_resolved_n: int
    hit_rate: float | None
    reason: Ineligible | None
    detail: str
    # The sample `hit_rate` was measured over. Distinct from `resolved_n`,
    # which counts every resolved prediction including those in buckets too
    # small to contribute a rate. Downstream, fractional Kelly and GATE B's
    # Wilson interval both need this one -- using resolved_n would state a
    # tighter confidence interval than the evidence supports.
    measured_n: int = 0


# PAPER < MICRO < SCALE: each phase carries every requirement of the one below
# it. HALTED is not on the ladder -- it is a refusal, not a lesser permission.
_PHASE_RANK = {
    TradingPhase.PAPER: 0,
    TradingPhase.MICRO: 1,
    TradingPhase.SCALE: 2,
}

# A backfilled prediction is marked in its provenance. `record_prediction` builds
# the provenance envelope itself and only `assumptions` is caller-settable, so a
# marker can legitimately arrive at either level and both are checked. The
# COALESCE is load-bearing: `jsonb ? key` on a missing sub-object yields NULL,
# and a FILTER over NULL drops the row -- which would silently classify every
# prediction without an `assumptions` key as backfilled.
_NOT_BACKFILLED = (
    "NOT (p.provenance ? 'backfill' "
    "OR COALESCE(p.provenance -> 'assumptions' ? 'backfill', false))"
)

# Mirrors the calibration_bucket view (migration 019) exactly -- same
# width_bucket deciles, same hit definition, same exclusion of pending -- with
# the entity kind joined in, which the view has no column for.
_BUCKETS = f"""
SELECT width_bucket(p.confidence, 0, 1, 10)                 AS bucket,
       (width_bucket(p.confidence, 0, 1, 10) - 1) / 10.0    AS bucket_low,
       width_bucket(p.confidence, 0, 1, 10) / 10.0          AS bucket_high,
       count(*)                                             AS n,
       count(*) FILTER (
           WHERE (p.direction = 'up'      AND p.outcome = 'upper')
              OR (p.direction = 'down'    AND p.outcome = 'lower')
              OR (p.direction = 'neutral' AND p.outcome = 'expiry')
       )                                                    AS hits,
       count(*) FILTER (WHERE {_NOT_BACKFILLED})            AS live_n
FROM prediction p
JOIN entity e ON e.id = p.entity_id
WHERE p.method = $1
  AND e.kind = $2
  AND p.outcome <> 'pending'
  AND (p.audience_user_id IS NULL OR p.audience_user_id = $3)
GROUP BY p.audience_user_id, width_bucket(p.confidence, 0, 1, 10)
"""


async def eligible(
    pool,
    *,
    method: str,
    entity_kind: str,
    audience_user_id: UUID | None,
    phase: TradingPhase,
    target_hit_rate: float,
    walk_forward_positive: bool | None,
) -> Eligibility:
    """May this method hold capital for this audience, in this phase.

    Audience-scoped the way calibration is (`publish.load_calibration`): the
    shared network's resolved predictions plus this audience's own, never
    another audience's. A prediction resolved against a byo_only price series
    belongs to the audience that licensed it, and letting it open a second
    operator's gate would redistribute that signal through the eligibility
    channel.

    `walk_forward_positive` is supplied by the caller rather than read here,
    because a walk-forward is an offline validation run over a chosen cutoff
    (`conviction/walk_forward.py`) and there is nothing in the ledger to derive
    it from. `None` means it was never run.

    Raises rather than returning a verdict when the ledger cannot be read: an
    eligibility computed without the counts it is made of would be a fabricated
    permission, and the caller cannot tell one from a genuine refusal.
    """
    phase = TradingPhase(phase)
    if math.isnan(target_hit_rate):
        raise ValueError(
            "target_hit_rate is NaN; every comparison against it is false, "
            "which would make every method eligible"
        )
    if not 0.0 <= target_hit_rate <= 1.0:
        raise ValueError(f"target_hit_rate out of range: {target_hit_rate}")

    rows = await pool.fetch(_BUCKETS, method, entity_kind, audience_user_id)
    buckets = [
        Calibration(
            claim_type=entity_kind,
            method=method,
            bucket_low=float(r["bucket_low"]),
            bucket_high=float(r["bucket_high"]),
            n=r["n"],
            hits=r["hits"],
        )
        for r in rows
    ]
    resolved_n = sum(b.n for b in buckets)
    live_resolved_n = sum(r["live_n"] for r in rows)

    # Pooled over the buckets that clear the sample floor, and only those. A
    # bucket below the floor has no rate to contribute -- folding its raw hits
    # into the pooled numerator would let three resolved predictions at 0.1
    # confidence move the number that authorises a position.
    usable = [b for b in buckets if b.hit_rate is not None]
    # The sample the rate was actually measured over, which is NOT resolved_n:
    # buckets below the calibration floor contribute outcomes to the count and
    # nothing to the rate. Reporting "100% over 34 predictions" when 9 of the 34
    # resolved against the call and only 25 entered the numerator is a false
    # statement in the audit trail for a capital decision -- and downstream both
    # fractional Kelly and GATE B's Wilson CI need the measured sample, not the
    # larger one.
    measured_n = sum(b.n for b in usable)
    hit_rate = sum(b.hits for b in usable) / measured_n if usable else None

    def verdict(reason: Ineligible | None, detail: str) -> Eligibility:
        return Eligibility(
            eligible=reason is None,
            phase=phase,
            method=method,
            entity_kind=entity_kind,
            resolved_n=resolved_n,
            live_resolved_n=live_resolved_n,
            hit_rate=hit_rate,
            reason=reason,
            detail=detail,
            measured_n=measured_n,
        )

    if phase is TradingPhase.HALTED:
        return verdict(
            Ineligible.PHASE_FORBIDS,
            "trading is halted; no method may hold capital regardless of record",
        )

    # Ordered by how fundamental the objection is. An unknown hit rate is a
    # different statement from a known bad one, and a rate measured over
    # twenty-nine outcomes is not yet worth arguing with.
    if hit_rate is None:
        return verdict(
            Ineligible.UNCALIBRATED,
            f"{method}/{entity_kind} has no confidence bucket with "
            f"{MIN_RESOLVED_FOR_CALIBRATION} resolved predictions "
            f"({resolved_n} resolved in total), so its hit rate is unknown",
        )
    if measured_n < MIN_RESOLVED_FOR_PAPER:
        return verdict(
            Ineligible.INSUFFICIENT_RESOLVED,
            f"{measured_n} resolved predictions actually entered the hit rate "
            f"for {method}/{entity_kind} ({resolved_n} resolved in total), "
            f"{MIN_RESOLVED_FOR_PAPER} required before any capital",
        )
    if hit_rate < target_hit_rate:
        return verdict(
            Ineligible.BELOW_HIT_RATE,
            f"calibrated hit rate {hit_rate:.2f} is below the target "
            f"{target_hit_rate:.2f} over {measured_n} measured predictions",
        )

    # Walk-forward is required from PAPER onward, not from MICRO. GATE A in
    # AUTOTRADE_PLAN.md admits a strategy to paper trading only once it has a
    # walk-forward result net of costs, and the reason applies at every phase:
    # an in-sample hit rate is fitted to the outcomes it is being judged on, so
    # a paper record built on it inherits the overfit rather than testing it.
    # Gating this behind MICRO meant a caller that never ran a walk-forward was
    # admitted to paper and accrued a track record that GATE B would then read.
    if walk_forward_positive is None:
        return verdict(
            Ineligible.NO_WALK_FORWARD,
            f"no walk-forward result exists for {method}; in-sample "
            "calibration is fitted to the outcomes it is judged on",
        )
    if not walk_forward_positive:
        return verdict(
            Ineligible.NEGATIVE_EXPECTANCY,
            f"{method} did not hold out of sample; the in-sample hit rate "
            f"{hit_rate:.2f} is selection, not edge",
        )

    rank = _PHASE_RANK[phase]
    if rank >= _PHASE_RANK[TradingPhase.SCALE] and (
        live_resolved_n < MIN_LIVE_RESOLVED_FOR_SCALE
    ):
        return verdict(
            Ineligible.BACKFILL_ONLY,
            f"{live_resolved_n} live resolved predictions of {resolved_n} "
            f"total, {MIN_LIVE_RESOLVED_FOR_SCALE} required to scale; "
            "backfilled outcomes calibrate but risked nothing",
        )

    return verdict(
        None,
        f"{method}/{entity_kind} resolved correctly {hit_rate:.0%} of the time "
        f"over {resolved_n} predictions ({live_resolved_n} live)",
    )
