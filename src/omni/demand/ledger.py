"""The demand ledger — what has been asked for, by whom, how urgently.

Demand is the input to the gap engine: gaps are demand minus coverage, so
nothing is fetched that nobody wants. A demand row is the unit of attention.

Two invariants are load-bearing here:

  * Demand is deactivated, never deleted. The ledger is a record of what was
    asked for, and that history is what later tells us which entities are
    persistently wanted. `withdraw` sets `active = false` and nothing more.
  * Demand is not deduplicated on write. Two users asking for the same thing
    are two rows; the aggregation that collapses them is `rank`, and the fact
    that more than one user cared is itself the signal `rank` reports as
    `requester_count`.

`weight` must be positive. The schema enforces it (CHECK weight > 0); this
module does not catch the violation, because a demand entry that silently
swallowed a bad weight would be exactly the kind of dishonest coverage the
store exists to avoid.
"""

from __future__ import annotations

from typing import Any

# `direct_attention` is the one demand channel Slice 0 supports; the column is
# NOT NULL, so every row carries the channel that produced it.
_DIRECT_CHANNEL = "direct"
_AUTONOMOUS_CHANNEL = "autonomous"

_INSERT_DEMAND = """
INSERT INTO demand (entity_id, claim_type, key, channel, requested_by,
                    weight, max_staleness, min_confidence)
VALUES ($1, $2::claim_type, $3, $4, $5, $6, $7, $8)
RETURNING id
"""


async def direct_attention(
    pool,
    *,
    entity_id: Any,
    claim_type: str,
    key: str | None = None,
    requested_by: Any | None = None,
    weight: float = 1.0,
    max_staleness: Any | None = None,
    min_confidence: float | None = None,
):
    """Record explicit demand and return its id.

    This is the one demand channel Slice 0 supports. A non-positive `weight`
    is rejected by the schema; the resulting `CheckViolationError` is allowed
    to propagate rather than be masked as a successful write.
    """
    return await pool.fetchval(
        _INSERT_DEMAND,
        entity_id,
        claim_type,
        key,
        _DIRECT_CHANNEL,
        requested_by,
        weight,
        max_staleness,
        min_confidence,
    )


async def autonomous_attention(
    pool,
    *,
    entity_id: Any,
    claim_type: str,
    key: str | None = None,
    requested_by: Any | None = None,
    weight: float = 0.5,
    max_staleness: Any | None = None,
    min_confidence: float | None = None,
):
    """Record autonomous demand -- the system's own attention, not a user's.

    The autonomous layer (AUTONOMOUS_PLAN.md Loop 8) directs the system at
    sectors and stocks it finds interesting, creating demand the existing
    sweep/fill/predict chain closes. The channel distinguishes system curiosity
    from a user's explicit ask, and the default weight (0.5) is below a user's
    (1.0) so autonomous work never starves user-directed work in the fill queue.

    ``requested_by`` should be the operator's user_id on a single-operator
    deployment. Without it, byo_only providers (Polygon) cannot attribute the
    fetched data and the gap stays unfillable -- the fill pipeline resolves
    ``credential_owner`` from the demand's ``requested_by``, and a NULL there
    means ``MissingCredentialOwner``. The AutonomousRunner resolves the
    operator at startup and passes it through. If None (no users yet), the
    demand is still recorded but only fillable by ``allowed``-class providers.
    """
    return await pool.fetchval(
        _INSERT_DEMAND,
        entity_id,
        claim_type,
        key,
        _AUTONOMOUS_CHANNEL,
        requested_by,
        weight,
        max_staleness,
        min_confidence,
    )


async def withdraw(pool, demand_id: Any) -> None:
    """Deactivate a demand row. Demand is never deleted."""
    await pool.execute(
        "UPDATE demand SET active = false WHERE id = $1", demand_id
    )


async def active_demand(
    pool,
    *,
    entity_id: Any | None = None,
    requested_by: Any | None = None,
) -> list:
    """Active demand rows, highest weight first.

    Either filter may be omitted. Inactive rows never appear here: a stale want
    that still reads back as active is worse than none at all, for the same
    reason a stale claim is.
    """
    conditions = ["active"]
    params: list = []
    for column, value in (("entity_id", entity_id), ("requested_by", requested_by)):
        if value is not None:
            params.append(value)
            conditions.append(f"{column} = ${len(params)}")
    sql = (
        "SELECT * FROM demand WHERE "
        + " AND ".join(conditions)
        + " ORDER BY weight DESC, created_at DESC"
    )
    return await pool.fetch(sql, *params)


_RANK_SQL = """
SELECT entity_id,
       claim_type,
       key,
       sum(weight) AS total_weight,
       count(DISTINCT requested_by) AS requester_count
FROM demand
WHERE active
GROUP BY entity_id, claim_type, key
ORDER BY total_weight DESC, requester_count DESC
"""


async def rank(pool, *, limit: int | None = None) -> list:
    """Aggregate active demand per (entity_id, claim_type, key), highest first.

    `total_weight` sums demand across users. `requester_count` is the number of
    distinct users who asked, because two users wanting the same thing is
    stronger demand than one user wanting it twice as much, and the summed
    weight alone cannot tell those apart. Both are computed in SQL.

    Inactive demand is excluded: a withdrawn want must not still steer the gap
    engine.
    """
    if limit is not None:
        return await pool.fetch(_RANK_SQL + " LIMIT $1", limit)
    return await pool.fetch(_RANK_SQL)
