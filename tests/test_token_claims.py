"""Token claim validation and middleware infrastructure behaviour (A07).

``int(claims.get("ver", 0))`` accepted coercion (``"3"``, ``3.0``, ``True``)
and raised on shapes a signed token can carry. The version claim is now
validated without coercion. The middleware also no longer downgrades a
verified token to anonymous when the database handle is missing -- that is
an outage, not a caller -- and the production resolver no longer has a
token-only fallback that bypasses revocation checks.
"""

from __future__ import annotations

import base64
import json
from uuid import uuid4

import pytest
from neutron.auth.jwt import create_token

from omni.auth import (
    resolve_audience_from_request,
    resolve_auth_version_from_request,
    verified_token_claims,
    verified_token_subject,
)
from omni.auth.middleware import ActivePrincipalMiddleware

GOOD_SECRET = "v" * 48


class _Req:
    def __init__(self, headers, state=None):
        self.headers = headers
        self.state = state


def _bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


def _b64url(payload: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b"=").decode()


def _signed(sub: str, ver) -> str:
    return create_token({"sub": sub, "ver": ver}, GOOD_SECRET)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("OMNI_JWT_SECRET", GOOD_SECRET)
    yield


SUBJECT = str(uuid4())


class TestVerClaimValidation:
    def test_a_plain_int_version_survives(self):
        claims = verified_token_claims(_Req(_bearer(_signed(SUBJECT, 3))))
        assert claims is not None
        assert claims["ver"] == 3
        assert type(claims["ver"]) is int

    def test_the_absent_legacy_claim_reads_as_zero(self):
        token = create_token({"sub": SUBJECT}, GOOD_SECRET)
        claims = verified_token_claims(_Req(_bearer(token)))
        assert claims is not None
        assert claims["ver"] == 0

    @pytest.mark.parametrize("ver", [None, True, False, 3.0, "3", [1], {"v": 1}])
    def test_coercible_or_structured_versions_are_anonymous(self, ver):
        assert verified_token_claims(_Req(_bearer(_signed(SUBJECT, ver)))) is None

    def test_a_numeric_string_version_is_anonymous(self):
        # json.dumps makes "3" a JSON string; int("3") used to accept it.
        assert verified_token_claims(_Req(_bearer(_signed(SUBJECT, "3")))) is None

    def test_a_huge_integer_version_is_anonymous_not_an_exception(self):
        assert (
            verified_token_claims(_Req(_bearer(_signed(SUBJECT, 2**31)))) is None
        )

    def test_a_negative_integer_version_is_anonymous(self):
        assert verified_token_claims(_Req(_bearer(_signed(SUBJECT, -1)))) is None


class TestResolverInfrastructureErrors:
    def test_a_request_without_middleware_state_raises(self):
        # The token-only fallback is gone: callers without middleware get
        # an infrastructure error, not an identity that skipped the
        # revocation and active checks.
        with pytest.raises(Exception) as excinfo:
            resolve_audience_from_request(_Req(_bearer(_signed(SUBJECT, 0))))
        assert "ActivePrincipalMiddleware" in str(excinfo.value)

    def test_signature_only_decoding_remains_available(self):
        assert verified_token_subject(_Req(_bearer(_signed(SUBJECT, 0)))) is not None


class _State:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _ScopeApp:
    def __init__(self, db=None):
        self.state = _State(db=db)


class TestMiddlewareBehaviour:
    async def _run(self, scope, inner=None):
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        async def default_inner(s, r, snd):
            await snd({"type": "http.response.start", "status": 200, "headers": []})

        await ActivePrincipalMiddleware(inner or default_inner)(scope, None, send)
        return sent

    def _scope(self, app, headers=None):
        return {
            "type": "http",
            "headers": [
                (k.encode(), v.encode()) for k, v in (headers or {}).items()
            ],
            "app": app,
        }

    async def test_a_verified_token_without_a_database_answers_503(self):
        # Anonymous is an answer about a caller; a missing database handle
        # is an outage. The old code proceeded anonymously and served
        # shared-data responses while every caller looked logged out.
        app = _ScopeApp(db=None)
        sent = await self._run(self._scope(app, _bearer(_signed(SUBJECT, 0))))
        assert sent[0]["status"] == 503

    async def test_anonymous_requests_still_pass_without_a_database(self):
        app = _ScopeApp(db=None)
        sent = await self._run(self._scope(app))
        assert sent[0]["status"] == 200

    async def test_state_carries_the_authenticated_epoch(self, db):
        from omni.main import create_app as _  # noqa: F401 - import sanity

        user_id = uuid4()
        await db.pool.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'x')",
            user_id,
            f"mw-{uuid4().hex[:6]}@example.com",
        )

        class _Db:
            def __init__(self, pool):
                self.pool = pool

        app = _ScopeApp(db=_Db(db.pool))
        scope = self._scope(app, _bearer(_signed(str(user_id), 0)))

        captured: dict = {}

        async def inner(s, r, snd):
            captured["audience"] = s["state"]["_omni_audience"]
            captured["version"] = s["state"]["_omni_auth_version"]
            await snd({"type": "http.response.start", "status": 200, "headers": []})

        async def swallow(message):
            pass

        await ActivePrincipalMiddleware(inner)(scope, None, swallow)
        assert captured["audience"] == user_id
        assert captured["version"] == 0

        state = _State(
            _omni_auth_checked=True, _omni_audience=user_id, _omni_auth_version=0
        )
        assert resolve_auth_version_from_request(_Req({}, state)) == 0
