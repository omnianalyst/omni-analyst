"""Alert conditions over coverage.

An alert is a condition evaluated against the claims the owner may actually
see, never a threshold on a price and never arbitrary user-supplied logic. The
condition set is closed: each kind is a fixed, pure predicate over claims,
validated at creation. Anything outside the set is rejected with a clear error
-- a rule engine that accepted an expression or eval would be a hole, not a
feature.

evaluate reads only through omni.coverage.visibility.visible_claims, scoped to
the alert's owner. A read path here that touched the claim table directly would
be a redistribution leak: an alert set on an entity would surface another user's
byo_only claim to the wrong audience. The audience parameter is therefore the
alert owner's id, never None for an alert the owner is evaluating, and
visible_claims enforces the rest.

Firing is recorded, not merely detected. evaluate writes each newly-satisfying
claim into alert_firing and raises the owner's demand for that (entity,
claim_type) the first time the alert ever fires. Recording belongs with
detection because a detected-but-unrecorded firing would re-fire on every poll,
which is exactly the noise the firing table exists to prevent. The
(alert_id, claim_id) primary key is the real dedup; the INSERT ... RETURNING
below is what makes the returned set match reality under concurrency: two
evaluations racing on the same alert each see only the rows they actually
inserted, so neither re-reports the other's firings (audit A09). Delivery
enqueueing happens in the SAME transaction through the ``notify`` hook, so a
firing exists if and only if its notification rows do.

Sequence conditions (value crossings, percent changes) are evaluated per
SOURCE series: claims for one (entity, claim_type) can come from several
unrelated publishers, and interleaving them manufactures crossings --
publisher A above the threshold, publisher B below, alternating forever --
out of two series that each hold a steady position (audit A16).
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from omni.coverage.visibility import visible_claims
from omni.demand.ledger import direct_attention

# The closed set of condition kinds. Adding one means writing its predicate in
# _satisfying and a test; the set is spelled out so a typo in the stored JSONB
# cannot widen what evaluate will run.
KNOWN_KINDS = frozenset({
    "value_above", "value_below",
    "pct_change_above", "pct_change_below",
    "staleness_exceeds", "contradiction",
})

_DEFAULT_VALUE_FIELD = "value"

#: A percent-change window is bounded to 100 years: anything wider can never
#: be distinguished from "all history" by the data this store holds, and an
#: unbounded value used to overflow timedelta construction during evaluation
#: (a stored ``window_days: 1e30`` killed the alert's evaluation every cycle).
MAX_WINDOW_DAYS = 36_500
#: Same reasoning for staleness, in seconds (1,000 days).
MAX_STALENESS_SECONDS = 86_400_000


class InvalidCondition(ValueError):
    """A condition the closed set does not recognise or cannot evaluate.

    Raised at creation so an unrecognised kind is a 400, not a silent
    never-fire row sitting in the table until someone wonders why nothing
    happened.
    """


def _finite_number(value: Any) -> float | None:
    """A usable threshold number, or None.

    JSON parses ``Infinity``, ``-Infinity`` and ``NaN`` by default and Python
    ints are unbounded, so "is a number" is not enough: a huge int overflows
    float(), NaN passes every isinstance check and then makes every
    comparison false -- a condition that looks set and can never fire. Only
    finite, convertible numbers are usable.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def validate_condition(condition: Any) -> dict:
    """Check a condition against the closed set and return it normalised.

    A condition that passed validation here is the only shape stored, so
    evaluate consumes trusted input and never has to branch on an unknown kind.
    """
    if not isinstance(condition, dict):
        raise InvalidCondition("condition must be a JSON object")
    kind = condition.get("kind")
    if not isinstance(kind, str):
        raise InvalidCondition("condition.kind is required")
    if kind not in KNOWN_KINDS:
        raise InvalidCondition(
            f"unknown condition '{kind}'; expected one of: {', '.join(sorted(KNOWN_KINDS))}"
        )

    if kind in ("value_above", "value_below"):
        threshold = _finite_number(condition.get("threshold"))
        if threshold is None:
            raise InvalidCondition(f"{kind}.threshold must be a finite number")
        field = condition.get("field", _DEFAULT_VALUE_FIELD)
        if not isinstance(field, str) or not field:
            raise InvalidCondition(f"{kind}.field must be a non-empty string")
        return {"kind": kind, "threshold": threshold, "field": field}

    if kind in ("pct_change_above", "pct_change_below"):
        pct = _finite_number(condition.get("pct"))
        if pct is None or pct <= 0:
            # A magnitude, not a signed direction: the kind carries the sign.
            raise InvalidCondition(f"{kind}.pct must be a positive finite number")
        window = _finite_number(condition.get("window_days"))
        if window is None or window < 1:
            raise InvalidCondition(f"{kind}.window_days must be a number >= 1")
        if window > MAX_WINDOW_DAYS:
            raise InvalidCondition(
                f"{kind}.window_days must be at most {MAX_WINDOW_DAYS}"
            )
        field = condition.get("field", _DEFAULT_VALUE_FIELD)
        if not isinstance(field, str) or not field:
            raise InvalidCondition(f"{kind}.field must be a non-empty string")
        return {
            "kind": kind,
            "pct": pct,
            "window_days": window,
            "field": field,
        }

    if kind == "staleness_exceeds":
        seconds = _finite_number(condition.get("seconds"))
        if seconds is None or seconds <= 0:
            raise InvalidCondition("staleness_exceeds.seconds must be a positive finite number")
        if seconds > MAX_STALENESS_SECONDS:
            raise InvalidCondition(
                f"staleness_exceeds.seconds must be at most {MAX_STALENESS_SECONDS}"
            )
        return {"kind": "staleness_exceeds", "seconds": seconds}

    return {"kind": "contradiction"}


def _loads(value: Any) -> Any:
    """Decode a JSONB column that asyncpg returns as its text form."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _claim_number(claim: dict, field: str) -> float | None:
    """The numeric at claim.value[field], or None if there is no number there.

    value is a JSONB object whose shape varies by claim type; the codebase
    convention is {"value": <number>}. The field name is a parameter to a fixed
    lookup, not an expression: this reads one key from one column and compares
    it, and that is all it will ever do.
    """
    value = _loads(claim.get("value"))
    if not isinstance(value, dict):
        return None
    raw = value.get(field)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _value_signature(claim: dict) -> str:
    """A stable string for a claim's value, to tell agreement from disagreement."""
    return json.dumps(_loads(claim.get("value")), sort_keys=True, default=str)


def _ordered(claims: list) -> list:
    """Claims oldest-first by knowledge_date, ties by id, values decoded.

    Crossing detection reads the sequence; the order claims come back from a
    query is not a sequence, it is an implementation detail.
    """
    return sorted(
        claims, key=lambda c: (c["knowledge_date"], str(c["id"]))
    )


def _level_predicate(condition: dict):
    """The per-claim predicate for the level kinds, as a callable."""
    kind = condition["kind"]
    field = condition["field"]
    threshold = condition["threshold"]

    def holds(c: dict) -> bool:
        v = _claim_number(c, field)
        if v is None:
            return False
        return v > threshold if kind == "value_above" else v < threshold

    return holds


def _pct_predicate(condition: dict, claims: list):
    """The per-claim predicate for the percent kinds, as a callable.

    A claim holds when its value has moved at least ``pct`` percent against the
    most recent claim at least ``window_days`` older -- "up 10% over the last
    30 days", measured claim to claim, never against a fabricated base. A claim
    with no such base (the window's own history) or a non-numeric base does not
    hold: an honest refusal, not a comparison against zero.
    """
    from bisect import bisect_right
    from datetime import timedelta

    kind = condition["kind"]
    field = condition["field"]
    pct = condition["pct"]
    window = timedelta(days=condition["window_days"])
    # Claims that never carried the watched field are not base candidates:
    # "no value" is not a price that existed, and treating it as one moves the
    # base of the series onto a row that never spoke for it.
    ordered = _ordered([c for c in claims if _claim_number(c, field) is not None])
    dates = [c["knowledge_date"] for c in ordered]

    def holds(c: dict) -> bool:
        v = _claim_number(c, field)
        if v is None:
            return False
        # The newest claim whose knowledge_date is at least the window older.
        idx = bisect_right(dates, c["knowledge_date"] - window) - 1
        if idx < 0:
            return False
        base = _claim_number(ordered[idx], field)
        if base is None or base == 0:
            return False
        change = (v - base) / base
        return change >= pct / 100 if kind == "pct_change_above" else change <= -pct / 100

    return holds


def _crossings(claims: list, holds) -> list:
    """The claims where the condition becomes true -- not stays true.

    This is the fix for the fires-on-every-claim defect: a level above a
    threshold used to fire a firing per claim (a daily close above 100 meant a
    notification per day, forever). A condition fires when it *crosses*: the
    claim holds and the claim before it did not (or there is no claim before
    it). Re-arming falls out of the same rule -- once above, further above
    claims are not crossings; after a dip below, the next above claim is.

    The (alert, claim) firing dedup remains the belt to these braces: a claim
    fires at most once ever, whatever the sequence does.
    """
    ordered = _ordered(claims)
    out = []
    prev_holds = False
    for c in ordered:
        now_holds = holds(c)
        if now_holds and not prev_holds:
            out.append(c)
        prev_holds = now_holds
    return out


def _satisfying(condition: dict, claims: list, now: datetime) -> list:
    """Pure: the claims this condition currently holds for.

    value_above / value_below / pct_change_* fire on crossings (see
    _crossings), computed PER SOURCE: claims for one (entity, claim_type) can
    arrive from several unrelated publishers, and interleaving them
    manufactures a crossing every time two publishers sit on opposite sides
    of the threshold -- two steady series read as an oscillation (audit A16).
    staleness_exceeds and contradiction are conditions over the *set* of
    claims; they fire on the concrete claim(s) that embody the condition, so
    the (alert, claim) dedup still pins them to a real row rather than a
    synthetic one.
    """
    kind = condition["kind"]

    if kind in ("value_above", "value_below", "pct_change_above", "pct_change_below"):
        out = []
        series: dict[str, list] = {}
        for c in claims:
            series.setdefault(c["source"], []).append(c)
        for source_claims in series.values():
            if kind in ("value_above", "value_below"):
                holds = _level_predicate(condition)
            else:
                holds = _pct_predicate(condition, source_claims)
            out.extend(_crossings(source_claims, holds))
        return out

    if kind == "staleness_exceeds":
        if not claims:
            # Nothing to be stale. An entity with no visible coverage has a
            # *missing* gap, not a stale one; staleness needs a reference claim
            # to be older than, and there is none. The gap engine owns "missing".
            return []
        newest = max(claims, key=lambda c: c["knowledge_date"])
        age = now - newest["knowledge_date"]
        if age > timedelta(seconds=condition["seconds"]):
            return [newest]
        return []

    # contradiction: two or more sources disagreeing on the same (key,
    # event_date) within this alert's (entity, claim_type). Every claim in a
    # disagreeing group is part of the condition, so each is a candidate
    # firing; the (alert, claim) dedup keeps it to one notification per claim.
    grouped: dict[tuple, list] = {}
    for c in claims:
        grouped.setdefault((c["key"], c["event_date"]), []).append(c)
    out = []
    for group in grouped.values():
        sources = {c["source"] for c in group}
        if len(sources) < 2:
            continue
        if len({_value_signature(c) for c in group}) >= 2:
            out.extend(group)
    return out


_FIRED_CLAIMS = "SELECT claim_id FROM alert_firing WHERE alert_id = $1"

# RETURNING claim_id is the dedup race fix (audit A09): under ON CONFLICT
# DO NOTHING a racing evaluation inserts nothing and gets nothing back, so
# each caller reports exactly the firings it recorded -- not the ones it
# merely observed as satisfying.
_INSERT_FIRING = """
INSERT INTO alert_firing (alert_id, claim_id)
VALUES ($1, $2)
ON CONFLICT (alert_id, claim_id) DO NOTHING
RETURNING claim_id
"""

_TOUCH_LAST_FIRED = "UPDATE alert SET last_fired_at = now() WHERE id = $1"


async def evaluate(pool, alert, *, audience: UUID | None, notify=None) -> list:
    """Record and return the claims that newly satisfy the alert's condition.

    Reads only through visible_claims scoped to ``audience`` (the alert owner);
    an alert never sees a claim its audience may not. Claims already recorded
    in alert_firing are skipped, and the INSERT ... ON CONFLICT ... RETURNING
    inside the transaction reports only the rows THIS evaluation inserted:
    the (alert, claim) primary key remains the dedup, but the returned set
    now matches what was actually written even when two evaluations race.

    ``notify``, when given, is awaited INSIDE the firing transaction as
    ``notify(conn, new_firings)``: the firing record and its notification
    queue rows commit or roll back together. Without that, a crash between
    the two transactions left firings nothing would ever deliver --
    evaluation skips already-fired claims, so those notifications were lost
    forever, not delayed. The first time the alert ever fires, one demand row
    is raised for its (entity, claim_type) -- the second effect of firing,
    that a watched condition is asked to stay covered.
    """
    condition = validate_condition(_loads(alert["condition"]))

    claims = await visible_claims(
        pool,
        audience=audience,
        entity_id=alert["entity_id"],
        claim_type=str(alert["claim_type"]),
    )
    satisfying = _satisfying(condition, claims, datetime.now(UTC))
    if not satisfying:
        return []

    already = {r["claim_id"] for r in await pool.fetch(_FIRED_CLAIMS, alert["id"])}
    candidates = [c for c in satisfying if c["id"] not in already]
    if not candidates:
        return []

    new: list = []
    async with pool.acquire() as conn, conn.transaction():
        for c in candidates:
            inserted = await conn.fetchval(_INSERT_FIRING, alert["id"], c["id"])
            if inserted is not None:
                new.append(c)
        if not new:
            # A racing evaluation recorded every candidate first; this one
            # wrote nothing and reports nothing.
            return []
        await conn.execute(_TOUCH_LAST_FIRED, alert["id"])

        # Raise demand once per alert: the first firing is the signal that this
        # user wants the thing kept covered. direct_attention hardcodes the
        # 'direct' channel (the same limitation watchlist-raised demand hits),
        # so alert-raised demand is indistinguishable from question-raised --
        # noted in the report as the one residual ambiguity.
        if not already:
            await direct_attention(
                conn,
                entity_id=alert["entity_id"],
                claim_type=str(alert["claim_type"]),
                requested_by=alert["user_id"],
            )

        if notify is not None:
            await notify(conn, new)

        # A one-shot's whole contract is "after it fires, stop watching".
        # Deactivating in the same transaction as the firing means there is no
        # window where the alert has fired and is still armed.
        if alert.get("one_shot"):
            await conn.execute(
                "UPDATE alert SET active = false WHERE id = $1", alert["id"]
            )

    return new


__all__ = [
    "KNOWN_KINDS",
    "InvalidCondition",
    "evaluate",
    "validate_condition",
]
