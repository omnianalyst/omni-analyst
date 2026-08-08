"""Calibration backfill for crypto producers.

Mirrors ``autonomous/backfill.py`` (the trend/equity backfill) for any crypto
producer registered in ``conviction/producers.py``. Each (entity, cutoff)
replays the producer at that historical timestamp against the coverage knowable
as-of the cutoff, then resolves the resulting prediction against the real
observed price path that followed. The outcomes are real; what they are not is
live, which is why every row carries the backfill marker that ``policy.py``
reads to keep GATE C shut.

Point-in-time is the single most important property: the producer sees only
claims whose ``knowledge_date <= cutoff``. The producers already enforce this
inside their ``_price_window`` / ``_funding_window`` readers (they filter on
``knowledge_date <= as_of``), so passing ``as_of=cutoff`` scopes the read
correctly. This module does NOT restate that filter -- a second copy of the one
rule that must never be got wrong is free to drift from the original without any
test noticing. The headline test plants a claim after the cutoff that would
flip the direction and asserts the producer does not see it.

Resolution uses the real observed price path, never a synthesized one. The
resolver (``ledger.resolve_due_predictions``) reads prices by ``event_date`` in
``[created_at, horizon_ends_at]`` -- the window that actually followed the
cutoff. If no price coverage exists in that window, ``_decide_outcome`` returns
``pending`` and the prediction is reported as ``unresolvable`` here, never
coerced to ``expiry``.

Non-overlapping cutoffs are enforced by default: overlapping horizons reuse the
same price path across predictions, so the outcomes are correlated and the
effective sample is smaller than the count. Cutoffs closer than the horizon
raise rather than silently inflating n.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from uuid import UUID

from omni.autonomous.backfill import _STAMP_BACKFILL
from omni.conviction.ledger import resolve_due_predictions
from omni.conviction.producers import producers_for

logger = logging.getLogger("omni.autonomous.crypto_backfill")


@dataclass(frozen=True)
class BackfillReport:
    generated: int = 0
    resolved: int = 0
    abstained: int = 0
    unresolvable: int = 0


class CutoffOverlapError(ValueError):
    """Cutoffs closer than the horizon reuse the same price path across
    predictions, inflating n with correlated outcomes. Raised rather than
    silently producing a sample smaller than the count suggests."""


def _producer_for(method: str, kind: str):
    for p in producers_for(kind):
        if p.method == method:
            return p
    return None


async def backfill_crypto_predictions(
    pool,
    *,
    method: str,
    entity_ids: list[UUID],
    cutoffs: list[datetime],
    horizon_days: int,
    audience_user_id: UUID | None,
    run_id: str,
) -> BackfillReport:
    """Replay a crypto producer at historical cutoffs for instant calibration.

    For each (entity, cutoff), invokes the producer registered for ``method``
    (via ``producers.producers_for``) with ``as_of`` set to the cutoff, so the
    producer reads only coverage whose ``knowledge_date <= cutoff`` -- it
    cannot peek at the bar it is predicting. The resulting prediction is
    resolved against the real observed price path between the cutoff and the
    horizon. If no price coverage exists in that window, the prediction stays
    pending and is reported as ``unresolvable`` (never scored as expiry).

    Every generated prediction carries ``provenance.assumptions.backfill``
    (run_id/cutoff/as_of), stamped in the same transaction as the insert using
    the exact SQL from ``backfill.py`` -- a row committed without its marker
    reads as live forever and opens GATE C on replayed history.

    Cutoffs must be spaced at least ``horizon_days`` apart; closer spacing
    raises ``CutoffOverlapError`` rather than silently inflating n.
    """
    horizon = timedelta(days=horizon_days)
    sorted_cutoffs = sorted(cutoffs)
    for prev, curr in pairwise(sorted_cutoffs):
        if (curr - prev) < horizon:
            raise CutoffOverlapError(
                f"cutoffs must be spaced at least {horizon_days} days apart; "
                f"{curr.isoformat()} is only {(curr - prev).days} days after "
                f"{prev.isoformat()}"
            )

    earliest_cutoff = sorted_cutoffs[0] if sorted_cutoffs else None
    generated_ids: list[UUID] = []
    abstained = 0

    for entity_id in entity_ids:
        kind = await pool.fetchval(
            "SELECT kind FROM entity WHERE id = $1", entity_id
        )
        if kind is None:
            raise ValueError(f"entity {entity_id} not found")

        producer = _producer_for(method, kind)
        if producer is None:
            raise ValueError(
                f"no producer registered for method={method!r} on kind={kind!r}"
            )

        for cutoff in sorted_cutoffs:
            try:
                async with pool.acquire() as conn, conn.transaction():
                    pid = await producer.produce(
                        conn,
                        entity_id=entity_id,
                        audience_user_id=audience_user_id,
                        as_of=cutoff,
                        horizon_ends_at=cutoff + horizon,
                        created_at=cutoff,
                    )
                    if pid is not None:
                        await conn.execute(
                            _STAMP_BACKFILL,
                            json.dumps({
                                "backfill": {
                                    "run_id": run_id,
                                    "cutoff": earliest_cutoff.isoformat(),
                                    "as_of": cutoff.isoformat(),
                                }
                            }),
                            pid,
                        )
                        generated_ids.append(pid)
                    else:
                        abstained += 1
            except Exception:
                logger.exception(
                    "crypto backfill abstain at entity=%s cutoff=%s",
                    entity_id,
                    cutoff.isoformat(),
                )
                abstained += 1

    await resolve_due_predictions(pool)

    resolved = 0
    unresolvable = 0
    if generated_ids:
        rows = await pool.fetch(
            "SELECT outcome FROM prediction WHERE id = ANY($1::uuid[])",
            generated_ids,
        )
        for row in rows:
            if row["outcome"] == "pending":
                unresolvable += 1
            else:
                resolved += 1

    logger.info(
        "crypto backfill [%s]: %d generated, %d resolved, %d abstained, "
        "%d unresolvable",
        method,
        len(generated_ids),
        resolved,
        abstained,
        unresolvable,
    )

    return BackfillReport(
        generated=len(generated_ids),
        resolved=resolved,
        abstained=abstained,
        unresolvable=unresolvable,
    )
