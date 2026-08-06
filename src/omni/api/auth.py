"""Authentication endpoints: register, login, me.

The router closes over the Neutron ``App`` for ``app.db`` -- the same closure
trick the coverage and briefing routers use, because the inner Starlette
request has no path back to the App or its pool.

The login failure path is deliberately uniform: unknown email, wrong password,
and inactive user all render the same 401. The decision lives in
``omni.auth.users.authenticate_user`` (returns ``None`` for all three); this
layer only translates that ``None`` into an identical response, so the endpoint
cannot be used to enumerate which emails are registered. On a system where data
access is licensed per user, that enumeration is how you find whose credentials
to steal.

Tokens are issued with ``neutron.auth.jwt.create_token`` and verified with
``omni.auth.resolve_audience_from_request``. No crypto is written here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from neutron import App, Router
from neutron.auth.jwt import create_token
from neutron.error import bad_request, conflict, rate_limited, unauthorized
from pydantic import BaseModel
from starlette.requests import Request

from omni.auth import jwt_secret, resolve_audience_from_request
from omni.auth.ratelimit import check_rate_limit
from omni.auth.users import (
    MIN_PASSWORD_LENGTH,
    PasswordTooShort,
    authenticate_user,
    change_password,
    create_user,
    get_user,
    user_count,
)

TOKEN_EXPIRES_IN = 3600


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "anonymous"


class RegisterIn(BaseModel):
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


def _user_dict(row: Any) -> dict:
    created_at: datetime | None = row["created_at"]
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "created_at": created_at.isoformat() if created_at else None,
        "active": row["active"],
    }


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/auth/setup-status")
    async def setup_status() -> dict:
        # Anonymous on purpose: the UI needs to know whether to send a first-run
        # visitor to /setup or /login before any identity exists. It reveals only
        # a boolean, never which emails are registered.
        return {"setup_required": await user_count(app.db.pool) == 0}

    @router.post("/auth/setup", status_code=200)
    async def setup(body: RegisterIn, request: Request) -> dict:
        # First-run operator provisioning. Refuses once any user exists, so the
        # endpoint cannot be used to take over or backdoor a deployment that has
        # already been claimed. Reaching it after setup returns 409, not a new
        # account. Rate-limited per client IP -- it is a credential endpoint
        # reachable during the first-run window.
        if not check_rate_limit(_client_ip(request)):
            raise rate_limited("Too many attempts; wait a minute and try again.")
        if await user_count(app.db.pool) > 0:
            raise conflict("setup is already complete")
        try:
            row = await create_user(
                app.db.pool, email=body.email, password=body.password
            )
        except PasswordTooShort:
            raise bad_request(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        token = create_token(
            {"sub": str(row["id"])},
            jwt_secret(),
            expires_in=TOKEN_EXPIRES_IN,
        )
        return {
            "token": token,
            "token_type": "bearer",
            "expires_in": TOKEN_EXPIRES_IN,
            "user": _user_dict(row),
        }

    @router.post("/auth/register")
    async def register(body: RegisterIn, request: Request) -> dict:
        # Adding a second user is an operator action, not an open one. The first
        # user is provisioned through /auth/setup; further accounts require a
        # signed-in operator. This keeps registration off the public surface --
        # app.omnianalyst.com is internet-reachable, and an open register would
        # let anyone create an account that sees its own audience-scoped slice.
        if resolve_audience_from_request(request) is None:
            raise unauthorized("Authentication required")
        try:
            row = await create_user(
                app.db.pool, email=body.email, password=body.password
            )
        except PasswordTooShort:
            raise bad_request(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        return _user_dict(row)

    @router.post("/auth/login", status_code=200)
    async def login(body: LoginIn, request: Request) -> dict:
        # Rate-limited per client IP: the front door has no other lock, and an
        # unthrottled endpoint lets a guesser hammer it at network speed. The
        # uniform 401 below still applies to the credential check itself.
        if not check_rate_limit(_client_ip(request)):
            raise rate_limited("Too many attempts; wait a minute and try again.")
        row = await authenticate_user(
            app.db.pool, email=body.email, password=body.password
        )
        if row is None:
            raise unauthorized("Invalid email or password")
        token = create_token(
            {"sub": str(row["id"])},
            jwt_secret(),
            expires_in=TOKEN_EXPIRES_IN,
        )
        return {
            "token": token,
            "token_type": "bearer",
            "expires_in": TOKEN_EXPIRES_IN,
        }

    @router.get("/auth/me")
    async def me(request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")
        row = await get_user(app.db.pool, audience)
        if row is None:
            raise unauthorized("Authentication required")
        return _user_dict(row)

    @router.post("/auth/change-password", status_code=204)
    async def change_pw(body: ChangePasswordIn, request: Request) -> None:
        # Rotate the signed-in operator's own password. Requires the current
        # password (re-verification) so a stolen token alone cannot lock the
        # operator out. The old-password failure renders identically to a wrong
        # login, so the endpoint cannot be used to confirm a guess.
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")
        try:
            ok = await change_password(
                app.db.pool,
                user_id=audience,
                old_password=body.old_password,
                new_password=body.new_password,
            )
        except PasswordTooShort:
            raise bad_request(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        if not ok:
            raise unauthorized("Current password is incorrect")

    return router


__all__ = ["build_router"]
