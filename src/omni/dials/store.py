"""Editorial dials, stored as bitemporal claims.

A dial is an editorial parameter an analysis consumes: a prior, a weight, a
baseline, a multiplier. Held as a module constant, a dial rewrites history every
time it is edited -- a backtest run today applies today's value to five-year-old
data, and nothing in the record shows the number in force back then was
different. Held as a claim, an edit is a *new* claim with its own
`knowledge_date`, so a point-in-time read returns the dial that was actually in
force. Every function here exists to make that the only way to read one.

**A dial is never a threshold.** It may be consumed as a feature by a
`DerivedCapability`; it may never set a surfacing cutoff and must never be read
by `conviction/gate.py`. That gate's thresholds come from the resolved-prediction
record and are never chosen, which is what makes "high conviction" mean
something measurable rather than something someone felt. A dial is chosen by
definition, so routing one into the gate would put an opinion in the position of
a statistic and quietly convert the calibration story into decoration. Dials
feed the analysis; calibration alone decides whether its output is spoken.

**`as_of` is required and has no default.** A default of "now" is how
point-in-time discipline erodes: every caller that forgets the argument silently
becomes non-point-in-time and no test fails. Making it required means forgetting
it is a `TypeError` at the call site.

**A missing dial reads as `None`, never as a default.** A fallback invented in
here is an editorial parameter nobody set, appearing in output with the
authority of one that was. A caller that needs a fallback states it at its own
call site, where it is visible.

Values are `Decimal` and are stored as JSON strings, so a dial round-trips
exactly rather than through binary64.

A global dial has no entity, but `claim.entity_id` is NOT NULL, so global dials
are anchored on a single reserved entity row (`kind='dial_scope'`,
`symbol='global'`) created on first write. `Dial.entity_id` reports `None` for
it, so the anchor is a storage detail and never reaches a caller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from omni.coverage.visibility import visible_claims_cte

_GLOBAL_SCOPE_KIND = "dial_scope"
_GLOBAL_SCOPE_SYMBOL = "global"
_GLOBAL_SCOPE_NAME = "global dial scope"


@dataclass(frozen=True)
class Dial:
    """One version of one dial: the value in force from `knowledge_date` on."""

    name: str
    entity_id: UUID | None
    value: Decimal
    methodology_version: str
    event_date: datetime
    knowledge_date: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise TypeError(
                f"dial {self.name!r} value must be Decimal, got "
                f"{type(self.value).__name__}"
            )
        # NaN and inf need their own refusal: every comparison against NaN is
        # false, so a range check written as a comparison passes it through and
        # the caller gets a labelled parameter computed from nothing.
        if self.value.is_nan() or self.value.is_infinite():
            raise ValueError(f"dial {self.name!r} value is not finite: {self.value}")
        if self.knowledge_date < self.event_date:
            raise ValueError(
                f"dial {self.name!r} knowledge_date {self.knowledge_date} "
                f"precedes event_date {self.event_date}"
            )


_UPSERT_GLOBAL_SCOPE = """
INSERT INTO entity (kind, symbol, name) VALUES ($1, $2, $3)
ON CONFLICT (kind, symbol) DO UPDATE SET name = EXCLUDED.name
RETURNING id
"""

_SELECT_GLOBAL_SCOPE = "SELECT id FROM entity WHERE kind = $1 AND symbol = $2"

_INSERT = """
INSERT INTO claim (entity_id, claim_type, key, value, source, event_date,
                   knowledge_date, confidence, redistributable,
                   audience_user_id, derivation)
VALUES ($1, 'dial'::claim_type, $2, $3::jsonb, $4, $5, $6, 1.0,
        $7::redistribution, $8, 'ingested'::claim_derivation)
ON CONFLICT DO NOTHING
RETURNING id
"""

_SELECT_EXISTING = """
SELECT id, value FROM claim
WHERE entity_id = $1 AND claim_type = 'dial' AND key = $2 AND source = $3
  AND event_date = $4 AND knowledge_date = $5
  AND audience_user_id IS NOT DISTINCT FROM $6
"""


def _decimal_from(row_value, name: str) -> Decimal:
    payload = row_value
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or "value" not in payload:
        raise ValueError(f"dial claim for {name!r} has no value: {row_value!r}")
    return Decimal(str(payload["value"]))


async def _scope_id(pool, entity_id: UUID | None, *, create: bool) -> UUID | None:
    if entity_id is not None:
        return entity_id
    if create:
        return await pool.fetchval(
            _UPSERT_GLOBAL_SCOPE,
            _GLOBAL_SCOPE_KIND,
            _GLOBAL_SCOPE_SYMBOL,
            _GLOBAL_SCOPE_NAME,
        )
    return await pool.fetchval(
        _SELECT_GLOBAL_SCOPE, _GLOBAL_SCOPE_KIND, _GLOBAL_SCOPE_SYMBOL
    )


def _read_query(*, point_in_time: bool) -> str:
    knowledge_filter = "AND c.knowledge_date <= $4" if point_in_time else ""
    direction = "DESC" if point_in_time else "ASC"
    limit = "LIMIT 1" if point_in_time else ""
    return f"""
        WITH visible AS (
        {visible_claims_cte("$1")}
        )
        SELECT c.value, c.source, c.event_date, c.knowledge_date
        FROM visible c
        WHERE c.entity_id = $2 AND c.claim_type = 'dial' AND c.key = $3
          {knowledge_filter}
        ORDER BY c.knowledge_date {direction}, c.event_date {direction},
                 c.observed_at {direction}
        {limit}
    """


async def set_dial(
    pool,
    *,
    name: str,
    entity_id: UUID | None,
    value: Decimal,
    methodology_version: str,
    event_date: datetime,
    knowledge_date: datetime,
    audience_user_id: UUID | None = None,
) -> UUID:
    """Record a dial value, knowable from `knowledge_date` on.

    Confidence is 1.0 because the claim is "this dial was set to this value",
    which is certain -- it is not an estimate of anything.

    Re-recording the identical value at the same coordinates is a no-op that
    returns the existing claim id, matching ingestion idempotency. Recording a
    *different* value at coordinates already occupied raises: that is an attempt
    to rewrite a dial's history in place, which is the failure this module
    exists to make impossible.
    """
    dial = Dial(
        name=name,
        entity_id=entity_id,
        value=value,
        methodology_version=methodology_version,
        event_date=event_date,
        knowledge_date=knowledge_date,
    )
    scope_id = await _scope_id(pool, entity_id, create=True)
    redistributable = "allowed" if audience_user_id is None else "byo_only"

    claim_id = await pool.fetchval(
        _INSERT,
        scope_id,
        dial.name,
        json.dumps({"value": str(dial.value)}),
        dial.methodology_version,
        dial.event_date,
        dial.knowledge_date,
        redistributable,
        audience_user_id,
    )
    if claim_id is not None:
        return claim_id

    existing = await pool.fetchrow(
        _SELECT_EXISTING,
        scope_id,
        dial.name,
        dial.methodology_version,
        dial.event_date,
        dial.knowledge_date,
        audience_user_id,
    )
    held = _decimal_from(existing["value"], dial.name)
    if held != dial.value:
        raise ValueError(
            f"dial {dial.name!r} is already {held} at knowledge_date "
            f"{dial.knowledge_date} under {dial.methodology_version}; a new "
            "value needs a new knowledge_date, not an overwrite"
        )
    return existing["id"]


async def get_dial(
    pool,
    *,
    name: str,
    entity_id: UUID | None,
    as_of: datetime,
    audience_user_id: UUID | None = None,
) -> Dial | None:
    """The dial in force as-of `as_of`, or None if none was knowable by then.

    Point-in-time by construction: a dial recorded after `as_of` is invisible,
    so a caller cannot read a value that had not been set yet. None means unset
    and is never substituted for.
    """
    scope_id = await _scope_id(pool, entity_id, create=False)
    if scope_id is None:
        return None
    row = await pool.fetchrow(
        _read_query(point_in_time=True), audience_user_id, scope_id, name, as_of
    )
    if row is None:
        return None
    return Dial(
        name=name,
        entity_id=entity_id,
        value=_decimal_from(row["value"], name),
        methodology_version=row["source"],
        event_date=row["event_date"],
        knowledge_date=row["knowledge_date"],
    )


async def history(
    pool,
    *,
    name: str,
    entity_id: UUID | None,
    audience_user_id: UUID | None = None,
) -> list[Dial]:
    """Every recorded version of this dial, oldest knowable first."""
    scope_id = await _scope_id(pool, entity_id, create=False)
    if scope_id is None:
        return []
    rows = await pool.fetch(
        _read_query(point_in_time=False), audience_user_id, scope_id, name
    )
    return [
        Dial(
            name=name,
            entity_id=entity_id,
            value=_decimal_from(row["value"], name),
            methodology_version=row["source"],
            event_date=row["event_date"],
            knowledge_date=row["knowledge_date"],
        )
        for row in rows
    ]
