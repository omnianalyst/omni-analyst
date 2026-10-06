from __future__ import annotations

from uuid import UUID

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from omni.auth import verified_token_claims


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

        claims = verified_token_claims(Request(scope))
        db = getattr(getattr(scope.get("app"), "state", None), "db", None)
        if claims is not None and db is not None:
            # The ver claim is the token's revocation epoch: password change
            # and logout-all increment users.auth_version, and a token minted
            # before that increment no longer speaks for the user. Claim-less
            # tokens read as 0, matching the column default, so deploying
            # this check does not log out every existing session.
            row = await db.pool.fetchrow(
                "SELECT id, role FROM users "
                "WHERE id = $1 AND active AND auth_version = $2",
                UUID(str(claims["sub"])),
                int(claims.get("ver", 0)),
            )
            if row is not None:
                state["_omni_audience"] = row["id"]
                state["_omni_role"] = row["role"]

        await self.app(scope, receive, send)


__all__ = ["ActivePrincipalMiddleware"]
