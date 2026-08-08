"""Which prediction methods may hold capital at all.

The conviction gate decides what is worth *saying*. This decides what is worth
*funding*, and the two are not the same question: a method calibrated well
enough to publish a finding is not thereby calibrated well enough to size a
position in it. So the bar here is strictly higher than the gate's -- the same
calibration source, read at a stricter floor, with two dimensions the gate does
not have.

**The bar is net expectancy, not hit rate.** It was hit rate, and that was the
wrong statistic rather than a badly-tuned one. Hit rate is a proxy for edge only
when payoffs are symmetric, and `trend.sma`'s are not by construction: its stop
IS the moving average, a level close to price, while its target is a
volatility-scaled move further out. Measured over a year of real coverage that
method resolved 424 crypto predictions at a 34.2% hit rate -- an interval
entirely below a coin flip -- and earned +29.2 bps per trade on a 4.32:1 payoff.
The gate refused it. The mirror is worse and was silent: a 67% method at 1:4
loses money on every trade and would have been admitted. So the ledger's own
realised P&L is pooled by `trading/expectancy.py` and compared, net of the
caller's round-trip cost, against a stated minimum. Hit rate is still reported,
because it is still informative; it no longer decides anything.

**The floor applies to `effective_n`, not to the raw count.** Those same 424
predictions spanned 44 distinct horizon dates. Nine highly-correlated crypto
assets resolving on one day are close to one observation, not nine, so a floor
of thirty applied to the raw count was cleared by roughly three independent
observations. `expectancy.effective_n` counts distinct horizons instead.

**Two properties can make a healthy expectancy meaningless, and both refuse.**
A third of that sample expired without touching a barrier; the ledger does not
store the price it expired at, so its P&L is assumed rather than measured, and
past `max_assumed_share` most of the result is a number nobody observed. And the
pooled figure was carried by two of nine assets against one deeply negative one
-- past `max_concentration`, one name is the strategy and the method name on it
is decoration.

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

**None is not zero.** An uncalibrated method has an unknown expectancy, and
trading on unknown is the failure this module exists to prevent. Likewise a
walk-forward that has never been run is not a walk-forward that passed:
`walk_forward_positive=None` refuses.

**The risk parameters have no defaults.** `round_trip_cost_bps`,
`min_expectancy_bps`, `min_effective_n`, `max_assumed_share` and
`max_concentration` are required keyword arguments. A default here would be an
invented risk parameter that every caller inherits without stating it, and the
caller that never thought about the cost of a round trip is exactly the caller
whose edge does not survive one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from uuid import UUID

from omni.conviction.gate import MIN_RESOLVED_FOR_CALIBRATION, Calibration
from omni.trading import expectancy

# The conviction gate surfaces a class at ten resolved predictions. Capital
# waits for thirty, per AUTOTRADE_PLAN.md GATE B. The number is unchanged; the
# quantity it counts is not. It now applies to `effective_n`, because the raw
# count overstated the sample by roughly tenfold on the first real run.
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
    # No longer produced by `eligible`: barring on the hit rate was the defect
    # this module was rewritten to fix, and the refusal path now reads net
    # expectancy. Kept because `bridge.py`'s tests name it, and because a reason
    # code that has been served to a caller is part of the interface even after
    # the branch that emitted it is gone.
    BELOW_HIT_RATE = "calibrated_hit_rate_is_below_the_target"
    INSUFFICIENT_RESOLVED = "too_few_resolved_predictions_for_this_method_and_kind"
    TOO_MUCH_ASSUMED = "too_much_of_the_realised_pnl_was_assumed_rather_than_measured"
    CONCENTRATED = "the_realised_edge_is_carried_by_a_single_entity"
    BELOW_EXPECTANCY = "net_expectancy_per_trade_is_below_the_required_minimum"
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
    # The realised figures, from `expectancy.compute` over the same audience's
    # resolved directional predictions. `None` means no expectancy was computed
    # -- an unmeasured method, not a flat one -- and the counts default to zero
    # so a caller assembling an `Eligibility` by hand (the bridge's tests do)
    # gets "nothing measured" rather than a fabricated figure.
    expectancy_n: int = 0
    effective_n: int = 0
    gross_expectancy_bps: Decimal | None = None
    net_expectancy_bps: Decimal | None = None
    assumed_share: Decimal | None = None
    concentration: Decimal | None = None
    positive_entities: int = 0


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

# The rows realised P&L is pooled from, one per resolved directional prediction.
#
# `neutral` is excluded for the same reason `api/trading.py` excludes it from
# the barrier geometry: a neutral call has no target and no stop, `bridge.py`
# refuses to build an intent from one, and folding a permanent zero into the
# mean would price trades that are never placed.
#
# The horizon date is taken in UTC rather than the session timezone, so the
# count of distinct horizons -- and therefore `effective_n`, and therefore the
# gate -- cannot change with a connection setting.
_RESOLVED_TRADES = """
SELECT COALESCE(e.symbol, e.id::text)                       AS entity_key,
       p.direction::text                                    AS direction,
       p.outcome::text                                      AS outcome,
       p.entry_price,
       p.upper_barrier,
       p.lower_barrier,
       ((p.horizon_ends_at AT TIME ZONE 'UTC')::date)::text  AS horizon_key
FROM prediction p
JOIN entity e ON e.id = p.entity_id
WHERE p.method = $1
  AND e.kind = $2
  AND p.outcome <> 'pending'
  AND p.direction <> 'neutral'
  AND (p.audience_user_id IS NULL OR p.audience_user_id = $3)
"""


def _require_finite(name: str, value: Decimal) -> None:
    if not value.is_finite():
        raise ValueError(
            f"{name} must be finite, got {value}; every comparison against a "
            f"NaN is false, so a NaN threshold does not bar anything"
        )


async def eligible(
    pool,
    *,
    method: str,
    entity_kind: str,
    audience_user_id: UUID | None,
    phase: TradingPhase,
    target_hit_rate: float,
    walk_forward_positive: bool | None,
    round_trip_cost_bps: Decimal,
    min_expectancy_bps: Decimal,
    min_effective_n: int,
    max_assumed_share: Decimal,
    max_concentration: Decimal,
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

    `target_hit_rate` is validated and reported but no longer bars: see the
    module docstring for why the hit rate was the wrong quantity to bar on. It
    is still rejected when it is NaN or out of range, because a caller that
    passes one has a bug that the previous gate would have turned into blanket
    permission.

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

    _require_finite("round_trip_cost_bps", round_trip_cost_bps)
    _require_finite("min_expectancy_bps", min_expectancy_bps)
    _require_finite("max_assumed_share", max_assumed_share)
    _require_finite("max_concentration", max_concentration)
    if round_trip_cost_bps < 0:
        raise ValueError(
            f"round_trip_cost_bps must not be a credit: {round_trip_cost_bps}"
        )
    if min_expectancy_bps <= 0:
        raise ValueError(
            f"min_expectancy_bps must be positive, got {min_expectancy_bps}; a "
            f"non-positive minimum admits a strategy that does not pay for the "
            f"error bar on its own cost model"
        )
    if min_effective_n < 1:
        raise ValueError(
            f"min_effective_n must be at least 1, got {min_effective_n}; a "
            f"floor of zero admits a method with no independent observations"
        )
    if not Decimal(0) <= max_assumed_share <= Decimal(1):
        raise ValueError(f"max_assumed_share out of range: {max_assumed_share}")
    if not Decimal(0) <= max_concentration <= Decimal(1):
        raise ValueError(f"max_concentration out of range: {max_concentration}")

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

    trade_rows = await pool.fetch(
        _RESOLVED_TRADES, method, entity_kind, audience_user_id
    )
    realised = expectancy.compute(
        [
            expectancy.ResolvedTrade(
                entity_key=r["entity_key"],
                direction=r["direction"],
                outcome=r["outcome"],
                entry_price=r["entry_price"],
                upper_barrier=r["upper_barrier"],
                lower_barrier=r["lower_barrier"],
                horizon_key=r["horizon_key"],
            )
            for r in trade_rows
        ]
    )
    measured = realised.n > 0
    gross_bps = realised.gross_bps if measured else None
    net_bps = realised.net_bps(round_trip_cost_bps) if measured else None
    assumed_share = realised.assumed_share if measured else None
    concentration = realised.concentration if measured else None

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
            expectancy_n=realised.n,
            effective_n=realised.effective_n,
            gross_expectancy_bps=gross_bps,
            net_expectancy_bps=net_bps,
            assumed_share=assumed_share,
            concentration=concentration,
            positive_entities=realised.positive_entities,
        )

    if phase is TradingPhase.HALTED:
        return verdict(
            Ineligible.PHASE_FORBIDS,
            "trading is halted; no method may hold capital regardless of record",
        )

    # Ordered by how fundamental the objection is. An unmeasured method is a
    # different statement from a measured bad one; a sample too small or too
    # assumed or too concentrated is a reason the number itself cannot be
    # argued with, so it is raised before the number's sign.
    if not measured or hit_rate is None:
        return verdict(
            Ineligible.UNCALIBRATED,
            f"{method}/{entity_kind} has no measured edge: {resolved_n} "
            f"resolved predictions, {realised.n} of them directional, and no "
            f"confidence bucket with {MIN_RESOLVED_FOR_CALIBRATION} outcomes",
        )
    if realised.effective_n < min_effective_n:
        return verdict(
            Ineligible.INSUFFICIENT_RESOLVED,
            f"{realised.n} resolved predictions for {method}/{entity_kind} span "
            f"only {realised.effective_n} distinct horizon dates, and correlated "
            f"names resolving on one date are close to one observation; "
            f"{min_effective_n} required before any capital",
        )
    if assumed_share > max_assumed_share:
        return verdict(
            Ineligible.TOO_MUCH_ASSUMED,
            f"{realised.assumed_n} of {realised.n} predictions expired without "
            f"touching a barrier, so {assumed_share:.1%} of the pooled P&L is "
            f"assumed rather than measured (limit {max_assumed_share:.1%}); the "
            f"ledger does not record the price they expired at",
        )
    if concentration > max_concentration:
        top = realised.per_entity[0][0] if realised.per_entity else "unknown"
        return verdict(
            Ineligible.CONCENTRATED,
            f"{concentration:.1%} of the absolute P&L for {method}/"
            f"{entity_kind} comes from one entity ({top}), above the "
            f"{max_concentration:.1%} limit; that is a position, not an edge",
        )
    if net_bps < min_expectancy_bps:
        return verdict(
            Ineligible.BELOW_EXPECTANCY,
            f"{method}/{entity_kind} realised {gross_bps:.1f} bps per trade "
            f"gross, {net_bps:.1f} bps net of {round_trip_cost_bps} bps of "
            f"round-trip cost, below the {min_expectancy_bps} bps minimum",
        )

    # Walk-forward is required from PAPER onward, not from MICRO. GATE A in
    # AUTOTRADE_PLAN.md admits a strategy to paper trading only once it has a
    # walk-forward result net of costs, and the reason applies at every phase:
    # an in-sample expectancy is fitted to the outcomes it is being judged on,
    # so a paper record built on it inherits the overfit rather than testing it.
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
            f"{method} did not hold out of sample; the in-sample "
            f"{net_bps:.1f} bps net is selection, not edge",
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
        f"{method}/{entity_kind} realised {net_bps:.1f} bps per trade net of "
        f"{round_trip_cost_bps} bps cost over {realised.n} predictions across "
        f"{realised.effective_n} distinct horizons ({live_resolved_n} live), "
        f"hit rate {hit_rate:.0%}",
    )
