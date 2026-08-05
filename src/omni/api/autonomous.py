"""HTTP read API for the autonomous layer's output.

The deduction chain produces three kinds of output a user or UI wants to read:
the macro regime assessment (shared, FRED-derived), the sector scores
(byo_only, Polygon-derived), and the surfaced findings (audience-scoped). The
briefing router already serves findings; this router serves the macro and sector
layers that sit above them in the chain.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from neutron import App, Router
from starlette.requests import Request

from omni.auth import resolve_audience_from_request


def _audience(request: Request) -> UUID | None:
    return resolve_audience_from_request(request)


def _jsonb(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/autonomous/regime")
    async def get_regime() -> dict:
        """The latest macro regime assessment.

        Shared coverage (FRED is allowed-class), so no auth required: every
        caller sees the same regime. Returns ``{}`` when no assessment exists
        yet (the macro loop abstains when FRED data is insufficient).
        """
        row = await app.db.pool.fetchrow(
            """
            SELECT value, event_date, knowledge_date
            FROM claim
            WHERE claim_type = 'regime_assessment'
              AND audience_user_id IS NULL
              AND redistributable = 'allowed'
              AND superseded_by IS NULL
            ORDER BY knowledge_date DESC LIMIT 1
            """
        )
        if row is None:
            return {}
        return {
            "value": _jsonb(row["value"]),
            "event_date": row["event_date"].isoformat() if row["event_date"] else None,
            "knowledge_date": (
                row["knowledge_date"].isoformat() if row["knowledge_date"] else None
            ),
        }

    @router.get("/autonomous/sectors")
    async def get_sectors(request: Request) -> list[dict]:
        """Latest sector scores, one per ETF.

        Sector scores derive from Polygon prices (byo_only), so they are
        audience-scoped: an anonymous caller sees only shared scores (if any);
        the operator sees their private scores. Returns ``[]`` when no scores
        exist or the caller has no visible ones.
        """
        audience = _audience(request)
        rows = await app.db.pool.fetch(
            """
            SELECT DISTINCT ON (e.symbol) e.symbol, e.name, c.value
            FROM claim c JOIN entity e ON e.id = c.entity_id
            WHERE c.claim_type = 'sector_score'
              AND c.superseded_by IS NULL
              AND (c.audience_user_id IS NULL OR c.audience_user_id = $1)
            ORDER BY e.symbol, c.knowledge_date DESC
            """,
            audience,
        )
        return [
            {"symbol": r["symbol"], "name": r["name"], "score": _jsonb(r["value"])}
            for r in rows
        ]

    return router
