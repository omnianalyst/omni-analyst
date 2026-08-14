from __future__ import annotations

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from omni.auth import verified_token_subject


class ActivePrincipalMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["_omni_auth_checked"] = True
        state["_omni_audience"] = None
        state["_omni_role"] = None

        audience = verified_token_subject(Request(scope))
        db = getattr(getattr(scope.get("app"), "state", None), "db", None)
        if audience is not None and db is not None:
            row = await db.pool.fetchrow(
                "SELECT id, role FROM users WHERE id = $1 AND active",
                audience,
            )
            if row is not None:
                state["_omni_audience"] = row["id"]
                state["_omni_role"] = row["role"]

        await self.app(scope, receive, send)


__all__ = ["ActivePrincipalMiddleware"]
