"""Recording what the system decided to say, and what it decided not to.

The gate in `gate.py` makes the judgement. This persists it — including the
refusals, which is the part that makes the published accuracy believable.

A product that stores only what it surfaced can claim any hit rate it likes,
because the denominator is invisible. Storing every verdict means the question
"how often does it stay quiet, and for which reason" has an answer, and that
answer is what a reader needs before trusting the ones that got through.
"""

from __future__ import annotations

import json
from uuid import UUID

from omni.conviction.gate import Calibration, Verdict

_INSERT_FINDING = """
INSERT INTO finding (claim_id, entity_id, audience_user_id, status, refusal,
                     method, confidence, threshold, calibrated_hit_rate,
                     supporting, disconfirming, prediction_id)
VALUES ($1,$2,$3,$4::finding_status,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12)
RETURNING id
"""

_BUCKETS = """
SELECT method, bucket_low, bucket_high, n, hits
FROM calibration_bucket
WHERE method = $1
  AND (audience_user_id IS NULL OR audience_user_id = $2)
"""


async def load_calibration(
    pool, *, claim_type: str, method: str, audience: UUID | None = None
) -> list[Calibration]:
    """Read the ledger's own record of how this class has performed.

    The gate derives its threshold from these. Nothing here invents a number:
    an unproven class returns an empty list and the gate refuses on that basis.

    Scoped to an audience the same way coverage is (coverage/visibility.py): a
    user's calibration is the shared network's record PLUS their own private
    outcomes, pooled -- 'a user sees the shared network plus their own private
    claims'. `audience=None` collapses to the shared buckets alone, which is the
    only view a shared (network) finding may use. This is the close on the
    calibration licence leak: a byo_only-resolved outcome lives in an
    audience-owned bucket that the shared query (audience_user_id IS NULL) can
    never read, so it cannot move a shared finding's threshold.
    """
    rows = await pool.fetch(_BUCKETS, method, audience)
    return [
        Calibration(
            claim_type=claim_type,
            method=r["method"],
            bucket_low=float(r["bucket_low"]),
            bucket_high=float(r["bucket_high"]),
            n=r["n"],
            hits=r["hits"],
        )
        for r in rows
    ]


async def record(
    pool,
    verdict: Verdict,
    *,
    entity_id: UUID,
    audience_user_id: UUID | None = None,
    prediction_id: UUID | None = None,
) -> UUID:
    """Persist a verdict, surfaced or refused.

    A surfaced verdict without a prediction is rejected by the schema rather
    than by this function: a finding nobody can score later would let the
    published hit rate drift away from what was actually claimed.
    """
    candidate = verdict.candidate
    return await pool.fetchval(
        _INSERT_FINDING,
        candidate.claim_id,
        entity_id,
        audience_user_id,
        "surfaced" if verdict.surfaced else "refused",
        verdict.refusal.value if verdict.refusal else None,
        candidate.method,
        candidate.confidence,
        verdict.threshold,
        verdict.calibrated_hit_rate,
        json.dumps(list(candidate.supporting)),
        json.dumps(list(candidate.disconfirming)),
        prediction_id,
    )


async def briefing(pool, *, audience: UUID | None = None, limit: int = 20) -> list[dict]:
    """What the system chose to say, newest first.

    Scoped to an audience the same way coverage is: a finding derived from a
    user's licensed data belongs to that user. `audience=None` returns the
    shared feed only.
    """
    rows = await pool.fetch(
        """
        SELECT f.id, f.claim_id, f.entity_id, f.method, f.confidence,
               f.threshold, f.calibrated_hit_rate, f.supporting,
               f.disconfirming, f.prediction_id, f.created_at,
               f.deduction_chain,
               e.symbol, e.name
        FROM finding f
        JOIN entity e ON e.id = f.entity_id
        WHERE f.status = 'surfaced'
          AND (f.audience_user_id IS NULL OR f.audience_user_id = $1)
        ORDER BY f.created_at DESC
        LIMIT $2
        """,
        audience,
        min(limit, 100),
    )
    return [dict(r) for r in rows]


async def scorecard(pool) -> list[dict]:
    """Accuracy on what was surfaced, per method.

    Nobody publishes this, which is exactly why it is worth publishing. It also
    only counts resolved predictions — an unresolved one is not a win.
    """
    rows = await pool.fetch(
        "SELECT method, surfaced, resolved, hits FROM finding_hit_rate "
        "ORDER BY surfaced DESC"
    )
    out = []
    for r in rows:
        d = dict(r)
        # None, not zero, below the sample floor. A hit rate computed from two
        # resolved predictions is noise wearing a percentage sign.
        d["hit_rate"] = (
            d["hits"] / d["resolved"] if d["resolved"] and d["resolved"] >= 10 else None
        )
        out.append(d)
    return out


async def refusal_counts(pool) -> dict[str, int]:
    """Why the system stayed quiet. The denominator behind the scorecard."""
    rows = await pool.fetch(
        "SELECT refusal, count(*) n FROM finding WHERE status = 'refused' "
        "GROUP BY refusal ORDER BY n DESC"
    )
    return {r["refusal"]: r["n"] for r in rows}
