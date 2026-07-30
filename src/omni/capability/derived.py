"""The derived capabilities that actually run today.

Sibling of ``builtin``: where that module binds ingestion adapters, this one
binds derivations -- claims computed from claims already in the store.
``perception.divergence`` is restated here as a **declaration**: two
``ArgumentSpec``s (``perception_macro`` and ``fundamental_metric``) plus the
existing ``compute_divergence``. ``fill_analysis`` materializes the arguments
through ``visible_claims`` (audience-scoped), abstains if either is short,
and calls the compute adapter.

The adapter (``compute_divergence_declared``) bridges the shape mismatch
between ``materialize``'s output (``Materialized``: values + claim_ids) and
``compute_divergence``'s input (``list[DivergenceInput]``: values + dates +
licence). See its docstring for where and why.

``touches_byo=True`` is the safe-direction planning hint; the actual decision
is made per-fill by ``resolve_derived_licence`` over the materialized inputs.
``consumes`` is derived from the spec claim types, not hand-written.
"""

from __future__ import annotations

import json
from uuid import UUID

from omni.capability.arguments import ArgumentSpec, Materialized
from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.coverage.visibility import visible_claims, visible_claims_cte
from omni.fill.derived import DerivedCapability, fill_analysis
from omni.perception.divergence import (
    DEFAULT_WINDOW,
    MIN_HISTORY,
    DivergenceInput,
    compute_divergence,
)

NAME = "perception.divergence"
PRODUCES = "perception_divergence"

ARGUMENTS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        name="perception_macro",
        claim_type="perception_macro",
        shape="series",
        transform="level",
        min_obs=MIN_HISTORY,
    ),
    ArgumentSpec(
        name="fundamental_metric",
        claim_type="fundamental_metric",
        shape="series",
        transform="level",
        min_obs=MIN_HISTORY,
    ),
)

CONSUMES = tuple(spec.claim_type for spec in ARGUMENTS)


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


_INPUTS_BY_IDS = f"""
SELECT c.id, c.event_date, c.knowledge_date, c.value,
       c.redistributable, c.audience_user_id
FROM ({visible_claims_cte()}) c
WHERE c.id = ANY($2::uuid[])
"""


async def _to_inputs_by_ids(
    pool, audience, claim_ids
) -> list[DivergenceInput]:
    """Re-read claims by id through ``visible_claims`` as ``DivergenceInput``s.

    Audience scoping is preserved: a private claim of another user that
    ``materialize`` correctly excluded is not present here either.
    """
    if not claim_ids:
        return []
    rows = await pool.fetch(_INPUTS_BY_IDS, audience, list(claim_ids))
    return _to_inputs(rows)


async def compute_divergence_declared(
    pool,
    gap,
    *,
    perception_macro: Materialized,
    fundamental_metric: Materialized,
):
    """Declared-path compute: adapt ``Materialized`` to ``DivergenceInput`` lists.

    **The shape mismatch** (D5's specific risk): ``materialize`` (D4) returns
    ``Materialized(value=list[float], claim_ids=tuple[UUID, ...])`` -- the
    levels and their provenance, but not the ``event_date`` /
    ``knowledge_date`` each claim carries. ``compute_divergence`` expects
    ``list[DivergenceInput]`` (id, event_date, knowledge_date, value,
    redistributable, audience_user_id) because the rolling-z engine indexes
    on ``event_date`` and the bitemporal rule sets ``knowledge_date`` to the
    newest input's. Those date fields are not in ``Materialized`` and cannot
    be added without changing ``arguments.py`` (D4, forbidden here).

    The adapter therefore re-reads the claims by id through ``visible_claims``
    (audience-scoped, the sanctioned reader) and builds the
    ``DivergenceInput`` lists ``compute_divergence`` validates. This is the
    single place where the ``Materialized``-to-``DivergenceInput`` conversion
    happens, so a future analysis that hits the same mismatch knows where to
    look.
    """
    audience = gap["audience_user_id"]
    perc = await _to_inputs_by_ids(
        pool, audience, perception_macro.claim_ids
    )
    facts = await _to_inputs_by_ids(
        pool, audience, fundamental_metric.claim_ids
    )
    return compute_divergence(perc, facts)


async def gather_divergence_inputs(
    pool, gap
) -> tuple[list[DivergenceInput], list[DivergenceInput]]:
    """Load perception_macro and fundamental_metric claims visible to the gap's
    audience, as DivergenceInputs.

    The injected-gather escape hatch: kept as the template for a capability
    whose inputs the declaration cannot describe (multi-entity panels,
    caller-built structures). Divergence itself now uses the declared path.
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
    """Adapt ``compute_divergence`` to the escape-hatch fill path's
    ``(draft, ids) | None`` contract, declaring every input as an edge."""
    draft = compute_divergence(perception, facts)
    if draft is None:
        return None
    return draft, [c.id for c in (*perception, *facts)]


DERIVED = DerivedCapability(
    name=NAME,
    arguments=ARGUMENTS,
    compute=compute_divergence_declared,
)


def build_derived_registry() -> Registry:
    """Register the derived capabilities that are wired and tested today."""
    registry = Registry()

    async def call(pool, gap):
        return await fill_analysis(pool, gap, capability=DERIVED)

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
