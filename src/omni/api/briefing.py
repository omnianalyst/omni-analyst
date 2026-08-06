"""HTTP read API over the findings pipeline.

The pipeline in ``omni.conviction.publish`` records what was surfaced, what was
refused, and the hit rate on what the system chose to say. None of it had a URL;
these endpoints give it one.

Audience scoping is the same rule the coverage API enforces, for the same
reason: a finding derived from a user's licensed data belongs to that user, and
serving it to anyone else makes this deployment the redistributor. Identity
comes from a verified JWT (``resolve_audience_from_request``); absent means the
shared feed only.

The router closes over the Neutron ``App`` for ``app.db``, the same closure
trick the coverage and objective routers use -- the inner Starlette request has
no path back to the App or its pool.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from neutron import App, Router
from neutron.error import unauthorized
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.conviction.publish import briefing, refusal_counts, scorecard


def _audience(request: Request) -> UUID | None:
    """Who is asking, from a verified token.

    An absent or invalid token is an anonymous caller, which on the read paths
    that return findings means shared coverage only.
    """
    return resolve_audience_from_request(request)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _jsonb(value: Any) -> Any:
    """asyncpg returns jsonb columns as text without a codec; decode so the
    response speaks JSON, not a serialized string."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def _finding_to_dict(row) -> dict:
    # Disconfirming evidence is rendered even when empty: a finding shown
    # without it reads as advocacy, and the client must not have to ask twice.
    return {
        "id": str(row["id"]),
        "claim_id": str(row["claim_id"]),
        "entity_id": str(row["entity_id"]),
        "entity": {
            "id": str(row["entity_id"]),
            "symbol": row["symbol"],
            "name": row["name"],
        },
        "method": row["method"],
        "confidence": row["confidence"],
        "threshold": row["threshold"],
        "calibrated_hit_rate": row["calibrated_hit_rate"],
        "direction": row["direction"],
        "entry_price": float(row["entry_price"]) if row.get("entry_price") is not None else None,
        "upper_barrier": float(row["upper_barrier"]) if row.get("upper_barrier") is not None else None,
        "lower_barrier": float(row["lower_barrier"]) if row.get("lower_barrier") is not None else None,
        "supporting": _jsonb(row["supporting"]),
        "disconfirming": _jsonb(row["disconfirming"]),
        "prediction_id": (
            str(row["prediction_id"]) if row["prediction_id"] else None
        ),
        "deduction_chain": _jsonb(row.get("deduction_chain") or "[]"),
        "created_at": _iso(row["created_at"]),
    }


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/briefing")
    async def get_briefing(request: Request) -> list[dict]:
        """The feed of what the system chose to say, newest first.

        An empty feed returns an empty list. Silence is a real answer here --
        dressing it up as a placeholder would undo the reason the gate exists.
        """
        audience = _audience(request)
        rows = await briefing(app.db.pool, audience=audience)
        return [_finding_to_dict(r) for r in rows]

    @router.get("/briefing/scorecard")
    async def get_scorecard(request: Request) -> list[dict]:
        """Accuracy per method on surfaced findings.

        Operator-only: the hit rate is computed from findings whose predictions
        were sourced under the operator's BYO credentials, so it is private
        intelligence, not a public stat. An anonymous caller gets nothing.

        Scoped to the caller's audience (shared network plus their own private
        findings); a second operator's byo-derived rate never enters the sum.

        ``hit_rate`` is null below the ten-resolution floor, not zero: a rate
        computed from a handful of resolutions is noise wearing a percentage
        sign. The floor is applied in ``publish.scorecard``; this layer carries
        it through rather than re-deriving it.
        """
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")
        return await scorecard(app.db.pool, audience=audience)

    @router.get("/briefing/refusals")
    async def get_refusals(request: Request) -> dict[str, int]:
        """Refused findings counted by reason.

        Operator-only, for the same licensing reason as the scorecard: the
        refusal mix is a function of which BYO-sourced demand was attempted.

        Scoped to the caller's audience, same as the scorecard. The denominator
        behind the scorecard, and the endpoint that makes the published hit rate
        believable.
        """
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")
        return await refusal_counts(app.db.pool, audience=audience)

    return router
