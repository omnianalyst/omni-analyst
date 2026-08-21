"""Data export: the caller's own data out of the system, as files.

Self-hosting means the data is yours; an export surface is part of that
promise. Three datasets, each audience-scoped exactly like its in-app view:

  * /export/holdings -- the caller's manual positions (their own notebook).
  * /export/claims/{entity_id} -- the claims the caller may see for one
    entity, through the same visibility CTE every claim read uses. Per entity
    on purpose: the whole store is millions of rows and "export everything"
    is not a file, it is a database dump (ops/backup.sh is that tool).
  * /export/scorecard -- accuracy per method, same scoping as the in-app
    scorecard (BYO-derived rates are private intelligence).

format=csv (default) returns text/csv with a download disposition; format=json
returns the same rows as JSON. CSV cells are the flat scalar columns; JSON
carries the full shapes (claim values stay objects).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from uuid import UUID

from neutron import App, Router
from neutron.error import bad_request, not_found, unauthorized
from starlette.requests import Request
from starlette.responses import Response

from omni.auth import resolve_audience_from_request
from omni.conviction.publish import scorecard
from omni.coverage.visibility import visible_claims

__all__ = ["build_router"]

_MAX_CLAIM_ROWS = 50_000


def _require_user(request: Request) -> UUID:
    user = resolve_audience_from_request(request)
    if user is None:
        raise unauthorized("Authentication required")
    return user


def _csv_response(rows: list[dict], filename: str) -> Response:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow({
            k: v.isoformat() if isinstance(v, (datetime, date)) else v
            for k, v in row.items()
        })
    return Response(
        out.getvalue(),
        media_type="text/csv",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


def _json_response(payload, filename: str) -> Response:
    return Response(
        json.dumps(payload, default=str),
        media_type="application/json",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


def _wants_csv(request: Request) -> bool:
    fmt = request.query_params.get("format", "csv").lower()
    if fmt not in ("csv", "json"):
        raise bad_request("format must be csv or json")
    return fmt == "csv"


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/export/holdings")
    async def export_holdings(request: Request) -> Response:
        """The caller's holdings, priced exactly as the portfolio page prices them."""
        user = _require_user(request)
        # Private import, deliberately: the pricing join (latest audience-
        # visible close per symbol) already exists in the holdings router and
        # a second implementation here is how the export and the page would
        # come to disagree about what a position is worth.
        from omni.api.holdings import _PRICED_HOLDINGS

        rows = await app.db.pool.fetch(
            _PRICED_HOLDINGS + ";",
            user, None,
        )
        records = [
            {
                "symbol": r["symbol"],
                "quantity": r["quantity"],
                "cost_basis": r["cost_basis"],
                "currency": r["currency"],
                "note": r["note"],
                "last_price": r["price"],
                "price_as_of": r["price_date"],
            }
            for r in rows
        ]
        if not records:
            if _wants_csv(request):
                # Header-only CSV: an empty export is still a valid file, and
                # the column set documents the shape of what is absent.
                records = [{
                    "symbol": None, "quantity": None, "cost_basis": None,
                    "currency": None, "note": None, "last_price": None,
                    "price_as_of": None,
                }]
            else:
                return _json_response({"holdings": []}, "holdings.json")
        if _wants_csv(request):
            return _csv_response(records, "holdings.csv")
        return _json_response({"holdings": records}, "holdings.json")

    @router.get("/export/claims/{entity_id}")
    async def export_claims(entity_id: UUID, request: Request) -> Response:
        """One entity's claims as the caller may see them (audience-scoped)."""
        user = _require_user(request)
        exists = await app.db.pool.fetchval(
            "SELECT 1 FROM entity WHERE id = $1", entity_id
        )
        if not exists:
            raise not_found(f"No entity {entity_id}")
        claims = await visible_claims(
            app.db.pool, audience=user, entity_id=entity_id, claim_type=None,
        )
        if len(claims) > _MAX_CLAIM_ROWS:
            raise bad_request(
                f"entity has {len(claims)} visible claims; export is capped at "
                f"{_MAX_CLAIM_ROWS}. Use ops/backup.sh for full-store dumps."
            )
        rows = [
            {
                "claim_type": c["claim_type"],
                "key": c["key"],
                "value": json.dumps(c["value"])
                if not isinstance(c["value"], str) else c["value"],
                "source": c["source"],
                "event_date": c["event_date"],
                "knowledge_date": c["knowledge_date"],
                "confidence": c["confidence"],
                "redistributable": c["redistributable"],
            }
            for c in claims
        ]
        if not rows:
            rows = [{
                "claim_type": None, "key": None, "value": None,
                "source": None, "event_date": None, "knowledge_date": None,
                "confidence": None, "redistributable": None,
            }]
        if _wants_csv(request):
            return _csv_response(rows, f"claims-{entity_id}.csv")
        return _json_response(
            {"entity_id": str(entity_id), "claims": rows},
            f"claims-{entity_id}.json",
        )

    @router.get("/export/scorecard")
    async def export_scorecard(request: Request) -> Response:
        """Accuracy per method, the same scoping as the in-app scorecard."""
        user = _require_user(request)
        rows = await scorecard(app.db.pool, audience=user)
        if not rows:
            rows = [{
                "method": None, "resolved": None, "hits": None,
                "hit_rate": None, "pending": None,
            }]
        if _wants_csv(request):
            return _csv_response(rows, "scorecard.csv")
        return _json_response({"scorecard": rows}, "scorecard.json")

    return router
