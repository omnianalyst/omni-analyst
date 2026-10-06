"""The audience resolver: who is asking, or nobody.

This is the replacement for the ``X-User-Id`` header shim. The coverage and
briefing APIs used to read an unauthenticated header to decide which private
claims to serve; that made identity a claim any caller could make. The real
answer is a verified JWT, decoded here.

The contract of ``resolve_audience_from_request`` is narrow and load-bearing:

* a valid token for a currently active user yields that user's id;
* an absent, malformed, expired or tampered token yields ``None`` -- the shared
  network only;
* it never reads ``X-User-Id`` and never falls back to another identity;
* a token failure never raises: a broken token is an anonymous caller, not an
  error, and certainly not somebody else. A *configuration* failure (missing
  or short signing key) does raise: an infrastructure fault must not
  masquerade as an unauthenticated caller.

``None`` flows downstream into ``visible_claims`` as ``audience=None``, which
already means "the shared network alone". Nothing here changes that semantics;
it only makes the upstream of that value trustworthy.
"""

from __future__ import annotations

import os
from uuid import UUID

from neutron.auth.jwt import decode_token
from neutron.error import AppError, internal_error
from starlette.requests import Request

_MIN_SECRET_LENGTH = 32


def jwt_secret() -> str:
    """The HS256 signing key, read from the environment with no usable default.

    A signing key that shipped in the source would not be a signing key, so
    there is no default here: the operator sets ``OMNI_JWT_SECRET`` (or
    ``JWT_SECRET``) in the process environment (systemd EnvironmentFile, docker
    env, or an export) -- the standard home for a signing key, not .env. Missing
    or too short is a configuration error raised at the point a token must be
    issued.
    """
    raw = os.environ.get("OMNI_JWT_SECRET") or os.environ.get("JWT_SECRET")
    if not raw:
        from omni.config import settings
        raw = settings.omni_jwt_secret
    if not raw:
        raise internal_error("OMNI_JWT_SECRET is not configured")
    if len(raw) < _MIN_SECRET_LENGTH:
        raise internal_error(
            f"OMNI_JWT_SECRET must be at least {_MIN_SECRET_LENGTH} characters"
        )
    return raw


def verified_token_claims(request: Request) -> dict | None:
    """Return the verified Bearer token's claims, else ``None``.

    Never raises on a broken token: absent, malformed, expired or tampered is
    the anonymous case. A misconfigured signing key is NOT anonymous -- that
    is an infrastructure failure, and downgrading it to "nobody" would serve
    shared-data responses while every caller looks logged-out. ``jwt_secret``
    raises instead, so a configuration fault surfaces as an error.

    The ``ver`` claim is validated without coercion: ``int(...)`` on a signed
    claim would accept ``"3"``, ``3.0`` and ``True``, raise on a list, and
    overflow PostgreSQL's integer range on a huge value -- all from input
    this deployment signed but a previous version's shape did not bound. A
    ``ver`` that is not a plain int in range is a token this deployment
    never issued; it reads as anonymous.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):]
    # Configuration faults propagate: they are infrastructure failures, not
    # the anonymous case. Only token decode failures downgrade to anonymous.
    secret = jwt_secret()
    try:
        payload = decode_token(token, secret)
    except AppError:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        UUID(str(sub))
    except (ValueError, TypeError):
        return None
    version = payload.get("ver", 0)  # claim-less pre-074 tokens read as 0
    if type(version) is not int or not 0 <= version <= 2_147_483_647:
        return None
    return {**payload, "ver": version}


def verified_token_subject(request: Request) -> UUID | None:
    """Return the caller's user id from a verified Bearer token, else ``None``.

    Never raises: any failure to produce a verified identity is the anonymous
    case. Does not read ``X-User-Id`` under any circumstance.
    """
    payload = verified_token_claims(request)
    if payload is None:
        return None
    return UUID(str(payload["sub"]))


def resolve_audience_from_request(request: Request) -> UUID | None:
    """Return the active principal established by request middleware.

    Application requests ALWAYS arrive through ActivePrincipalMiddleware,
    which checked the token's revocation epoch and the user's active flag
    against the database. A request without that middleware state is not an
    anonymous caller -- it is a routing/infrastructure fault, and treating
    it as anonymous would serve exactly the token-only access (no
    revocation, no active check) the middleware exists to prevent. Such a
    call raises; signature-only decoding for isolated tests is
    ``verified_token_subject``.
    """
    state = getattr(request, "state", None)
    if state is None or not getattr(state, "_omni_auth_checked", False):
        raise internal_error(
            "ActivePrincipalMiddleware did not run for this request"
        )
    return getattr(state, "_omni_audience", None)


def resolve_role_from_request(request: Request) -> str | None:
    state = getattr(request, "state", None)
    if state is None or not getattr(state, "_omni_auth_checked", False):
        raise internal_error(
            "ActivePrincipalMiddleware did not run for this request"
        )
    return getattr(state, "_omni_role", None)


def resolve_auth_version_from_request(request: Request) -> int | None:
    """The authenticated row's auth_version, for compare-and-swap writes."""
    state = getattr(request, "state", None)
    if state is None or not getattr(state, "_omni_auth_checked", False):
        raise internal_error(
            "ActivePrincipalMiddleware did not run for this request"
        )
    return getattr(state, "_omni_auth_version", None)


__all__ = [
    "jwt_secret",
    "resolve_audience_from_request",
    "resolve_auth_version_from_request",
    "resolve_role_from_request",
    "verified_token_claims",
    "verified_token_subject",
]
