"""The research record: every hypothesis tested, and the bar it had to clear.

Read-only, and authenticated for the same reason `/system/status` is -- it
reveals what this deployment has searched and how hard, which is operator
information rather than public information.

This endpoint exists because a search that hides its failures misrepresents
itself in both directions: it can read as no search at all, or as an unbroken
run of successes. Forty-nine tested and forty-nine rejected is the honest state,
and it is also the evidence that the one strategy that did survive was not
selected by looking until something looked good.
"""

from __future__ import annotations

from neutron import App, Router
from neutron.error import forbidden, unauthorized
from starlette.requests import Request

from omni.auth import resolve_audience_from_request, resolve_role_from_request
from omni.research.decay import latest_edge_states
from omni.research.publish import read_history, summarise


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/research/hypotheses")
    async def hypotheses(request: Request) -> dict:
        if resolve_audience_from_request(request) is None:
            raise unauthorized("Authentication required")
        if resolve_role_from_request(request) != "operator":
            raise forbidden("Operator access required")

        history = await read_history(app.db.pool)
        return {"summary": summarise(history), "tests": history}

    @router.get("/research/edge-state")
    async def edge_state(request: Request) -> dict:
        if resolve_audience_from_request(request) is None:
            raise unauthorized("Authentication required")
        if resolve_role_from_request(request) != "operator":
            raise forbidden("Operator access required")

        states = await latest_edge_states(app.db.pool)
        return {
            "books": [
                {
                    "book": row.book,
                    "promoted": row.promoted,
                    "state": row.state,
                    "scored_sessions": row.scored_sessions,
                    "recent_sessions": row.recent_sessions,
                    "mean_session_excess": (
                        float(row.mean_session_excess)
                        if row.mean_session_excess is not None else None
                    ),
                    "decay_p": float(row.decay_p) if row.decay_p is not None else None,
                    "window_start": (
                        row.window_start.isoformat()
                        if row.window_start is not None else None
                    ),
                    "window_end": (
                        row.window_end.isoformat()
                        if row.window_end is not None else None
                    ),
                    "reason": row.reason,
                    "as_of": row.as_of.isoformat(),
                    "evaluated_at": (
                        row.evaluated_at.isoformat()
                        if row.evaluated_at is not None else None
                    ),
                }
                for row in states
            ],
            "alerts": [row.book for row in states if row.promoted and row.state == "decayed"],
        }

    return router
