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
(alert_id, claim_id) primary key is the real dedup; evaluate's skip of
already-fired rows is the efficiency that keeps it from recomputing them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from omni.coverage.visibility import visible_claims
from omni.demand.ledger import direct_attention

# The closed set of condition kinds. Adding one means writing its predicate in
# _satisfying and a test; the set is spelled out so a typo in the stored JSONB
# cannot widen what evaluate will run.
KNOWN_KINDS = frozenset({"value_above", "value_below", "staleness_exceeds", "contradiction"})

_DEFAULT_VALUE_FIELD = "value"


class InvalidCondition(ValueError):
    """A condition the closed set does not recognise or cannot evaluate.

    Raised at creation so an unrecognised kind is a 400, not a silent
    never-fire row sitting in the table until someone wonders why nothing
    happened.
    """


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
        threshold = condition.get("threshold")
        # bool is an int subclass; reject it explicitly so `true` is not a 1.
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise InvalidCondition(f"{kind}.threshold must be a number")
        field = condition.get("field", _DEFAULT_VALUE_FIELD)
        if not isinstance(field, str) or not field:
            raise InvalidCondition(f"{kind}.field must be a non-empty string")
        return {"kind": kind, "threshold": float(threshold), "field": field}

    if kind == "staleness_exceeds":
        seconds = condition.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise InvalidCondition("staleness_exceeds.seconds must be a number")
        if seconds <= 0:
            raise InvalidCondition("staleness_exceeds.seconds must be positive")
        return {"kind": "staleness_exceeds", "seconds": float(seconds)}

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


def _satisfying(condition: dict, claims: list, now: datetime) -> list:
    """Pure: the claims this condition currently holds for.

    value_above / value_below are per-claim. staleness_exceeds and
    contradiction are conditions over the *set* of claims; they fire on the
    concrete claim(s) that embody the condition, so the (alert, claim) dedup
    still pins them to a real row rather than a synthetic one.
    """
    kind = condition["kind"]

    if kind in ("value_above", "value_below"):
        field = condition["field"]
        threshold = condition["threshold"]
        out = []
        for c in claims:
            v = _claim_number(c, field)
            if v is None:
                continue
            if (kind == "value_above" and v > threshold) or (
                kind == "value_below" and v < threshold
            ):
                out.append(c)
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

_INSERT_FIRING = """
INSERT INTO alert_firing (alert_id, claim_id)
VALUES ($1, $2)
ON CONFLICT (alert_id, claim_id) DO NOTHING
"""

_TOUCH_LAST_FIRED = "UPDATE alert SET last_fired_at = now() WHERE id = $1"


async def evaluate(pool, alert, *, audience: UUID | None) -> list:
    """Record and return the claims that newly satisfy the alert's condition.

    Reads only through visible_claims scoped to ``audience`` (the alert owner);
    an alert never sees a claim its audience may not. Claims already recorded
    in alert_firing are skipped, so a condition that remains true produces one
    firing per claim rather than one per evaluation. The first time the alert
    ever fires, one demand row is raised for its (entity, claim_type) -- the
    second effect of firing, that a watched condition is asked to stay covered.
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
    new = [c for c in satisfying if c["id"] not in already]
    if not new:
        return []

    async with pool.acquire() as conn, conn.transaction():
        for c in new:
            await conn.execute(_INSERT_FIRING, alert["id"], c["id"])
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

    return new


__all__ = [
    "KNOWN_KINDS",
    "InvalidCondition",
    "evaluate",
    "validate_condition",
]
