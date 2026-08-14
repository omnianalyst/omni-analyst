"""Phase E: synthesis findings -- connect the deduction chain.

The surface loop publishes a finding when a prediction clears the calibrated
threshold. This loop traces the macro and sector evidence behind that finding
and stores append-only enrichment revisions so later evidence cannot rewrite
what an earlier enrichment said.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("omni.autonomous.synthesis")


@dataclass(frozen=True)
class SynthesisReport:
    findings_enriched: int = 0
    findings_skipped: int = 0


_FINDINGS_TO_ENRICH = """
SELECT f.id, f.prediction_id, f.entity_id, f.audience_user_id,
       f.created_at, p.direction, p.confidence, p.method,
       revision.deduction_chain AS previous_chain
FROM finding f JOIN prediction p ON p.id = f.prediction_id
LEFT JOIN LATERAL (
  SELECT r.deduction_chain
  FROM finding_enrichment_revision r
  WHERE r.finding_id = f.id
  ORDER BY r.evidence_as_of DESC, r.created_at DESC, r.id DESC
  LIMIT 1
) revision ON true
WHERE f.status = 'surfaced'
  AND f.created_at <= $1
"""

_SECTOR_OF_COMPANY = """
SELECT etf.id, etf.symbol
FROM entity_edge e JOIN entity etf ON etf.id = e.to_entity
WHERE e.from_entity = $1 AND e.relation = 'member_of_sector'
  AND etf.kind = 'sector_etf'
  AND e.created_at <= $2
LIMIT 1
"""

_LATEST_SECTOR_SCORE = """
SELECT c.id, c.value, c.source, c.event_date, c.knowledge_date,
       c.confidence, c.credential_owner, c.redistributable,
       c.audience_user_id
FROM claim c
WHERE c.entity_id = $1 AND c.claim_type = 'sector_score'
  AND c.knowledge_date <= $3
  AND (
    (c.audience_user_id IS NULL AND c.redistributable = 'allowed')
    OR ($2::uuid IS NOT NULL AND c.audience_user_id = $2)
  )
ORDER BY c.knowledge_date DESC LIMIT 1
"""

_LATEST_REGIME = """
SELECT c.id, c.value, c.source, c.event_date, c.knowledge_date,
       c.confidence, c.credential_owner, c.redistributable,
       c.audience_user_id
FROM claim c
WHERE c.claim_type = 'regime_assessment'
  AND c.knowledge_date <= $1
  AND c.audience_user_id IS NULL AND c.redistributable = 'allowed'
ORDER BY c.knowledge_date DESC LIMIT 1
"""

_INSERT_REVISION = """
INSERT INTO finding_enrichment_revision
    (finding_id, evidence_as_of, deduction_chain)
VALUES ($1, $2, $3::jsonb)
"""


def _decode(raw):
    if isinstance(raw, (str, bytes)):
        return json.loads(raw)
    return raw


def _evidence(row) -> dict:
    return {
        "source": row["source"],
        "event_date": row["event_date"].isoformat(),
        "knowledge_date": row["knowledge_date"].isoformat(),
        "confidence": float(row["confidence"]),
        "credential_owner": row["credential_owner"],
        "redistributable": row["redistributable"],
        "audience_user_id": (
            str(row["audience_user_id"]) if row["audience_user_id"] is not None else None
        ),
    }


async def enrich_findings(pool, *, as_of: datetime | None = None) -> SynthesisReport:
    """Append point-in-time deduction-chain revisions for surfaced findings."""
    evidence_as_of = as_of or await pool.fetchval("SELECT now()")
    findings = await pool.fetch(_FINDINGS_TO_ENRICH, evidence_as_of)
    if not findings:
        return SynthesisReport()

    enriched = 0
    skipped = 0

    for finding in findings:
        chain = []
        stock_layer = {
            "layer": "stock",
            "entity_id": str(finding["entity_id"]),
            "prediction_id": str(finding["prediction_id"]),
            "direction": finding["direction"],
            "confidence": float(finding["confidence"]),
            "method": finding["method"],
            "audience_user_id": (
                str(finding["audience_user_id"])
                if finding["audience_user_id"] is not None
                else None
            ),
        }

        sector_row = await pool.fetchrow(
            _SECTOR_OF_COMPANY, finding["entity_id"], evidence_as_of
        )
        if sector_row is not None:
            score_row = await pool.fetchrow(
                _LATEST_SECTOR_SCORE,
                sector_row["id"],
                finding["audience_user_id"],
                evidence_as_of,
            )
            if score_row is not None:
                score = _decode(score_row["value"])
                chain.append(
                    {
                        "layer": "sector",
                        "claim_id": str(score_row["id"]),
                        "etf_symbol": sector_row["symbol"],
                        "rs_percentile": score.get("rs_percentile"),
                        "trend": score.get("trend"),
                        "macro_alignment": score.get("macro_alignment"),
                        "evidence": _evidence(score_row),
                    }
                )
                stock_layer["sector_etf"] = sector_row["symbol"]

        regime_row = await pool.fetchrow(_LATEST_REGIME, evidence_as_of)
        if regime_row is not None:
            regime = _decode(regime_row["value"])
            chain.append(
                {
                    "layer": "macro",
                    "claim_id": str(regime_row["id"]),
                    "cycle_phase": regime.get("cycle_phase"),
                    "risk_regime": regime.get("risk_regime"),
                    "inflation_regime": regime.get("inflation_regime"),
                    "policy_stance": regime.get("policy_stance"),
                    "recession_probability": regime.get("recession_probability"),
                    "evidence": _evidence(regime_row),
                }
            )

        macro = next((item for item in chain if item["layer"] == "macro"), None)
        sector = next((item for item in chain if item["layer"] == "sector"), None)
        chain_ordered = []
        if macro:
            chain_ordered.append(macro)
        if sector:
            chain_ordered.append(sector)
        chain_ordered.append(stock_layer)

        if _decode(finding["previous_chain"]) == chain_ordered:
            skipped += 1
            continue

        await pool.execute(
            _INSERT_REVISION,
            finding["id"],
            evidence_as_of,
            json.dumps(chain_ordered),
        )
        enriched += 1

    if enriched:
        logger.info("synthesis enriched %d findings", enriched)
    return SynthesisReport(findings_enriched=enriched, findings_skipped=skipped)
