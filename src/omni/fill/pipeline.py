"""The fill pipeline — where a gap becomes coverage, or an honest refusal.

This is the component the other three exist to feed. It leases a ranked gap,
routes it to a capability, and writes back either claims or a `fill_attempt`
recording why the gap could not be closed.

The refusal path is the important one. A gap-filler that always produces
something is how fabricated coverage enters the store, so `Unavailable` from a
capability is a normal outcome that gets recorded with its reason, not an error
to be swallowed or papered over with a default.

Leases, not locks: a worker that dies mid-fill loses its lease and another
worker picks the gap up, the same mechanism the Neutron job queue uses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence
from uuid import UUID

from omni.coverage.writer import (
    MissingCredentialOwner,
    ProhibitedSource,
    write_claims,
)
from omni.ingest.protocol import ClaimDraft, Unavailable

DEFAULT_LEASE_SECONDS = 300

# A capability takes the gap's target and returns drafts. Anything that cannot
# answer raises Unavailable with a reason a human can act on.
Capability = Callable[..., Awaitable[Sequence[ClaimDraft]]]


@dataclass(frozen=True)
class Registration:
    """A capability bound to the source it speaks for."""

    name: str
    source: str
    provider_key: str
    call: Capability


class CapabilityRegistry:
    """What the scheduler is allowed to call to close a gap.

    Keyed by claim type: a gap in `macro_series_point` is answerable by
    whatever is registered for that type. Several capabilities may serve one
    type — corroboration from a second source is what clears an `unverified`
    gap — so registration appends rather than replaces.
    """

    def __init__(self) -> None:
        self._by_claim_type: dict[str, list[Registration]] = {}

    def register(
        self,
        claim_type: str,
        *,
        name: str,
        source: str,
        provider_key: str,
        call: Capability,
    ) -> None:
        self._by_claim_type.setdefault(claim_type, []).append(
            Registration(name=name, source=source, provider_key=provider_key, call=call)
        )

    def for_claim_type(self, claim_type: str) -> list[Registration]:
        return list(self._by_claim_type.get(claim_type, []))


_CLAIM_GAP = """
UPDATE gap SET lease_owner = $1, lease_expires_at = now() + ($2 || ' seconds')::interval
WHERE id = (
    SELECT id FROM gap
    WHERE resolved_at IS NULL
      AND (lease_expires_at IS NULL OR lease_expires_at < now())
    ORDER BY score DESC, detected_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, entity_id, claim_type, key, gap_class, audience_user_id, score
"""

_RECORD_ATTEMPT = """
INSERT INTO fill_attempt (gap_id, capability, outcome, claim_id, reason, finished_at)
VALUES ($1, $2, $3::fill_outcome, $4, $5, now())
RETURNING id
"""

_RESOLVE = """
UPDATE gap SET resolved_at = now(), lease_owner = NULL, lease_expires_at = NULL
WHERE id = $1
"""

_RELEASE = """
UPDATE gap SET lease_owner = NULL, lease_expires_at = NULL WHERE id = $1
"""


@dataclass(frozen=True)
class FillResult:
    gap_id: UUID
    outcome: str
    capability: str | None
    claim_ids: list[UUID]
    reason: str | None


async def claim_next_gap(
    pool, *, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> dict | None:
    """Lease the highest-scoring open gap, or None if there is nothing to do."""
    row = await pool.fetchrow(_CLAIM_GAP, worker_id, str(lease_seconds))
    return dict(row) if row else None


async def fill_gap(
    pool,
    gap: dict,
    *,
    registry: CapabilityRegistry,
    credential_owner: UUID | None = None,
    licensed: Sequence[str] = (),
) -> FillResult:
    """Attempt one gap. Always records an attempt; never fabricates."""
    gap_id = gap["id"]
    candidates = registry.for_claim_type(gap["claim_type"])

    if not candidates:
        reason = f"no capability registered for claim type {gap['claim_type']}"
        await _record(pool, gap_id, None, "unfillable", None, reason)
        await pool.execute(_RELEASE, gap_id)
        return FillResult(gap_id, "unfillable", None, [], reason)

    failures: list[str] = []
    for registration in candidates:
        try:
            drafts = await registration.call(gap["key"])
        except Unavailable as exc:
            failures.append(f"{registration.name}: {exc}")
            continue
        except (ProhibitedSource, MissingCredentialOwner) as exc:
            failures.append(f"{registration.name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            reason = f"{registration.name} raised {type(exc).__name__}: {exc}"
            await _record(pool, gap_id, registration.name, "error", None, reason)
            await pool.execute(_RELEASE, gap_id)
            return FillResult(gap_id, "error", registration.name, [], reason)

        if not drafts:
            # An empty result is a real answer: the source has nothing for this
            # target. Recording it stops the gap being re-attempted forever.
            reason = f"{registration.name} returned no observations"
            await _record(pool, gap_id, registration.name, "unfillable", None, reason)
            await pool.execute(_RESOLVE, gap_id)
            return FillResult(gap_id, "unfillable", registration.name, [], reason)

        try:
            claim_ids = await write_claims(
                pool,
                drafts,
                entity_id=gap["entity_id"],
                source=registration.source,
                provider_key=registration.provider_key,
                credential_owner=credential_owner or gap["audience_user_id"],
                licensed=licensed,
            )
        except (ProhibitedSource, MissingCredentialOwner) as exc:
            failures.append(f"{registration.name}: {exc}")
            continue

        first = claim_ids[0] if claim_ids else None
        await _record(pool, gap_id, registration.name, "filled", first, None)
        await pool.execute(_RESOLVE, gap_id)
        return FillResult(gap_id, "filled", registration.name, claim_ids, None)

    reason = "; ".join(failures)
    await _record(pool, gap_id, candidates[0].name, "unfillable", None, reason)
    await pool.execute(_RELEASE, gap_id)
    return FillResult(gap_id, "unfillable", candidates[0].name, [], reason)


async def _record(
    pool,
    gap_id: UUID,
    capability: str | None,
    outcome: str,
    claim_id: UUID | None,
    reason: str | None,
) -> None:
    await pool.execute(
        _RECORD_ATTEMPT, gap_id, capability or "none", outcome, claim_id, reason
    )


async def run_once(
    pool,
    *,
    registry: CapabilityRegistry,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    licensed: Sequence[str] = (),
) -> FillResult | None:
    """Lease and attempt a single gap. Returns None when the queue is empty."""
    gap = await claim_next_gap(pool, worker_id=worker_id, lease_seconds=lease_seconds)
    if gap is None:
        return None
    return await fill_gap(pool, gap, registry=registry, licensed=licensed)


async def drain(
    pool,
    *,
    registry: CapabilityRegistry,
    worker_id: str,
    max_gaps: int = 100,
    licensed: Sequence[str] = (),
) -> list[FillResult]:
    """Work the queue until it is empty or `max_gaps` have been attempted.

    Bounded on purpose: an unbounded drain against a gap engine that can reopen
    gaps is a way to spend an API budget in one call.
    """
    results: list[FillResult] = []
    deadline = time.monotonic() + 300
    while len(results) < max_gaps and time.monotonic() < deadline:
        result = await run_once(
            pool, registry=registry, worker_id=worker_id, licensed=licensed
        )
        if result is None:
            break
        results.append(result)
    return results
