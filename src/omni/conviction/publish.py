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
                     supporting, disconfirming, prediction_id, evidence_searched)
VALUES ($1,$2,$3,$4::finding_status,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12,$13)
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
        # Recorded, not inferred. An empty disconfirming list means "looked and
        # found nothing" only when this is true; on rows written before the
        # search existed it means "never looked", and nothing else in the row
        # can tell the two apart.
        candidate.searched_for_disconfirming,
    )


async def briefing(pool, *, audience: UUID | None = None, limit: int = 20) -> list[dict]:
    """What the system currently says, newest first.

    Scoped to an audience the same way coverage is: a finding derived from a
    user's licensed data belongs to that user. `audience=None` returns the
    shared feed only.

    One row per (entity, method, audience): the newest. The finding table is an
    append-only ledger -- every pass writes a fresh row so the refusal
    denominator and the historical record stay intact -- but the *feed* is a
    statement of the current view, and a ledger read raw is not that. Two passes
    an hour apart legitimately produce opposite directions on the same name as
    price crosses the moving average; showing both makes the product contradict
    itself in a single screen. The superseded rows are still on the ledger and
    still score; they are simply not what the system says now.

    A call whose prediction has resolved is likewise not what the system says
    now -- it is what the system said, and it belongs to the scorecard. Nothing
    would otherwise retire it: a resolved finding can only be displaced by a
    newer *surfaced* row for the same key, so if the next pass refuses (the
    confidence fell, the evidence went quiet) the resolved call would stand as
    current indefinitely. Deduping first and filtering after would be wrong for
    the same reason -- it would let a resolved row hide a live one behind it --
    so the filter is inside the subquery, where the newest row per key is chosen
    from the still-open ones.
    """
    rows = await pool.fetch(
        """
        SELECT * FROM (
            SELECT DISTINCT ON (f.entity_id, f.method, f.audience_user_id)
                   f.id, f.claim_id, f.entity_id, f.method, f.confidence,
                   f.threshold, f.calibrated_hit_rate, f.supporting,
                   f.disconfirming, f.prediction_id, f.created_at,
                   enrichment.deduction_chain, f.evidence_searched,
                   e.symbol, e.name,
                   p.direction, p.entry_price, p.upper_barrier, p.lower_barrier
            FROM finding f
            JOIN entity e ON e.id = f.entity_id
            LEFT JOIN prediction p ON p.id = f.prediction_id
            LEFT JOIN LATERAL (
                SELECT r.deduction_chain
                FROM finding_enrichment_revision r
                WHERE r.finding_id = f.id
                ORDER BY r.evidence_as_of DESC, r.created_at DESC, r.id DESC
                LIMIT 1
            ) enrichment ON true
            WHERE f.status = 'surfaced'
              AND (f.audience_user_id IS NULL OR f.audience_user_id = $1)
              AND (p.id IS NULL OR p.outcome = 'pending')
            ORDER BY f.entity_id, f.method, f.audience_user_id, f.created_at DESC
        ) current
        ORDER BY created_at DESC
        LIMIT $2
        """,
        audience,
        min(limit, 100),
    )
    return [dict(r) for r in rows]


async def scorecard(pool, *, audience: UUID | None = None) -> list[dict]:
    """Accuracy on what was surfaced, per method.

    Nobody publishes this, which is exactly why it is worth publishing. It also
    only counts resolved predictions — an unresolved one is not a win.

    Scoped the same way coverage is: an operator's published accuracy is over
    the shared network's surfaced findings PLUS their own private ones, never
    another operator's -- a byo-derived hit rate is a deterministic function of
    that operator's licensed demand, and serving it to a second operator would
    make this deployment the redistributor. The view carries audience_user_id
    through its GROUP BY (migration 028) so this query can re-aggregate the
    shared row plus the caller's own.
    """
    rows = await pool.fetch(
        """
        SELECT method,
               COALESCE(SUM(surfaced), 0)::bigint AS surfaced,
               COALESCE(SUM(resolved), 0)::bigint AS resolved,
               COALESCE(SUM(hits), 0)::bigint AS hits
        FROM finding_hit_rate
        WHERE audience_user_id IS NULL OR audience_user_id = $1
        GROUP BY method
        ORDER BY surfaced DESC
        """,
        audience,
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


async def refusal_counts(pool, *, audience: UUID | None = None) -> dict[str, int]:
    """Why the system stayed quiet. The denominator behind the scorecard.

    Scoped the same way as the scorecard: an operator sees the shared refusal
    mix plus their own, never another operator's.
    """
    rows = await pool.fetch(
        "SELECT refusal, count(*) n FROM finding WHERE status = 'refused' "
        "AND (audience_user_id IS NULL OR audience_user_id = $1) "
        "GROUP BY refusal ORDER BY n DESC",
        audience,
    )
    return {r["refusal"]: r["n"] for r in rows}
