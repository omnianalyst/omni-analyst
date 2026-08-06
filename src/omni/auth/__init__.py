"""The audience resolver: who is asking, or nobody.

This is the replacement for the ``X-User-Id`` header shim. The coverage and
briefing APIs used to read an unauthenticated header to decide which private
claims to serve; that made identity a claim any caller could make. The real
answer is a verified JWT, decoded here.

The contract of ``resolve_audience_from_request`` is narrow and load-bearing:

* a valid token yields that user's id;
* an absent, malformed, expired or tampered token yields ``None`` -- the shared
  network only;
* it never reads ``X-User-Id`` and never falls back to another identity;
* it never raises. A broken token is an anonymous caller, not an error, and
  certainly not somebody else.

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


def resolve_audience_from_request(request: Request) -> UUID | None:
    """Return the caller's user id from a verified Bearer token, else ``None``.

    Never raises: any failure to produce a verified identity is the anonymous
    case. Does not read ``X-User-Id`` under any circumstance.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):]
    try:
        secret = jwt_secret()
    except AppError:
        return None
    try:
        payload = decode_token(token, secret)
    except Exception:  # noqa: BLE001 - any decode failure = not authenticated, never a 500
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        return UUID(str(sub))
    except (ValueError, TypeError):
        return None


__all__ = ["jwt_secret", "resolve_audience_from_request"]
