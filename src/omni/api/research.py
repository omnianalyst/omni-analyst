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
from neutron.error import unauthorized
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.research.publish import read_history, summarise


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/research/hypotheses")
    async def hypotheses(request: Request) -> dict:
        if resolve_audience_from_request(request) is None:
            raise unauthorized("Authentication required")

        history = await read_history(app.db.pool)
        return {"summary": summarise(history), "tests": history}

    return router
