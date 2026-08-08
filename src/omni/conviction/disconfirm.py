"""Gathering the case against a directional call before it is surfaced.

The conviction gate refuses a candidate that carries only supporting reasons,
because a one-sided finding is advocacy. This module is what makes that refusal
mean something: it runs a fixed set of deterministic checks over coverage the
system already holds and reports what it found on both sides.

``searched`` is the load-bearing field. It is True only when the primary check
actually had its inputs -- never a default, never a constant. When the price
history behind a call cannot be read, nothing was examined, and the gate refuses
on that basis rather than surfacing a call whose counter-case was never looked
for. A UI that renders "disconfirming: none found" is only truthful if that
distinction is real.

The checks, in the order they are reported:

1. **Longer-horizon disagreement** (primary). The call reads an SMA over its own
   window; the same signal over twice that window is the slower trend it is
   fighting. Disagreement is the most direct evidence against a trend call.
2. **Proximity to invalidation** (primary). The invalidation barrier is the MA
   itself, so a call entering near the MA is one ordinary session from being
   wrong regardless of how the confidence reads.
3. **Macro opposition** (best-effort). A risk-off regime under an up call, or
   risk-on under a down call.
4. **This entity's own record** (best-effort). Resolved predictions for this
   entity and method that went the other way more often than not.

Checks 1 and 2 share the price window, so both run or neither does -- that is
what ``searched`` reports. Checks 3 and 4 read coverage that legitimately may
not exist yet; their absence removes evidence from the report but does not
falsify the claim that a search happened.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import numpy as np

from omni.conviction.gate import MIN_RESOLVED_FOR_CALIBRATION
from omni.conviction.trend import _price_window, _realized_vol

# A call entering within this many realized volatilities of its own moving
# average is fragile: the MA is the invalidation barrier, so the distance to
# being proven wrong is less than a typical session's move.
_FRAGILE_VOL_DISTANCE = 0.5

# Beyond this the vol-normalised distance stops carrying information: a series
# smooth enough to produce it has a denominator near zero, so the ratio measures
# the smoothness rather than the trend. Found by running the search over a
# synthetic perfectly-linear ramp, which reported "434.5 volatilities".
_IMPLAUSIBLE_VOL_DISTANCE = 10.0

# Regimes that argue against a direction. Read as: an "up" call is opposed by a
# risk_off regime or a contraction phase.
_OPPOSING_RISK = {"up": "risk_off", "down": "risk_on"}
_OPPOSING_PHASE = {"up": "contraction", "down": "expansion"}

# The methods these checks describe. The parameter sweep writes trend.sma.w20,
# .w50 and so on, so this is a prefix rather than an exact name. A DCF call is
# predicated on cash flows, not a moving average -- running these checks against
# one would produce sentences that are confidently wrong about what invalidates
# it, which is worse than admitting no search exists for it.
_SUPPORTED_METHOD_PREFIX = "trend.sma"


@dataclass(frozen=True)
class Evidence:
    """What the search turned up, and whether it ran at all.

    ``supported`` and ``searched`` are different facts. A method with no search
    written for it is an unfinished part of the product; a search that had no
    inputs is a gap in the data. Both refuse, but collapsing them would hide the
    first behind the second -- a DCF call would read as "we could not gather
    evidence" when the truth is "nobody has written the checks yet".
    """

    searched: bool
    supporting: tuple[str, ...] = ()
    disconfirming: tuple[str, ...] = ()
    supported: bool = True


def _sma_direction(closes: list[float]) -> str | None:
    """Which way the trend points over ``closes``, or None if it does not.

    Price exactly on the mean is no trend, matching ``trend_call``'s refusal to
    call one. Compared with ``np.isclose`` on the price scale rather than ``==``:
    a mean over floats lands a rounding error away from the last close far more
    often than it lands on it.
    """
    if len(closes) < 2:
        return None
    mean = float(np.mean(closes))
    last = closes[-1]
    if not np.isfinite(mean) or not np.isfinite(last):
        return None
    if np.isclose(last, mean, rtol=0.0, atol=abs(last) * 1e-9):
        return None
    return "up" if last > mean else "down"


async def _latest_regime(pool, *, as_of: datetime) -> dict | None:
    """The regime knowable at ``as_of``, not the newest one on record.

    Bitemporal, like every other read here. A call written in March judged
    against June's regime is being second-guessed with information that did not
    exist when it was made -- which quietly inflates the backfill harness's
    evidence quality and makes a historical sweep look better than live.
    """
    row = await pool.fetchrow(
        """
        SELECT value FROM claim
        WHERE claim_type = 'regime_assessment'
          AND audience_user_id IS NULL
          AND redistributable = 'allowed'
          AND superseded_by IS NULL
          AND knowledge_date <= $1
        ORDER BY knowledge_date DESC LIMIT 1
        """,
        as_of,
    )
    if row is None:
        return None
    value = row["value"]
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    return value if isinstance(value, dict) else None


async def _entity_record(
    pool, *, entity_id: UUID, method: str, audience: UUID | None, as_of: datetime
) -> tuple[int, int]:
    """(resolved, hits) for this entity and method, as known at ``as_of``.

    Only predictions already resolved by ``as_of`` count. Including later
    resolutions would let a call cite a track record assembled after it was
    made.
    """
    row = await pool.fetchrow(
        """
        SELECT count(*) AS resolved,
               count(*) FILTER (
                 WHERE (direction = 'up' AND outcome = 'upper')
                    OR (direction = 'down' AND outcome = 'lower')
               ) AS hits
        FROM prediction
        WHERE entity_id = $1 AND method = $2
          AND audience_user_id IS NOT DISTINCT FROM $3
          AND outcome <> 'pending'
          AND resolved_at <= $4
        """,
        entity_id,
        method,
        audience,
        as_of,
    )
    if row is None:
        return 0, 0
    return int(row["resolved"]), int(row["hits"])


async def gather_evidence(
    pool,
    *,
    entity_id: UUID,
    method: str,
    direction: str,
    audience: UUID | None,
    as_of: datetime,
    window: int = 50,
) -> Evidence:
    """Run the checks and report both sides.

    Returns ``Evidence(searched=False)`` when no primary check could reach a
    verdict, which is the honest input to the gate's refusal -- not an error,
    and not a pass. That is the case when the price window cannot be read, and
    equally when it can but is degenerate (a flat or non-finite series, price
    sitting on its own average): reading fifty identical closes is not a search,
    and reporting it as one would let a finding through carrying no evidence at
    all -- which the `surfaced_findings_name_their_evidence` constraint then
    rejects at insert time, aborting the whole surfacing pass.

    Only ``trend.sma*`` is supported. The checks read a moving average and a
    realized vol because that is what a trend call is predicated on; against a
    DCF call the same sentences would be confidently wrong about what
    invalidates it. A method with no search written for it gets
    ``searched=False`` and is refused, which is the correct outcome rather than
    a gap to paper over.
    """
    if not method.startswith(_SUPPORTED_METHOD_PREFIX):
        return Evidence(searched=False, supported=False)

    closes = await _price_window(
        pool, entity_id=entity_id, audience=audience, as_of=as_of,
        limit=window * 2,
    )
    if len(closes) < window:
        return Evidence(searched=False)

    supporting: list[str] = []
    disconfirming: list[str] = []
    # A primary check that reached a verdict. If neither did, nothing was
    # actually examined, whatever the price window's length suggested.
    ran_primary = False

    entry = closes[-1]
    near = closes[-window:]
    sma = float(np.mean(near))
    vol = _realized_vol(near)

    # 1. The slower trend. Only meaningful with a genuinely longer window --
    #    with exactly `window` closes available the long SMA is the same series
    #    and the comparison would be vacuous, so it is reported as not run.
    if len(closes) > window:
        slow = _sma_direction(closes)
        if slow is not None:
            ran_primary = True
            if slow != direction:
                disconfirming.append(
                    f"the slower {len(closes)}-day trend points {slow}, "
                    "against this call"
                )
            else:
                supporting.append(
                    f"the slower {len(closes)}-day trend also points {slow}"
                )

    # 2. Distance to the invalidation barrier, in the call's own vol units.
    if np.isfinite(vol) and vol > 0.0:
        ran_primary = True
        distance = abs(entry - sma) / vol
        if distance < _FRAGILE_VOL_DISTANCE:
            disconfirming.append(
                f"price is only {distance:.1f} volatilities from the average "
                "that invalidates this call"
            )
        elif distance > _IMPLAUSIBLE_VOL_DISTANCE:
            # A series that is smooth but not exactly flat -- a pegged rate, a
            # halted or interpolated quote -- has a vol near zero without
            # tripping the flatness guard, so the ratio explodes. "434.5
            # volatilities" is arithmetically true and tells a reader nothing.
            # Report the direction of the fact and stop quoting the number.
            side = "above" if direction == "up" else "below"
            supporting.append(
                f"price is far {side} its {window}-day average (over "
                f"{_IMPLAUSIBLE_VOL_DISTANCE:.0f} volatilities, on a series too "
                "smooth for the ratio to be meaningful)"
            )
        else:
            side = "above" if direction == "up" else "below"
            supporting.append(
                f"price is {distance:.1f} volatilities {side} its "
                f"{window}-day average"
            )

    if not ran_primary:
        return Evidence(searched=False)

    # 3. Macro opposition. Absent regime coverage removes the check, not the search.
    regime = await _latest_regime(pool, as_of=as_of)
    if regime:
        risk = regime.get("risk_regime")
        phase = regime.get("cycle_phase")
        if risk and risk == _OPPOSING_RISK.get(direction):
            disconfirming.append(f"the macro regime is {risk.replace('_', ' ')}")
        elif risk and risk == _OPPOSING_RISK.get("down" if direction == "up" else "up"):
            supporting.append(f"the macro regime is {risk.replace('_', ' ')}")
        if phase and phase == _OPPOSING_PHASE.get(direction):
            disconfirming.append(f"the economy reads as {phase}")

    # 4. This entity's own resolved record with this method.
    resolved, hits = await _entity_record(
        pool, entity_id=entity_id, method=method, audience=audience, as_of=as_of
    )
    if resolved >= MIN_RESOLVED_FOR_CALIBRATION:
        rate = hits / resolved
        if rate < 0.5:
            disconfirming.append(
                f"{method} has been right on this name only {rate:.0%} of the "
                f"time across {resolved} resolved calls"
            )
        else:
            supporting.append(
                f"{method} has been right on this name {rate:.0%} of the time "
                f"across {resolved} resolved calls"
            )

    return Evidence(
        searched=True,
        supporting=tuple(supporting),
        disconfirming=tuple(disconfirming),
    )


__all__ = ["Evidence", "gather_evidence"]
