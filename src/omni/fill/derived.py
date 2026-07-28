"""The derived-claim fill path.

``pipeline.fill_gap`` only writes ingested claims: it hands a key to a
capability and persists the drafts through ``write_claims``. A derived claim
cannot take that path, because it must declare its inputs in the same
transaction or migration 002's deferred trigger rejects it at commit, and
``write_claims`` cannot do that -- and must not be changed, because it is the
single place the ingestion licence rule lives.

This is the counterpart for derived claims. It reuses the pipeline's
record/resolve/release semantics so the retry backoff and the
unfillable-with-a-reason contract behave identically, but persists the claim
and its ``claim_input`` edges through ``write_divergence`` in one transaction,
and resolves the result's licence from the inputs themselves via
``resolve_derived_licence`` -- never from a caller's guess.

``compute`` and ``gather`` are injected. ``gather`` reads the gap's input
claims through ``visible_claims`` (never the claim table directly), scoped to
the gap's ``audience_user_id``. ``compute`` turns those streams into a
``(ClaimDraft, input_claim_ids)`` pair, or ``None``. ``None`` is an honest
abstention -- not enough aligned history yet -- recorded as ``unfillable``
with a reason and released with backoff, so a later fill sees whatever new
coverage has arrived.
"""

from __future__ import annotations

from omni.fill.pipeline import (
    _RELEASE,
    _RESOLVE,
    MAX_ATTEMPTS,
    RETRY_BASE_SECONDS,
    FillResult,
    _record,
)
from omni.perception.divergence import resolve_derived_licence, write_divergence

CAPABILITY = "perception.divergence"


async def fill_derived(pool, gap, *, compute, gather) -> FillResult:
    """Fill a derived-claim gap. Always records an attempt; never fabricates.

    ``gather`` returns the gap's input claims grouped by stream (for
    divergence, a ``(perception, facts)`` pair of ``DivergenceInput`` lists).
    ``compute`` turns those streams into a ``(ClaimDraft, input_claim_ids)``
    pair, or ``None`` to abstain.

    ``None`` is ``unfillable`` with a reason and released with the pipeline's
    backoff -- not resolved -- because the inputs that would make the claim
    computable may arrive later. A successful compute resolves the licence
    from the inputs via ``resolve_derived_licence`` and writes claim and edges
    atomically through ``write_divergence``.
    """
    gap_id = gap["id"]

    gathered = await gather(pool, gap)
    computed = compute(*gathered)

    if computed is None:
        all_inputs = [c for group in gathered for c in group]
        reason = (
            f"insufficient inputs to derive {gap['claim_type']}: "
            f"{len(all_inputs)} input claim(s)"
        )
        await _record(pool, gap_id, CAPABILITY, "unfillable", None, reason)
        await pool.execute(_RELEASE, gap_id, MAX_ATTEMPTS, RETRY_BASE_SECONDS)
        return FillResult(gap_id, "unfillable", CAPABILITY, [], reason)

    draft, input_claim_ids = computed
    all_inputs = [c for group in gathered for c in group]
    used_ids = set(input_claim_ids)
    licence_inputs = [c for c in all_inputs if c.id in used_ids]

    redistributable, audience = resolve_derived_licence(licence_inputs)

    claim_id = await write_divergence(
        pool,
        draft,
        entity_id=gap["entity_id"],
        input_claim_ids=input_claim_ids,
        audience_user_id=audience,
        redistributable=redistributable,
    )

    await _record(pool, gap_id, CAPABILITY, "filled", claim_id, None)
    await pool.execute(_RESOLVE, gap_id)
    return FillResult(gap_id, "filled", CAPABILITY, [claim_id], None)
