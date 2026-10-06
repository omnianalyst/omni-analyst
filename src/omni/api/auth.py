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
from neutron.error import bad_request, forbidden, rate_limited, unauthorized
from pydantic import BaseModel, Field
from starlette.requests import Request

from omni.auth import (
    jwt_secret,
    resolve_audience_from_request,
    resolve_role_from_request,
)
from omni.auth.forwarded import client_ip_from_request
from omni.auth.ratelimit import check_rate_limit
from omni.auth.throttle import (
    check_login_throttle,
    record_login_failure,
    record_login_success,
)
from omni.auth.users import (
    MIN_PASSWORD_LENGTH,
    PasswordTooShort,
    authenticate_user,
    bump_auth_version,
    change_password,
    create_initial_operator,
    create_user,
    get_user,
    user_count,
)
from omni.config import settings

TOKEN_EXPIRES_IN = settings.token_expires_in

# Credential-field ceilings: these bodies reach Argon2 and the single hashing
# executor, so an unbounded string is a cheap way to tie that worker up. The
# email bound is the RFC-wise maximum (320); the password bound is far above
# any real credential and far below anything worth hashing.
MAX_EMAIL_LENGTH = 320
MAX_PASSWORD_LENGTH = 1024


class RegisterIn(BaseModel):
    email: str = Field(max_length=MAX_EMAIL_LENGTH)
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class LoginIn(BaseModel):
    email: str = Field(max_length=MAX_EMAIL_LENGTH)
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(max_length=MAX_PASSWORD_LENGTH)


def _user_dict(row: Any) -> dict:
    created_at: datetime | None = row["created_at"]
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "created_at": created_at.isoformat() if created_at else None,
        "active": row["active"],
        "role": row["role"],
    }


def _issue_token(row: Any) -> str:
    # ver is the revocation epoch checked by ActivePrincipalMiddleware;
    # riding it in the claim is what lets a password change or logout-all
    # retire tokens that have not expired yet.
    return create_token(
        {"sub": str(row["id"]), "ver": row["auth_version"]},
        jwt_secret(),
        expires_in=TOKEN_EXPIRES_IN,
    )


async def _admit(pool, request: Request, email: str) -> None:
    """Both gates every credential endpoint passes before any hashing.

    The in-memory per-IP window is the cheap burst guard; the database check
    is the authoritative, replica-shared lockout keyed by (email, ip) with
    escalating cooldowns. Either refusing answers 429.
    """
    ip = client_ip_from_request(request)
    if not check_rate_limit(ip):
        raise rate_limited("Too many attempts; wait a minute and try again.")
    admitted, _retry_after = await check_login_throttle(
        pool, email=email, ip=ip
    )
    if not admitted:
        raise rate_limited("Too many attempts; wait a minute and try again.")


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
        await _admit(app.db.pool, request, body.email)
        try:
            row = await create_initial_operator(
                app.db.pool, email=body.email, password=body.password
            )
        except PasswordTooShort:
            raise bad_request(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        await record_login_success(
            app.db.pool, email=body.email, ip=client_ip_from_request(request)
        )
        return {
            "token": _issue_token(row),
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
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")
        if resolve_role_from_request(request) != "operator":
            raise forbidden("Operator access required")
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
        # Two-layer throttling before any hashing, then the uniform 401 below.
        # Failures are recorded only when admitted, so a locked-out guesser
        # does not extend the lockout, and a successful login clears the key.
        await _admit(app.db.pool, request, body.email)
        ip = client_ip_from_request(request)
        row = await authenticate_user(
            app.db.pool, email=body.email, password=body.password
        )
        if row is None:
            await record_login_failure(app.db.pool, email=body.email, ip=ip)
            raise unauthorized("Invalid email or password")
        await record_login_success(app.db.pool, email=body.email, ip=ip)
        return {
            "token": _issue_token(row),
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
        # operator out. A wrong current password answers 400, not 401: the
        # bearer session IS valid, and a 401 here would make the client clear
        # it -- one typo logging the operator out. Guessing still requires a
        # working session, so nothing is enumerated that was not already.
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
            raise bad_request("Current password is incorrect")

    @router.post("/auth/logout-all", status_code=204)
    async def logout_all(request: Request) -> None:
        # Revoke every bearer token issued to the signed-in user so far --
        # including this one -- by moving the user's auth_version past every
        # ver claim in flight. The password is untouched; the client clears
        # its stored token and re-authenticates.
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")
        await bump_auth_version(app.db.pool, audience)

    return router


__all__ = ["build_router"]
