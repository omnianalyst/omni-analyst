"""Demand minus coverage, computed per audience.

Every claim read goes through omni.coverage.visibility.visible_claims. A gap
computed against claims the audience cannot see is a redistribution leak, so
this module never queries the claim table directly: it asks visibility for what
the demand's audience may see, and classifies what comes back.

Each active demand row is evaluated against the claims visible to that demand's
own audience (its `requested_by`). The optional `audience` argument to
detect_gaps narrows *which demands* are processed; it never overrides the
audience a demand is evaluated for, because that is the step the rule cannot
afford to get wrong.

Five classes, ordered by how urgently the network should surface them. The
ordering is load-bearing: it decides which gap a worker picks up first, and a
quiet ordering is a quiet network. See GAP_CLASS_WEIGHTS.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from omni.coverage.visibility import visible_claims

# The partial unique index from migration 001, spelled exactly as the index
# expression so ON CONFLICT can infer against it. The COALESCE columns are why
# persisting twice for the same (entity, type, key, class, audience) updates
# rather than raising: two NULL keys collapse to '' in the index.
_GAP_OPEN_TARGET = (
    "(entity_id, claim_type, COALESCE(key, ''), gap_class, "
    "COALESCE(audience_user_id, '00000000-0000-0000-0000-000000000000'::uuid)) "
    "WHERE resolved_at IS NULL"
)

# Higher is more urgent. The score is `summed_demand_weight * class_weight`.
#
# contradictory is ranked first because a conflict the network found on its own
# is the highest-signal thing it can say: two sources looked at the same fact
# and disagreed, which means either a data fault or a real-world change worth a
# human's attention. missing comes next: there is nothing to serve at all.
# stale beats low_confidence because a fact about an old state is actively
# misleading, whereas a low-confidence fact is at least honestly labelled.
# unverified is last: the claim exists and may well be correct; it only lacks
# a second source, and corroboration is the gentlest of the five to want.
GAP_CLASS_WEIGHTS: dict[str, float] = {
    "contradictory": 1000.0,
    "missing": 100.0,
    "stale": 10.0,
    "low_confidence": 5.0,
    "unverified": 1.0,
}


async def detect_gaps(pool, *, audience: UUID | None = None) -> list[dict]:
    """Classify the gap between active demand and visible coverage.

    With no `audience`, every active demand is evaluated against its own
    `requested_by`. With `audience=X`, only demands requested by X are
    processed (each still evaluated against X). Network demands
    (`requested_by IS NULL`) are therefore covered by the default full scan,
    not by a user-filtered one.
    """
    if audience is None:
        demands = await pool.fetch(
            "SELECT id, entity_id, claim_type, key, requested_by, weight, "
            "max_staleness, min_confidence FROM demand WHERE active"
        )
    else:
        demands = await pool.fetch(
            "SELECT id, entity_id, claim_type, key, requested_by, weight, "
            "max_staleness, min_confidence FROM demand "
            "WHERE active AND requested_by = $1",
            audience,
        )

    # Demands for the same fact amplify its urgency rather than double-insert.
    # Group by identity and sum weights; the strictest freshness/confidence
    # threshold in the group wins, because a single pickier requester makes
    # the shared gap as hard to satisfy as the hardest of its demands.
    grouped: dict[tuple, dict] = {}
    for d in demands:
        gkey = (
            d["entity_id"],
            str(d["claim_type"]),
            d["key"],
            d["requested_by"],
        )
        bucket = grouped.get(gkey)
        if bucket is None:
            bucket = {
                "entity_id": d["entity_id"],
                "claim_type": str(d["claim_type"]),
                "key": d["key"],
                "audience": d["requested_by"],
                "weight": 0.0,
                "max_staleness": None,
                "min_confidence": None,
                "demand_ids": [],
            }
            grouped[gkey] = bucket
        bucket["weight"] += d["weight"]
        bucket["demand_ids"].append(d["id"])
        if d["max_staleness"] is not None and (
            bucket["max_staleness"] is None
            or d["max_staleness"] < bucket["max_staleness"]
        ):
            bucket["max_staleness"] = d["max_staleness"]
        if d["min_confidence"] is not None and (
            bucket["min_confidence"] is None
            or d["min_confidence"] > bucket["min_confidence"]
        ):
            bucket["min_confidence"] = d["min_confidence"]

    gaps: list[dict] = []
    now = datetime.now(UTC)
    for group in grouped.values():
        claims = await visible_claims(
            pool,
            audience=group["audience"],
            entity_id=group["entity_id"],
            claim_type=group["claim_type"],
            key=group["key"],
        )
        gaps.extend(_classify(group, claims, now))
    return gaps


def _classify(group: dict, claims: list, now: datetime) -> list[dict]:
    base = {
        "entity_id": group["entity_id"],
        "claim_type": group["claim_type"],
        "key": group["key"],
        "audience_user_id": group["audience"],
        "demand_ids": [str(d) for d in group["demand_ids"]],
    }

    # No visible claim is its own outcome. The other four classes are claims
    # about claims, so they cannot fire when there is nothing to describe; a
    # null-valued claim still counts as coverage here (FRED's "no figure
    # published yet"), which is what stops the engine from re-requesting a
    # known hole forever.
    if not claims:
        return [_gap(base, "missing", group["weight"], {"reason": "no visible claim"})]

    out: list[dict] = []

    newest = max(c["knowledge_date"] for c in claims)
    if group["max_staleness"] is not None:
        age = now - newest
        if age > group["max_staleness"]:
            out.append(
                _gap(
                    base,
                    "stale",
                    group["weight"],
                    {
                        "newest_knowledge_date": newest.isoformat(),
                        "age_seconds": age.total_seconds(),
                        "max_staleness_seconds": group["max_staleness"].total_seconds(),
                    },
                )
            )

    best = max(c["confidence"] for c in claims)
    if group["min_confidence"] is not None and best < group["min_confidence"]:
        out.append(
            _gap(
                base,
                "low_confidence",
                group["weight"],
                {"best_confidence": best, "min_confidence": group["min_confidence"]},
            )
        )

    # unverified counts distinct sources, not rows: two vintages from the same
    # source are one voice, not corroboration.
    sources = {c["source"] for c in claims}
    if len(sources) == 1:
        out.append(
            _gap(
                base,
                "unverified",
                group["weight"],
                {"sole_source": next(iter(sources))},
            )
        )

    conflicts = _find_contradictions(claims)
    if conflicts:
        out.append(_gap(base, "contradictory", group["weight"], {"conflicts": conflicts}))

    return out


def _find_contradictions(claims: list) -> list[dict]:
    """Same (key, event_date) seen by two sources with two different values.

    This is the class usually forgotten and the most valuable one to surface,
    so it is detected independently of how complete coverage otherwise looks:
    a demand with two corroborating sources on one event_date and a conflict
    on another still has a contradictory gap.
    """
    by_key_event: dict[tuple, list] = {}
    for c in claims:
        by_key_event.setdefault((c["key"], c["event_date"]), []).append(c)

    conflicts: list[dict] = []
    for (key, event_date), group in by_key_event.items():
        sources = {c["source"] for c in group}
        if len(sources) < 2:
            continue
        values = {json.dumps(c["value"], sort_keys=True) for c in group}
        if len(values) >= 2:
            conflicts.append(
                {
                    "key": key,
                    "event_date": event_date.isoformat(),
                    "sources": sorted(sources),
                    "values": [c["value"] for c in group],
                }
            )
    return conflicts


def _gap(base: dict, gap_class: str, weight: float, detail: dict) -> dict:
    merged: dict[str, Any] = {
        **base,
        "gap_class": gap_class,
        "score": weight * GAP_CLASS_WEIGHTS[gap_class],
        "detail": detail,
    }
    return merged


async def persist_gaps(pool, gaps: list[dict]) -> int:
    """Write gaps, refreshing score/detail/detected_at if the gap already exists.

    The partial unique index permits exactly one open gap per identity, so a
    re-run of detection updates in place rather than producing a queue full of
    duplicates. A gap that was resolved is untouched here -- it reopens only by
    being detected again after its resolved row is in the past, which the index
    allows because its predicate excludes resolved rows.
    """
    if not gaps:
        return 0

    sql = f"""
        INSERT INTO gap (entity_id, claim_type, key, gap_class,
                         audience_user_id, score, detail)
        VALUES ($1, $2::claim_type, $3, $4::gap_class, $5, $6, $7::jsonb)
        ON CONFLICT {_GAP_OPEN_TARGET}
        DO UPDATE SET score = EXCLUDED.score,
                      detail = EXCLUDED.detail,
                      detected_at = now()
    """

    count = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for g in gaps:
                await conn.execute(
                    sql,
                    g["entity_id"],
                    g["claim_type"],
                    g["key"],
                    g["gap_class"],
                    g["audience_user_id"],
                    g["score"],
                    json.dumps(g["detail"]),
                )
                count += 1
    return count


async def resolve_gap(pool, gap_id: UUID) -> bool:
    """Close an open gap. Returns False if it was already resolved or absent."""
    status = await pool.execute(
        "UPDATE gap SET resolved_at = now() WHERE id = $1 AND resolved_at IS NULL",
        gap_id,
    )
    return status.endswith("1")
