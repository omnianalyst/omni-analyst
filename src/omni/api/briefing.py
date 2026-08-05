"""HTTP read API over the findings pipeline.

The pipeline in ``omni.conviction.publish`` records what was surfaced, what was
refused, and the hit rate on what the system chose to say. None of it had a URL;
these endpoints give it one.

Audience scoping is the same rule the coverage API enforces, for the same
reason: a finding derived from a user's licensed data belongs to that user, and
serving it to anyone else makes this deployment the redistributor. ``X-User-Id``
is the access-control hint; absent means the shared feed only.

The router closes over the Neutron ``App`` for ``app.db``, the same closure
trick the coverage and objective routers use -- the inner Starlette request has
no path back to the App or its pool.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from neutron import App, Router
from neutron.error import bad_request
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.conviction.publish import briefing, refusal_counts, scorecard


def _audience(request: Request) -> UUID | None:
    """Who is asking, from a verified token — never from a header.

    This read X-User-Id, so any caller could name any user and read their
    licensed claims. The store's constraints were sound and the identity
    in front of them was a claim. An absent or invalid token is an
    anonymous caller, which means shared coverage only.
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
    async def get_scorecard() -> list[dict]:
        """Accuracy per method on surfaced findings.

        ``hit_rate`` is null below the ten-resolution floor, not zero: a rate
        computed from a handful of resolutions is noise wearing a percentage
        sign. The floor is applied in ``publish.scorecard``; this layer carries
        it through rather than re-deriving it.
        """
        return await scorecard(app.db.pool)

    @router.get("/briefing/refusals")
    async def get_refusals() -> dict[str, int]:
        """Refused findings counted by reason.

        The denominator behind the scorecard, and the endpoint that makes the
        published hit rate believable.
        """
        return await refusal_counts(app.db.pool)

    return router
