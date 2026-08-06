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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from omni.capability.registry import Registry
from omni.coverage.writer import (
    MissingCredentialOwner,
    ProhibitedSource,
    write_claims,
)
from omni.entities.resolve import _PROVIDER_IDENTIFIER, key_for
from omni.ingest.protocol import ClaimDraft, Unavailable

DEFAULT_LEASE_SECONDS = 300

#: A gap whose source keeps failing backs off exponentially rather than being
#: retried as fast as the loop turns. Without this, one unreachable provider
#: burns an API budget in seconds.
RETRY_BASE_SECONDS = 30
MAX_ATTEMPTS = 6

#: How long to wait before re-querying a source that answered correctly but had
#: nothing newer than what the store already holds ("all N already held"). This
#: is not a failure -- the source worked -- so it must not burn a retry attempt
#: or count toward MAX_ATTEMPTS. A daily-bar source (Polygon) publishes once per
#: close, so re-querying every sweep wastes the entire free-tier rate budget on
#: data that cannot have changed. Six hours catches a newly published bar within
#: that window while freeing ~80% of the rate limit for productive fills.
NO_NEW_DATA_COOLDOWN_SECONDS = 6 * 3600

# A capability takes the gap's target and returns drafts. Anything that cannot
# answer raises Unavailable with a reason a human can act on.
Capability = Callable[..., Awaitable[Sequence[ClaimDraft]]]




_CLAIM_GAP = """
UPDATE gap SET lease_owner = $1, lease_expires_at = now() + ($2 || ' seconds')::interval
WHERE id = (
    SELECT id FROM gap
    WHERE resolved_at IS NULL
      AND (lease_expires_at IS NULL OR lease_expires_at < now())
      AND (next_attempt_at IS NULL OR next_attempt_at <= now())
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

# Per-entity providers (sec_edgar/polygon/coingecko) fetch by an identifier that
# lives on the entity row, not by the gap's series key. The columns mirror what
# resolve.key_for reads, so the row can be handed to it unchanged.
_LOAD_ENTITY = """
SELECT id, kind, symbol, name, identifiers
FROM entity
WHERE id = $1
"""

_RESOLVE = """
UPDATE gap SET resolved_at = now(), lease_owner = NULL, lease_expires_at = NULL
WHERE id = $1
"""

# Cooldown for "source answered but had nothing new". Unlike _RELEASE this is
# not a failure: attempts is not incremented, the gap is not resolved, and the
# retry is scheduled at a fixed cadence rather than an exponential backoff. The
# gap stays OPEN so persist_gaps' ON CONFLICT DO UPDATE refreshes its score
# while preserving next_attempt_at -- without this, resolving the gap let
# detect_gaps open a fresh one each sweep with next_attempt_at NULL, re-querying
# the source every cycle for data that could not have changed.
_COOLDOWN = """
UPDATE gap SET lease_owner = NULL, lease_expires_at = NULL,
               next_attempt_at = now() + ($2 || ' seconds')::interval
WHERE id = $1
"""

# Release with backoff. attempts is incremented so repeated failures wait
# longer, and a gap that has exhausted MAX_ATTEMPTS is resolved rather than
# retried forever -- an unreachable source is a fact about the world, and the
# fill_attempt rows record why.
_RELEASE = """
UPDATE gap SET
    lease_owner = NULL,
    lease_expires_at = NULL,
    attempts = attempts + 1,
    next_attempt_at = CASE
        WHEN attempts + 1 >= $2 THEN NULL
        ELSE now() + ($3 * power(2, attempts)) * interval '1 second'
    END,
    resolved_at = CASE WHEN attempts + 1 >= $2 THEN now() ELSE NULL END
WHERE id = $1
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
    registry: Registry,
    credential_owner: UUID | None = None,
    licensed: Sequence[str] = (),
) -> FillResult:
    """Attempt one gap. Always records an attempt; never fabricates."""
    gap_id = gap["id"]
    candidates = registry.producing(gap["claim_type"])

    if not candidates:
        reason = f"no capability registered for claim type {gap['claim_type']}"
        await _record(pool, gap_id, None, "unfillable", None, reason)
        await pool.execute(_RELEASE, gap_id, MAX_ATTEMPTS, RETRY_BASE_SECONDS)
        return FillResult(gap_id, "unfillable", None, [], reason)

    failures: list[str] = []
    # A per-entity provider (one whose provider_key is in _PROVIDER_IDENTIFIER)
    # is fetched by an identifier on the entity row, never by gap["key"]: the
    # latter is NULL for a fundamental/price gap, and handing it through is how a
    # CIK of "None" reached EDGAR. Load the row once only when such a candidate
    # exists, so a FRED/rss gap pays no extra round-trip.
    needs_entity = any(c.provider_key in _PROVIDER_IDENTIFIER for c in candidates)
    entity = await _load_entity(pool, gap["entity_id"]) if needs_entity else None

    for registration in candidates:
        if registration.is_derived:
            # A derived capability produces a claim from coverage already in the
            # store, not from a fetch: its call is (pool, gap) and returns a
            # self-contained FillResult -- fill_analysis records the attempt,
            # writes the claim with its input edges, resolves/releases the gap
            # and resolves the licence from the materialized inputs itself. So
            # this branch returns directly; it must not fall through to the
            # adapter's write_claims path (which would double-record and expects
            # drafts, not a FillResult). The earlier code handed every candidate
            # the single-key adapter arg, which is a TypeError for a derived
            # call -- so derived gaps never closed under the scheduler.
            try:
                return await registration.call(pool, gap)
            except Unavailable as exc:
                failures.append(f"{registration.name}: {exc}")
                continue
            except (ProhibitedSource, MissingCredentialOwner) as exc:
                failures.append(f"{registration.name}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                reason = f"{registration.name} raised {type(exc).__name__}: {exc}"
                await _record(pool, gap_id, registration.name, "error", None, reason)
                await pool.execute(_RELEASE, gap_id, MAX_ATTEMPTS, RETRY_BASE_SECONDS)
                return FillResult(gap_id, "error", registration.name, [], reason)

        try:
            if registration.provider_key in _PROVIDER_IDENTIFIER:
                if entity is None:
                    raise Unavailable(
                        f"no entity row for {gap['entity_id']}; cannot resolve "
                        f"{registration.provider_key!r} identifier"
                    )
                arg = key_for(entity, registration.provider_key)
            else:
                arg = gap["key"]
                if arg is None:
                    # A NULL series key is not something to fetch: passing it
                    # through is how a CIK of "None" reached EDGAR, and for a
                    # series/rss provider it would invent a call to series None
                    # or feed URL None. Decline rather than hand the adapter NULL.
                    raise Unavailable(
                        f"gap has no key and {registration.provider_key!r} "
                        f"is fetched by it"
                    )
            drafts = await registration.call(arg)
        except Unavailable as exc:
            failures.append(f"{registration.name}: {exc}")
            continue
        except (ProhibitedSource, MissingCredentialOwner) as exc:
            failures.append(f"{registration.name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            reason = f"{registration.name} raised {type(exc).__name__}: {exc}"
            await _record(pool, gap_id, registration.name, "error", None, reason)
            await pool.execute(_RELEASE, gap_id, MAX_ATTEMPTS, RETRY_BASE_SECONDS)
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
                source=(registration.source or registration.provider_key),
                provider_key=registration.provider_key,
                credential_owner=credential_owner or gap["audience_user_id"],
                licensed=licensed,
            )
        except (ProhibitedSource, MissingCredentialOwner) as exc:
            failures.append(f"{registration.name}: {exc}")
            continue

        if not claim_ids:
            # The source answered, but every draft was already held -- a
            # correct source re-queried. Not 'filled' (nothing written) and not
            # a failure (the source worked). Cooldown, not resolve: resolving
            # let detect_gaps reopen a fresh gap next sweep with no backoff,
            # re-querying the source every cycle for data that could not have
            # changed (this was burning ~the entire free-tier rate limit).
            reason = (
                f"{registration.name} returned no new observations "
                f"(all {len(drafts)} already held)"
            )
            await _record(pool, gap_id, registration.name, "unfillable", None, reason)
            await pool.execute(
                _COOLDOWN, gap_id, str(NO_NEW_DATA_COOLDOWN_SECONDS)
            )
            return FillResult(gap_id, "unfillable", registration.name, [], reason)

        first = claim_ids[0]
        await _record(pool, gap_id, registration.name, "filled", first, None)
        await pool.execute(_RESOLVE, gap_id)
        return FillResult(gap_id, "filled", registration.name, claim_ids, None)

    reason = "; ".join(failures)
    await _record(pool, gap_id, candidates[0].name, "unfillable", None, reason)
    await pool.execute(_RELEASE, gap_id, MAX_ATTEMPTS, RETRY_BASE_SECONDS)
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


async def _load_entity(pool, entity_id):
    row = await pool.fetchrow(_LOAD_ENTITY, entity_id)
    return dict(row) if row else None


async def run_once(
    pool,
    *,
    registry: Registry,
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
    registry: Registry,
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
