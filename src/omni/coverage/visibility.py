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
