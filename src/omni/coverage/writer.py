"""Turning adapter drafts into claims.

This is the single place a claim's licence class and audience are decided.
Adapters deliberately cannot express either — a `ClaimDraft` has no audience
and no redistribution field — so there is exactly one code path where the
redistribution rule can be got right or wrong, rather than one per adapter.

The rule, from the credential catalog:

    allowed     -> shared coverage, no owner
    byo_only    -> private to the credential owner, who must be known
    prohibited  -> never written at all

An unknown provider raises. A provider nobody has classified must not reach
the store, because the class it would default to is a guess about somebody's
licence terms.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from uuid import UUID

from omni.credentials.catalog import redistribution_for
from omni.ingest.protocol import ClaimDraft

_INSERT = """
INSERT INTO claim (entity_id, claim_type, key, value, unit, evidence, source,
                   event_date, knowledge_date, confidence, credential_owner,
                   redistributable, audience_user_id, derivation)
VALUES ($1,$2::claim_type,$3,$4::jsonb,$5,$6::jsonb,$7,$8,$9,$10,$11,
        $12::redistribution,$13,$14::claim_derivation)
ON CONFLICT DO NOTHING
RETURNING id
"""


class ProhibitedSource(Exception):
    """The provider's terms forbid using it in a commercial product at all."""


class MissingCredentialOwner(Exception):
    """A byo_only source was fetched without recording whose key was used."""


def resolve_audience(
    provider_key: str,
    *,
    credential_owner: UUID | None,
    licensed: Sequence[str] = (),
) -> tuple[str, UUID | None]:
    """Decide a claim's licence class and audience.

    Returns `(redistributable, audience_user_id)` matching the CHECK in
    migration 001: an allowed claim has no audience, a byo_only claim must
    have one.
    """
    klass = redistribution_for(provider_key, licensed=licensed)

    if klass == "prohibited":
        raise ProhibitedSource(
            f"{provider_key} may not be used in a commercial product"
        )
    if klass == "allowed":
        # Even if a key was used, the data is redistributable, so it belongs
        # to the network rather than to the user who happened to fetch it.
        return "allowed", None
    if credential_owner is None:
        raise MissingCredentialOwner(
            f"{provider_key} is byo_only; a claim from it cannot be written "
            "without the user whose credential fetched it"
        )
    return "byo_only", credential_owner


async def write_claims(
    pool,
    drafts: Iterable[ClaimDraft],
    *,
    entity_id: UUID,
    source: str,
    provider_key: str,
    credential_owner: UUID | None = None,
    licensed: Sequence[str] = (),
) -> list[UUID]:
    """Persist drafts as claims. Returns the ids actually inserted.

    Re-ingesting an observation already held is a no-op rather than an error:
    the unique index on (entity, type, key, source, event_date,
    knowledge_date) is the idempotency guarantee, so a re-run costs nothing
    and duplicates nothing.
    """
    redistributable, audience = resolve_audience(
        provider_key, credential_owner=credential_owner, licensed=licensed
    )
    owner_label = None if audience is None else "user"

    written: list[UUID] = []
    async with pool.acquire() as conn, conn.transaction():
        for draft in drafts:
            claim_id = await conn.fetchval(
                _INSERT,
                entity_id,
                draft.claim_type,
                draft.key,
                json.dumps(draft.value),
                draft.unit,
                json.dumps(draft.evidence) if draft.evidence else None,
                source,
                draft.event_date,
                draft.knowledge_date,
                draft.confidence,
                owner_label,
                redistributable,
                audience,
                "ingested",
            )
            if claim_id is not None:
                written.append(claim_id)
    return written
