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
from neutron.error import bad_request, unauthorized
from pydantic import BaseModel
from starlette.requests import Request

from omni.auth import jwt_secret, resolve_audience_from_request
from omni.auth.users import (
    MIN_PASSWORD_LENGTH,
    PasswordTooShort,
    authenticate_user,
    create_user,
    get_user,
)

TOKEN_EXPIRES_IN = 3600


class RegisterIn(BaseModel):
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


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

    @router.post("/auth/register")
    async def register(body: RegisterIn) -> dict:
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
    async def login(body: LoginIn) -> dict:
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

    return router


__all__ = ["build_router"]
