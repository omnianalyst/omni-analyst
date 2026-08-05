"""Phase E: synthesis findings -- connect the deduction chain.

The existing surface loop publishes a finding when a prediction clears the
calibrated threshold. That finding says "AAPL up, 0.74 confidence." It does not
say WHY -- the macro backdrop, the sector that led the scanner to AAPL, the
chain of reasoning that connects the two. This loop enriches autonomous
findings with that chain, storing it in ``finding.deduction_chain`` so the UI
renders the full narrative: "expansion -> XLK strongest -> AAPL stands out at
0.74."

The chain is built by tracing backward from the finding:
  finding -> prediction -> entity -> sector_etf (via member_of_sector edge)
           -> latest sector_score -> latest regime_assessment

Each link is a real claim with provenance, event_date, knowledge_date. The
chain is auditable -- a user can trace any finding back to its macro root. If a
link is missing (no sector_score, no regime_assessment), the chain is partial;
the loop writes what it has rather than refusing, because a stock-level finding
without the macro context is still useful, just less narrated.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

logger = logging.getLogger("omni.autonomous.synthesis")


@dataclass(frozen=True)
class SynthesisReport:
    findings_enriched: int = 0
    findings_skipped: int = 0


_FINDINGS_TO_ENRICH = """
SELECT f.id, f.prediction_id, f.entity_id, p.direction, p.confidence, p.method
FROM finding f JOIN prediction p ON p.id = f.prediction_id
WHERE f.status = 'surfaced'
  AND COALESCE(f.deduction_chain, '[]'::jsonb) = '[]'::jsonb
"""

_SECTOR_OF_COMPANY = """
SELECT etf.id, etf.symbol
FROM entity_edge e JOIN entity etf ON etf.id = e.to_entity
WHERE e.from_entity = $1 AND e.relation = 'member_of_sector'
  AND etf.kind = 'sector_etf'
LIMIT 1
"""

_LATEST_SECTOR_SCORE = """
SELECT c.id, c.value
FROM claim c
WHERE c.entity_id = $1 AND c.claim_type = 'sector_score'
  AND c.superseded_by IS NULL
ORDER BY c.knowledge_date DESC LIMIT 1
"""

_LATEST_REGIME = """
SELECT c.id, c.value
FROM claim c
WHERE c.claim_type = 'regime_assessment' AND c.superseded_by IS NULL
  AND c.audience_user_id IS NULL AND c.redistributable = 'allowed'
ORDER BY c.knowledge_date DESC LIMIT 1
"""


def _decode(raw):
    if isinstance(raw, (str, bytes)):
        return json.loads(raw)
    return raw


async def enrich_findings(pool) -> SynthesisReport:
    """Trace the deduction chain for each surfaced autonomous finding.

    For every surfaced finding that has no chain yet, builds the macro ->
    sector -> stock narrative and writes it to ``deduction_chain``. Returns a
    report of how many were enriched. Skips findings that already have a chain
    (idempotent).
    """
    findings = await pool.fetch(_FINDINGS_TO_ENRICH)
    if not findings:
        return SynthesisReport()

    enriched = 0
    skipped = 0

    for f in findings:
        chain = []

        stock_layer = {
            "layer": "stock",
            "entity_id": str(f["entity_id"]),
            "prediction_id": str(f["prediction_id"]),
            "direction": f["direction"],
            "confidence": float(f["confidence"]),
            "method": f["method"],
        }

        sector_row = await pool.fetchrow(_SECTOR_OF_COMPANY, f["entity_id"])
        if sector_row is not None:
            score_row = await pool.fetchrow(
                _LATEST_SECTOR_SCORE, sector_row["id"]
            )
            if score_row is not None:
                sv = _decode(score_row["value"])
                chain.append({
                    "layer": "sector",
                    "claim_id": str(score_row["id"]),
                    "etf_symbol": sector_row["symbol"],
                    "rs_percentile": sv.get("rs_percentile"),
                    "trend": sv.get("trend"),
                    "macro_alignment": sv.get("macro_alignment"),
                })
                stock_layer["sector_etf"] = sector_row["symbol"]

        regime_row = await pool.fetchrow(_LATEST_REGIME)
        if regime_row is not None:
            rv = _decode(regime_row["value"])
            chain.append({
                "layer": "macro",
                "claim_id": str(regime_row["id"]),
                "cycle_phase": rv.get("cycle_phase"),
                "risk_regime": rv.get("risk_regime"),
                "inflation_regime": rv.get("inflation_regime"),
                "policy_stance": rv.get("policy_stance"),
                "recession_probability": rv.get("recession_probability"),
            })

        chain.append(stock_layer)

        chain_ordered = chain
        macro = next((c for c in chain_ordered if c["layer"] == "macro"), None)
        sector = next((c for c in chain_ordered if c["layer"] == "sector"), None)
        chain_ordered = []
        if macro:
            chain_ordered.append(macro)
        if sector:
            chain_ordered.append(sector)
        chain_ordered.append(stock_layer)

        await pool.execute(
            "UPDATE finding SET deduction_chain = $1::jsonb WHERE id = $2",
            json.dumps(chain_ordered),
            f["id"],
        )
        enriched += 1

    if enriched:
        logger.info("synthesis enriched %d findings", enriched)
    return SynthesisReport(findings_enriched=enriched, findings_skipped=skipped)
