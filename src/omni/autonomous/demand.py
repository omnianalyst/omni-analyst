"""Phase D: the autonomous demand loop (Loop 8).

The sector scanner (Phase C) identified which sectors are moving and how the
macro regime aligns with them. This loop turns that intelligence into DEMAND --
it tells the existing sweep/fill/predict chain which constituents to cover.

The loop reads the latest sector_scores, ranks sectors by relative strength and
macro alignment, and creates autonomous demand for price_snapshot on the
constituents of the top sectors. The demand channel is 'autonomous' (weight 0.5,
below user demand's 1.0) so the system's curiosity never outranks a user's
explicit attention in the fill queue.

Demand is idempotent: if an active autonomous demand already exists for an
(entity, claim_type), a second one is not created -- amplifying weight without
adding signal is the deduplication the ledger forbids on the write path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from omni.demand.ledger import autonomous_attention

logger = logging.getLogger("omni.autonomous.demand")

# Company comparison is only honest when every GICS sector can enter the
# candidate set. User demand still outranks this autonomous weight, while the
# fill queue progressively closes coverage across all eleven sectors.
_TOP_SECTORS = 11
_AUTONOMOUS_WEIGHT = 0.5

_EXISTING_AUTONOMOUS = """
SELECT 1 FROM demand
WHERE entity_id = $1 AND claim_type = 'price_snapshot'
  AND channel = 'autonomous' AND active
LIMIT 1
"""

_CONSTITUENTS_OF_SECTOR = """
SELECT e.from_entity
FROM entity_edge e
JOIN entity etf ON etf.id = e.to_entity
WHERE e.relation = 'member_of_sector' AND etf.kind = 'sector_etf'
  AND etf.symbol = $1
"""


@dataclass(frozen=True)
class DemandReport:
    sectors_demanded: int = 0
    constituents_demanded: int = 0

    def summary(self) -> str:
        if not self.sectors_demanded and not self.constituents_demanded:
            return "no new standing demand (all constituents already covered)"
        return (
            f"standing demand confirmed for {self.sectors_demanded} sectors, "
            f"{self.constituents_demanded} new constituents"
        )


def _rank_sectors(scores: list[dict]) -> list[dict]:
    """Rank sector scores: highest RS first, favorable alignment as tiebreak."""
    alignment_rank = {"favorable": 0, "unknown": 1, "unfavorable": 2}

    return sorted(
        scores,
        key=lambda s: (
            -float(s["rs_percentile"]),
            alignment_rank.get(s.get("macro_alignment", "unknown"), 1),
        ),
    )


async def create_autonomous_demand(
    pool, *, top_n: int = _TOP_SECTORS, operator_user_id=None
) -> DemandReport:
    """Create autonomous price demand for the top sectors' constituents.

    Reads the latest sector_score per ETF, ranks them, and for the top ``top_n``
    creates (idempotently) autonomous demand on their constituents. The demand
    is for ``price_snapshot`` -- the fill loop then fetches the prices via
    Polygon, the predict loop runs the trend producer, and the surface loop
    publishes findings. This loop is the trigger; the existing chain does the
    work.

    ``operator_user_id`` is set as ``requested_by`` on every demand row, so the
    fill pipeline can attribute byo_only Polygon fetches to the operator.
    Without it, autonomous demand for prices stays unfillable
    (``MissingCredentialOwner`` in the writer).
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (e.symbol) e.symbol, c.value
        FROM claim c JOIN entity e ON e.id = c.entity_id
        WHERE c.claim_type = 'sector_score' AND c.superseded_by IS NULL
        ORDER BY e.symbol, c.knowledge_date DESC
        """
    )
    if not rows:
        return DemandReport()

    import json

    scores = []
    for r in rows:
        v = r["value"]
        if isinstance(v, (str, bytes)):
            v = json.loads(v)
        scores.append(v)

    ranked = _rank_sectors(scores)[:top_n]

    sectors_n = 0
    constituents_n = 0
    for entry in ranked:
        symbol = entry.get("etf_symbol")
        if not symbol:
            continue
        constituents = await pool.fetch(_CONSTITUENTS_OF_SECTOR, symbol)
        if not constituents:
            continue
        sectors_n += 1
        for row in constituents:
            entity_id = row["from_entity"]
            exists = await pool.fetchval(_EXISTING_AUTONOMOUS, entity_id)
            if exists:
                continue
            await autonomous_attention(
                pool,
                entity_id=entity_id,
                claim_type="price_snapshot",
                requested_by=operator_user_id,
                weight=_AUTONOMOUS_WEIGHT,
            )
            constituents_n += 1

    if constituents_n:
        logger.info(
            "autonomous demand: %d sectors, %d constituents",
            sectors_n, constituents_n,
        )
    return DemandReport(
        sectors_demanded=sectors_n, constituents_demanded=constituents_n
    )
