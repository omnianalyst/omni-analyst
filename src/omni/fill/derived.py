"""The derived-claim fill path.

``pipeline.fill_gap`` only writes ingested claims: it hands a key to a
capability and persists the drafts through ``write_claims``. A derived claim
cannot take that path, because it must declare its inputs in the same
transaction or migration 002's deferred trigger rejects it at commit, and
``write_claims`` cannot do that -- and must not be changed, because it is the
single place the ingestion licence rule lives.

This is the counterpart for derived claims. ``fill_analysis`` generalizes the
path D2 traced through ``perception.divergence``: a capability declares its
inputs as ``ArgumentSpec``s (D4), ``fill_analysis`` materializes each through
``omni.capability.arguments.materialize``, and abstains if any is short.
For structures the declaration cannot describe (multi-entity panels,
caller-built inputs) a capability may instead provide an injected ``gather``
-- the escape hatch D2 leaves open -- with the same record/resolve/release
semantics and the same atomic write.

Both branches resolve the result's licence from the inputs themselves via
``resolve_derived_licence`` -- never from a caller's guess -- and persist the
claim and its ``claim_input`` edges through ``write_derived`` in one
transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID

from omni.capability.arguments import (
    Abstention,
    ArgumentSpec,
    Materialized,
    materialize,
)
from omni.coverage.visibility import visible_claims_cte
from omni.fill.pipeline import (
    _RELEASE,
    _RESOLVE,
    MAX_ATTEMPTS,
    RETRY_BASE_SECONDS,
    FillResult,
    _record,
)
from omni.perception.divergence import resolve_derived_licence, write_derived


@dataclass(frozen=True)
class DerivedCapability:
    """What ``fill_analysis`` needs from a capability to fill a derived gap.

    **Declared path** (``arguments`` is not None): ``fill_analysis``
    materializes each ``ArgumentSpec`` through ``visible_claims``
    (audience-scoped), abstains if any is short, then calls
    ``compute(pool, gap, **materialized)`` where each kwarg is the spec's
    ``Materialized``. ``compute`` returns a ``ClaimDraft`` or ``None``
    (honest abstention).

    **Injected escape hatch** (``arguments`` is None, ``gather`` is not
    None): for structures the declaration cannot describe. ``fill_analysis``
    calls ``gather(pool, gap)`` then ``compute(*gathered)``. ``compute``
    returns ``(ClaimDraft, list[UUID]) | None``.

    The ``claim_input`` edges are written from the materialized claim_ids
    (declared) or the compute-reported ids (injected), never from a guess.
    """

    name: str
    compute: Callable
    arguments: tuple[ArgumentSpec, ...] | None = None
    gather: Callable | None = None


_LICENCE_BY_IDS = f"""
SELECT c.redistributable, c.audience_user_id
FROM ({visible_claims_cte()}) c
WHERE c.id = ANY($2::uuid[])
"""


async def _licence_inputs(pool, audience, claim_ids: list[UUID]) -> list:
    """Re-read the materialized input claims to recover their licence fields.

    ``materialize`` (D4) returns values and claim_ids but not the
    ``redistributable`` / ``audience_user_id`` that ``resolve_derived_licence``
    needs. Those are re-read through ``visible_claims`` (audience-scoped) --
    never the bare table -- so a private claim of another user that
    ``materialize`` correctly excluded is not present here either.

    ``resolve_derived_licence`` accesses ``.redistributable`` and
    ``.audience_user_id``; ``SimpleNamespace`` carries exactly those, without
    fabricating a ``value`` that ``DivergenceInput`` would require.
    """
    if not claim_ids:
        return []
    rows = await pool.fetch(_LICENCE_BY_IDS, audience, list(claim_ids))
    return [
        SimpleNamespace(
            redistributable=r["redistributable"],
            audience_user_id=r["audience_user_id"],
        )
        for r in rows
    ]


async def fill_analysis(
    pool, gap, *, capability: DerivedCapability
) -> FillResult:
    """Fill a derived-claim gap. Always records an attempt; never fabricates.

    Routes to the declared path (``capability.arguments``) or the injected
    escape hatch (``capability.gather``). See ``DerivedCapability`` for the
    contracts. Both branches reuse ``_record`` / ``_RELEASE`` from the
    pipeline so the retry backoff and the unfillable-with-a-reason contract
    are identical to the ingested path.
    """
    if capability.arguments is not None:
        return await _fill_declared(pool, gap, capability)
    return await _fill_injected(pool, gap, capability)


async def _fill_declared(pool, gap, capability: DerivedCapability) -> FillResult:
    """Declared path: materialize ArgumentSpecs, then compute."""
    gap_id = gap["id"]
    cap_name = capability.name
    entity_id = gap["entity_id"]
    audience = gap["audience_user_id"]

    materialized: dict[str, Materialized] = {}
    abstentions: list[Abstention] = []
    for spec in capability.arguments:
        result = await materialize(
            spec, pool, entity_id=entity_id, audience=audience
        )
        if isinstance(result, Abstention):
            abstentions.append(result)
        else:
            materialized[spec.name] = result

    if abstentions:
        reason = (
            f"insufficient inputs to derive {gap['claim_type']}: "
            + "; ".join(a.reason for a in abstentions)
        )
        await _record(pool, gap_id, cap_name, "unfillable", None, reason)
        await pool.execute(_RELEASE, gap_id, MAX_ATTEMPTS, RETRY_BASE_SECONDS)
        return FillResult(gap_id, "unfillable", cap_name, [], reason)

    computed = await capability.compute(pool, gap, **materialized)

    if computed is None:
        count = sum(len(m.claim_ids) for m in materialized.values())
        reason = (
            f"insufficient inputs to derive {gap['claim_type']}: "
            f"{count} input claim(s)"
        )
        await _record(pool, gap_id, cap_name, "unfillable", None, reason)
        await pool.execute(_RELEASE, gap_id, MAX_ATTEMPTS, RETRY_BASE_SECONDS)
        return FillResult(gap_id, "unfillable", cap_name, [], reason)

    draft = computed

    input_claim_ids = list(
        dict.fromkeys(
            cid for m in materialized.values() for cid in m.claim_ids
        )
    )

    licence_inputs = await _licence_inputs(pool, audience, input_claim_ids)
    redistributable, audience_resolved = resolve_derived_licence(licence_inputs)

    claim_id = await write_derived(
        pool,
        draft,
        entity_id=entity_id,
        input_claim_ids=input_claim_ids,
        audience_user_id=audience_resolved,
        redistributable=redistributable,
    )

    await _record(pool, gap_id, cap_name, "filled", claim_id, None)
    await pool.execute(_RESOLVE, gap_id)
    return FillResult(gap_id, "filled", cap_name, [claim_id], None)


async def _fill_injected(pool, gap, capability: DerivedCapability) -> FillResult:
    """Escape hatch: the capability's own gather + compute.

    Unchanged from ``fill_derived`` -- the path for multi-entity panels and
    caller-built structures the declaration cannot describe. ``compute``
    returns ``(draft, input_claim_ids) | None`` so it can declare a subset of
    the gathered claims as the real inputs.
    """
    gap_id = gap["id"]
    cap_name = capability.name

    gathered = await capability.gather(pool, gap)
    computed = capability.compute(*gathered)

    if computed is None:
        all_inputs = [c for group in gathered for c in group]
        reason = (
            f"insufficient inputs to derive {gap['claim_type']}: "
            f"{len(all_inputs)} input claim(s)"
        )
        await _record(pool, gap_id, cap_name, "unfillable", None, reason)
        await pool.execute(_RELEASE, gap_id, MAX_ATTEMPTS, RETRY_BASE_SECONDS)
        return FillResult(gap_id, "unfillable", cap_name, [], reason)

    draft, input_claim_ids = computed
    all_inputs = [c for group in gathered for c in group]
    used_ids = set(input_claim_ids)
    licence_inputs = [c for c in all_inputs if c.id in used_ids]

    redistributable, audience = resolve_derived_licence(licence_inputs)

    claim_id = await write_derived(
        pool,
        draft,
        entity_id=gap["entity_id"],
        input_claim_ids=input_claim_ids,
        audience_user_id=audience,
        redistributable=redistributable,
    )

    await _record(pool, gap_id, cap_name, "filled", claim_id, None)
    await pool.execute(_RESOLVE, gap_id)
    return FillResult(gap_id, "filled", cap_name, [claim_id], None)
