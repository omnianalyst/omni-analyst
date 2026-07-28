"""The derived capabilities that actually run today.

Sibling of ``builtin``: where that module binds ingestion adapters, this one
binds derivations -- claims computed from claims already in the store. Each
derivation names its inputs (so the planner can route a gap to it) and carries
``touches_byo=True``, because whether the output is shareable depends on the
licences of inputs a static descriptor cannot see. Over-restricting is the
safe direction; the actual decision is made per-fill by ``resolve_derived_licence``.

The bound ``gather`` reads inputs only through ``visible_claims`` scoped to
the gap's audience, never the claim table directly -- reading a private input
another user cannot see into a derivation would be the redistribution leak the
licence rule exists to prevent.
"""

from __future__ import annotations

import json
from uuid import UUID

from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.coverage.visibility import visible_claims
from omni.fill.derived import fill_derived
from omni.perception.divergence import DivergenceInput, compute_divergence

NAME = "perception.divergence"
PRODUCES = "perception_divergence"
CONSUMES = ("perception_macro", "fundamental_metric")


def _scalar(raw) -> float | None:
    """Pull a float out of a claim's JSONB ``value``.

    asyncpg returns jsonb columns as text (no codec is set on the pool), so a
    string is decoded first. A null-valued observation -- FRED's "." for an
    unpublished period -- has no scalar to extract and is skipped: it is
    coverage of the period, not an input to a computation.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    val = raw.get("value")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_inputs(rows) -> list[DivergenceInput]:
    out: list[DivergenceInput] = []
    for r in rows:
        val = _scalar(r["value"])
        if val is None:
            continue
        out.append(
            DivergenceInput(
                id=r["id"],
                event_date=r["event_date"],
                knowledge_date=r["knowledge_date"],
                value=val,
                redistributable=r["redistributable"],
                audience_user_id=r["audience_user_id"],
            )
        )
    return out


async def gather_divergence_inputs(
    pool, gap
) -> tuple[list[DivergenceInput], list[DivergenceInput]]:
    """Load perception_macro and fundamental_metric claims visible to the gap's
    audience, as DivergenceInputs.

    Two streams, in the order ``compute_divergence`` expects them: perception
    first, facts second. Each is read through ``visible_claims`` scoped to the
    gap's ``audience_user_id``, so an input private to another user is never
    gathered and therefore never reaches a derivation it must not feed.
    """
    audience: UUID | None = gap["audience_user_id"]
    entity_id = gap["entity_id"]

    perception_rows = await visible_claims(
        pool, audience=audience, entity_id=entity_id, claim_type=CONSUMES[0]
    )
    fact_rows = await visible_claims(
        pool, audience=audience, entity_id=entity_id, claim_type=CONSUMES[1]
    )
    return _to_inputs(perception_rows), _to_inputs(fact_rows)


def compute_divergence_claim(
    perception: list[DivergenceInput], facts: list[DivergenceInput]
):
    """Adapt ``compute_divergence`` to the fill path's ``(draft, ids) | None``
    contract, declaring every input as an edge."""
    draft = compute_divergence(perception, facts)
    if draft is None:
        return None
    return draft, [c.id for c in (*perception, *facts)]


def build_derived_registry() -> Registry:
    """Register the derived capabilities that are wired and tested today."""
    registry = Registry()

    async def call(pool, gap):
        return await fill_derived(
            pool, gap, compute=compute_divergence_claim,
            gather=gather_divergence_inputs,
        )

    registry.add(
        Capability(
            name=NAME,
            description=(
                "Cross-domain divergence: fundamentals improving while "
                "perception deteriorates (or the reverse). Computable only "
                "because perception and fundamentals live in one store."
            ),
            consumes=CONSUMES,
            produces=(PRODUCES,),
            touches_byo=True,
            cost=0.1,
            maturity=Maturity.WIRED,
            callability=Callability.YES,
            origin="omni.perception.divergence",
            call=call,
        )
    )
    return registry
