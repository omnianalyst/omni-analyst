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

Admission (audit A01/A02) is ``omni.auth.admission.credential_guard`` around
every request that hashes a password: atomic IP/account budgets plus a
serialized check-verify-record transaction per (email, IP). Authorization
failures are raised AFTER the guard commits -- raising inside it would roll
the failure record back with the refusal.

Tokens are issued with ``neutron.auth.jwt.create_token`` and verified with
``omni.auth.resolve_audience_from_request``. No crypto is written here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from neutron import App, Router
from neutron.auth.jwt import create_token
from neutron.error import AppError, bad_request, conflict, forbidden, unauthorized
from pydantic import BaseModel, Field
from starlette.requests import Request

from omni.auth import (
    jwt_secret,
    resolve_audience_from_request,
    resolve_auth_version_from_request,
    resolve_role_from_request,
)
from omni.auth.admission import credential_guard
from omni.auth.forwarded import client_ip_from_request
from omni.auth.ratelimit import check_rate_limit
from omni.auth.throttle import record_login_failure, record_login_success
from omni.auth.users import (
    MIN_PASSWORD_LENGTH,
    CredentialChangedConcurrently,
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
    # retire tokens that have not expired yet. The version issued is the
    # one whose password was just verified -- never reread later, which
    # would make an old-password login into a newly valid token.
    return create_token(
        {"sub": str(row["id"]), "ver": row["auth_version"]},
        jwt_secret(),
        expires_in=TOKEN_EXPIRES_IN,
    )


def _burst_guard(request: Request) -> str:
    """The cheap per-process limiter ahead of any database work.

    Not the authoritative budget -- replicas each get this allowance -- but
    it absorbs a single-process hammer without touching PostgreSQL.
    """
    ip = client_ip_from_request(request)
    if not check_rate_limit(ip):
        from neutron.error import rate_limited

        raise rate_limited("Too many attempts; wait a minute and try again.")
    return ip


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
        ip = _burst_guard(request)
        try:
            async with credential_guard(
                app.db.pool, email=body.email, ip=ip
            ) as conn:
                row = await create_initial_operator(
                    conn, email=body.email, password=body.password
                )
                await record_login_success(conn, email=body.email, ip=ip)
        except PasswordTooShort:
            raise bad_request(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        except AppError:
            # Setup already complete is a failed claim on a credential
            # endpoint; the pair's history should say so. Recorded after
            # the guard: the refused claim rolled the transaction back, and
            # the record must outlive it.
            await record_login_failure(app.db.pool, email=body.email, ip=ip)
            raise
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
        # Register hashes a password through the same single executor as
        # login, so it takes the same admission (A02): an operator script
        # creating accounts in a loop cannot queue unbounded hashing work.
        ip = _burst_guard(request)
        try:
            async with credential_guard(
                app.db.pool, email=body.email, ip=ip
            ) as conn:
                row = await create_user(
                    conn, email=body.email, password=body.password
                )
        except PasswordTooShort:
            raise bad_request(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        return _user_dict(row)

    @router.post("/auth/login", status_code=200)
    async def login(body: LoginIn, request: Request) -> dict:
        # Admission is one serialized unit per (email, IP): budgets, throttle
        # check, password verification and the outcome record all commit (or
        # roll back) together. The 401 for a failed verification is raised
        # after the guard exits -- inside it, the rollback would erase the
        # failure the next admission decision reads.
        ip = _burst_guard(request)
        async with credential_guard(
            app.db.pool, email=body.email, ip=ip
        ) as conn:
            row = await authenticate_user(
                conn, email=body.email, password=body.password
            )
            if row is None:
                await record_login_failure(conn, email=body.email, ip=ip)
            else:
                await record_login_success(conn, email=body.email, ip=ip)
        if row is None:
            raise unauthorized("Invalid email or password")
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
        # Rotate the signed-in user's own password. The account is the
        # AUTHENTICATED one -- its email is loaded server-side, never taken
        # from the request, which only carries passwords. Admission keys on
        # that server-loaded email (A02): a stolen session cannot hammer the
        # hashing executor with password guesses unthrottled.
        #
        # A wrong current password answers 400, not 401: the bearer session
        # IS valid, and a 401 here would make the client clear it -- one
        # typo logging the operator out. Guessing still requires a working
        # session, so nothing is enumerated that was not already.
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")
        email = await app.db.pool.fetchval(
            "SELECT email FROM users WHERE id = $1 AND active", audience
        )
        if email is None:
            raise unauthorized("Authentication required")
        expected_auth_version = resolve_auth_version_from_request(request)
        ip = _burst_guard(request)
        try:
            async with credential_guard(
                app.db.pool, email=email, ip=ip
            ) as conn:
                ok = await change_password(
                    conn,
                    user_id=audience,
                    old_password=body.old_password,
                    new_password=body.new_password,
                    expected_auth_version=expected_auth_version,
                )
        except PasswordTooShort:
            raise bad_request(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        except CredentialChangedConcurrently:
            raise conflict(
                "Your credentials changed in another session; "
                "sign in again and retry"
            )
        except ValueError as exc:
            raise bad_request(str(exc)) from exc
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
