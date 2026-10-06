"""The request middleware that turns a verified token into an active principal.

Signature validity alone is not identity: the database row decides. A token
whose ``ver`` claim no longer matches ``users.auth_version`` was issued
before a password change or logout-all and speaks for nobody, and a user
row that is gone or inactive has no audience regardless of what any token
says.

If a verified token exists but the database handle does not, the request
answers 503 rather than proceeding as anonymous. Anonymous is a real answer
about a caller; a missing infrastructure dependency is an outage, and
serving shared-data responses during one would let every logged-in caller
be quietly downgraded.
"""

from __future__ import annotations

from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse
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
        state["_omni_auth_version"] = None

        claims = verified_token_claims(Request(scope))
        if claims is None:
            await self.app(scope, receive, send)
            return

        db = getattr(getattr(scope.get("app"), "state", None), "db", None)
        if db is None:
            response = JSONResponse(
                {"detail": "authentication store unavailable"}, status_code=503
            )
            await response(scope, receive, send)
            return

        # The ver claim is the token's revocation epoch: password change
        # and logout-all increment users.auth_version, and a token minted
        # before that increment no longer speaks for the user. Claim-less
        # tokens read as 0, matching the column default, so deploying
        # this check does not log out every existing session. The row's
        # auth_version is exported to request state so compare-and-swap
        # writes (password change) bind to the exact epoch that was
        # authenticated -- never to a user-supplied value.
        row = await db.pool.fetchrow(
            "SELECT id, role, auth_version FROM users "
            "WHERE id = $1 AND active AND auth_version = $2",
            UUID(str(claims["sub"])),
            claims["ver"],
        )
        if row is not None:
            state["_omni_audience"] = row["id"]
            state["_omni_role"] = row["role"]
            state["_omni_auth_version"] = row["auth_version"]

        await self.app(scope, receive, send)


__all__ = ["ActivePrincipalMiddleware"]
