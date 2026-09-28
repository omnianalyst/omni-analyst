"""What a given audience is allowed to see.

Every read of the claim table goes through here. The rule is small but it is
the one that must never be got wrong, so it exists once rather than at each
call site:

    a user sees the shared network, plus their own private claims

"Shared" means redistributable and unowned. "Private" means fetched with that
user's own credential under terms that forbid passing the data on. There is no
third case: migration 001 constrains a claim to be exactly one of the two.

`audience=None` means the shared network alone — the correct default for
anything unauthenticated, and for computing what the network holds
independent of any user.
"""

from __future__ import annotations

import json
from uuid import UUID

# Deliberately a fragment rather than a view: callers need to compose it with
# their own filters, and a view would tempt them to bypass it with a join.
VISIBLE_CLAIMS = """
SELECT c.* FROM claim c
WHERE c.superseded_by IS NULL
  AND (
        (c.audience_user_id IS NULL AND c.redistributable = 'allowed')
     OR (c.audience_user_id IS NOT NULL AND c.audience_user_id = $1)
      )
"""


def visible_claims_cte(audience_param: str = "$1") -> str:
    """The visibility fragment as a CTE body, for composing into a larger query."""
    return VISIBLE_CLAIMS.replace("$1", audience_param)


async def visible_claim_summary(
    pool,
    *,
    audience: UUID | None,
    entity_id: UUID,
    claim_type: str,
    key: str | None,
) -> dict:
    """Summarize a demanded fact without materializing its claim history."""
    params = [audience, entity_id, claim_type]
    where = "c.entity_id = $2 AND c.claim_type = $3::claim_type"
    if key is not None:
        params.append(key)
        where += " AND c.key = $4"
    visible = f"WITH visible AS ({visible_claims_cte()}) "
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        row = await conn.fetchrow(
            visible
            + "SELECT count(*) AS count, max(c.knowledge_date) AS newest, "
            + "max(c.confidence) AS best, array_agg(DISTINCT c.source) AS sources "
            + f"FROM visible c WHERE {where}",
            *params,
        )
        conflicts = []
        if row["sources"] is not None and len(row["sources"]) > 1:
            conflicts = await conn.fetch(
                visible
                + "SELECT c.key, c.event_date, "
                + "array_agg(DISTINCT c.source ORDER BY c.source) AS sources, "
                + "min(c.value::text) AS first_value, max(c.value::text) AS last_value "
                + f"FROM visible c WHERE {where} "
                + "GROUP BY c.key, c.event_date "
                + "HAVING count(DISTINCT c.source) > 1 AND count(DISTINCT c.value) > 1 "
                + "ORDER BY max(c.knowledge_date) DESC, c.event_date DESC LIMIT 33",
                *params,
            )
    return {
        "count": row["count"],
        "newest": row["newest"],
        "best": row["best"],
        "sources": row["sources"] or [],
        "conflicts_truncated": len(conflicts) > 32,
        "conflicts": [
            {
                "key": conflict["key"],
                "event_date": conflict["event_date"].isoformat(),
                "sources": conflict["sources"],
                "values": [json.loads(conflict["first_value"]), json.loads(conflict["last_value"])],
            }
            for conflict in conflicts[:32]
        ],
    }


async def visible_claims(
    pool,
    *,
    audience: UUID | None,
    entity_id: UUID | None = None,
    claim_type: str | None = None,
    key: str | None = None,
) -> list:
    """Claims this audience may see, most recently knowable first."""
    conditions = [
        "c.superseded_by IS NULL",
        ("((c.audience_user_id IS NULL AND c.redistributable = 'allowed')"
         " OR (c.audience_user_id IS NOT NULL AND c.audience_user_id = $1))"),
    ]
    params: list = [audience]

    for column, value in (
        ("entity_id", entity_id),
        ("claim_type", claim_type),
        ("key", key),
    ):
        if value is not None:
            params.append(value)
            cast = "::claim_type" if column == "claim_type" else ""
            conditions.append(f"c.{column} = ${len(params)}{cast}")

    sql = (
        "SELECT c.* FROM claim c WHERE "
        + " AND ".join(conditions)
        + " ORDER BY c.knowledge_date DESC, c.event_date DESC"
    )
    return await pool.fetch(sql, *params)
