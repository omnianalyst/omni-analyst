"""Authentication: the audience must be real, not asserted.

The redistribution guarantee in omni.coverage.visibility turns on
``audience_user_id``. Until now that value came from an unauthenticated
``X-User-Id`` header, so any caller could name any user and read their licensed
data. These tests pin the replacement: a verified JWT resolves to a real user,
and everything else -- absent, tampered, wrongly-signed, alg=none, expired,
claiming to be somebody else -- resolves to anonymous (``None``), never to the
named user.

The forgery-resolves-to-None test is the one the entire licence model depends
on; if it regresses, the private tier is open.
"""

import asyncio
import base64
import json
from uuid import uuid4

import pytest
from neutron.test import TestClient

from omni.api.auth import build_router
from omni.auth import resolve_audience_from_request
from omni.main import create_app

GOOD_SECRET = "x" * 48
WRONG_SECRET = "y" * 48


class _Lifespan:
    """Drive the ASGI lifespan protocol, which httpx's ASGITransport skips."""

    def __init__(self, app):
        self._app = app
        self._receive = asyncio.Queue()
        self._send = asyncio.Queue()
        self._task = None

    async def __aenter__(self):
        self._task = asyncio.create_task(
            self._app({"type": "lifespan"}, self._receive.get, self._send.put)
        )
        await self._receive.put({"type": "lifespan.startup"})
        message = await self._send.get()
        assert message["type"] == "lifespan.startup.complete", message
        return self._app

    async def __aexit__(self, *exc):
        await self._receive.put({"type": "lifespan.shutdown"})
        await self._send.get()
        await self._task


class _Req:
    """Minimal stand-in for starlette.Request: resolve_audience_from_request
    only reads request.headers.get(...)."""

    def __init__(self, headers):
        self.headers = headers


def _bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


def _b64url(payload: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b"=").decode()


def _make_app(database_url):
    app = create_app(database_url)
    app.include_router(build_router(app))
    return app


async def _setup_first_user(client, email="op@example.com", password="a" * 16):
    """Provision the operator through the first-run /auth/setup endpoint and
    return the issued token. Every test that needs a user starts here now that
    open /auth/register is gated behind a signed-in operator."""
    r = await client.post(
        "/auth/setup", json={"email": email, "password": password}
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("OMNI_JWT_SECRET", GOOD_SECRET)
    yield


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


async def test_login_is_rate_limited_per_client_ip(db, database_url):
    # The front door has no other lock; an unthrottled /auth/login lets a
    # guesser hammer it at network speed. After the per-IP ceiling the next
    # attempt is refused with 429, not 401 (so a brute-forcer cannot tell
    # ceiling from a wrong-password).
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        # First provision so login can actually authenticate -- the limit counts
        # attempts regardless of correctness.
        await client.post(
            "/auth/setup",
            json={"email": "op@example.com", "password": "a" * 16},
        )
        # The autouse conftest reset cleared the setup attempt; flush the window
        # again so only the logins below count.
        from omni.auth.ratelimit import reset_for_test

        reset_for_test()

        for _ in range(5):
            r = await client.post(
                "/auth/login",
                json={"email": "op@example.com", "password": "wrong"},
            )
            assert r.status_code == 401, r.text

        r_limited = await client.post(
            "/auth/login",
            json={"email": "op@example.com", "password": "wrong"},
        )
        assert r_limited.status_code == 429


async def test_setup_then_login_round_trips_to_the_same_user(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r_setup = await client.post(
            "/auth/setup",
            json={"email": "alice@example.com", "password": "a" * 16},
        )
        assert r_setup.status_code == 200, r_setup.text
        registered_id = r_setup.json()["user"]["id"]
        assert r_setup.json()["user"]["role"] == "operator"
        # setup returns a token already; prove it resolves to the new user.
        setup_token = r_setup.json()["token"]

        r_login = await client.post(
            "/auth/login",
            json={"email": "ALICE@example.com", "password": "a" * 16},
        )
        assert r_login.status_code == 200, r_login.text
        token = r_login.json()["token"]

        r_me = await client.get("/auth/me", headers=_bearer(token))

    # The login is case-insensitive and yields a token that maps back.
    resolved = resolve_audience_from_request(_Req(_bearer(token)))
    assert resolved is not None
    assert str(resolved) == registered_id

    # The setup-issued token is equally valid.
    assert (
        str(resolve_audience_from_request(_Req(_bearer(setup_token))))
        == registered_id
    )

    assert r_me.status_code == 200
    assert r_me.json()["id"] == registered_id
    assert r_me.json()["email"] == "alice@example.com"


async def test_setup_status_reflects_user_count(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        # Empty deployment: setup is required.
        r_empty = await client.get("/auth/setup-status")
        assert r_empty.status_code == 200
        assert r_empty.json()["setup_required"] is True

        await _setup_first_user(client)

        r_done = await client.get("/auth/setup-status")
        assert r_done.json()["setup_required"] is False


async def test_setup_refuses_once_any_user_exists(db, database_url):
    """The anti-backdoor invariant. Once the deployment is claimed, /auth/setup
    must not create another account -- otherwise anyone hitting the public
    domain could provision a second operator."""
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        await _setup_first_user(client, email="first@example.com")

        r = await client.post(
            "/auth/setup",
            json={"email": "stranger@example.com", "password": "a" * 16},
        )
    assert r.status_code == 409, r.text

    # And no stranger row was written.
    count = await db.pool.fetchval(
        "SELECT count(*) FROM users WHERE lower(email) = $1",
        "stranger@example.com",
    )
    assert count == 0


async def test_concurrent_setup_creates_exactly_one_operator(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        first, second = await asyncio.gather(
            client.post(
                "/auth/setup",
                json={"email": "first@example.com", "password": "a" * 16},
            ),
            client.post(
                "/auth/setup",
                json={"email": "second@example.com", "password": "b" * 16},
            ),
        )

    assert sorted((first.status_code, second.status_code)) == [200, 409]
    rows = await db.pool.fetch("SELECT email, role FROM users")
    assert len(rows) == 1
    assert rows[0]["role"] == "operator"


async def test_register_requires_a_signed_in_operator(db, database_url):
    """Open registration on an internet-reachable domain lets anyone create an
    account that sees its own audience-scoped slice. After setup, /auth/register
    must demand a token; the anonymous call is refused."""
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_first_user(client)

        # Anonymous (no token) -> refused.
        r_anon = await client.post(
            "/auth/register",
            json={"email": "intruder@example.com", "password": "a" * 16},
        )
        assert r_anon.status_code == 401

        # Authenticated operator may add a second user.
        r_authed = await client.post(
            "/auth/register",
            json={"email": "second@example.com", "password": "b" * 16},
            headers=_bearer(token),
        )
        assert r_authed.status_code == 201, r_authed.text
        assert r_authed.json()["role"] == "member"


async def test_member_cannot_register_another_user(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        operator_token = await _setup_first_user(client)
        created = await client.post(
            "/auth/register",
            json={"email": "member@example.com", "password": "b" * 16},
            headers=_bearer(operator_token),
        )
        assert created.status_code == 201, created.text

        login = await client.post(
            "/auth/login",
            json={"email": "member@example.com", "password": "b" * 16},
        )
        member_token = login.json()["token"]
        denied = await client.post(
            "/auth/register",
            json={"email": "third@example.com", "password": "c" * 16},
            headers=_bearer(member_token),
        )

    assert denied.status_code == 403
    assert await db.pool.fetchval(
        "SELECT count(*) FROM users WHERE email = 'third@example.com'"
    ) == 0


async def test_forged_or_tampered_token_resolves_to_none_not_the_named_user(
    db, database_url
):
    """The test the licence model depends on.

    Every forgery vector must resolve to None, never to the user id the token
    claims. The most damning case is last: a token that names the victim and is
    signed with a secret that is not this deployment's.
    """
    from neutron.auth.jwt import create_token

    victim_id = uuid4()

    # A legitimately issued token, as a control -- it must resolve.
    good_token = create_token({"sub": str(victim_id)}, GOOD_SECRET)
    assert resolve_audience_from_request(_Req(_bearer(good_token))) == victim_id

    # 1. Signature tampered: flip the first character of the signature segment.
    #    The first char maps to the high bits of HMAC byte 0, which are always
    #    significant -- unlike the last char, whose low bits are base64 padding
    #    and flipping it leaves the decoded signature bytes identical.
    header, _payload, signature = good_token.split(".")
    mutated_sig_char = "A" if signature[0] != "A" else "B"
    sig_mutated = (
        f"{header}.{_payload}.{mutated_sig_char}{signature[1:]}"
    )
    assert resolve_audience_from_request(_Req(_bearer(sig_mutated))) is None

    # 2. Payload tampered: re-encode the payload to name a different user,
    #    keep the original signature -> the signature no longer matches the
    #    signing input.
    forged_payload = _b64url({"sub": str(uuid4())})
    payload_mutated = f"{header}.{forged_payload}.{signature}"
    assert resolve_audience_from_request(_Req(_bearer(payload_mutated))) is None

    # 3. alg=none: classic confusion attack. decode_token only allows HS256.
    none_token = (
        _b64url({"alg": "none", "typ": "JWT"})
        + "."
        + _b64url({"sub": str(victim_id)})
        + "."
    )
    assert resolve_audience_from_request(_Req(_bearer(none_token))) is None

    # 4. Wrong signer: a well-formed token that names the victim but was signed
    #    with a secret that is not this deployment's. This is the impersonation
    #    attempt; it must not resolve to the named user.
    impostor = create_token({"sub": str(victim_id)}, WRONG_SECRET)
    resolved = resolve_audience_from_request(_Req(_bearer(impostor)))
    assert resolved is None, "a wrongly-signed token resolved to the victim"
    assert resolved != victim_id

    # 5. A valid signature over a payload that carries no subject.
    no_sub = create_token({"role": "admin"}, GOOD_SECRET)
    assert resolve_audience_from_request(_Req(_bearer(no_sub))) is None


async def test_absent_token_resolves_to_none(db, database_url):
    assert resolve_audience_from_request(_Req({})) is None
    # And a non-Bearer scheme is not treated as a token.
    assert (
        resolve_audience_from_request(
            _Req({"authorization": f"Basic {GOOD_SECRET}"})
        )
        is None
    )

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/auth/me")
    assert r.status_code == 401


async def test_wrong_password_and_unknown_email_produce_identical_responses(
    db, database_url
):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        await _setup_first_user(client, email="real@example.com", password="correct-horse-12")

        r_wrong_pw = await client.post(
            "/auth/login",
            json={"email": "real@example.com", "password": "wrong-password-1"},
        )
        r_unknown = await client.post(
            "/auth/login",
            json={"email": "ghost@example.com", "password": "anything-here-1"},
        )

    # Identical means byte-identical: an attacker cannot distinguish the cases
    # by status code, body, or content length.
    assert r_wrong_pw.status_code == 401
    assert r_unknown.status_code == 401
    assert r_wrong_pw.content == r_unknown.content
    assert r_wrong_pw.headers["content-type"] == r_unknown.headers["content-type"]


async def test_password_under_12_characters_is_refused(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.post(
            "/auth/setup",
            json={"email": "short@example.com", "password": "only11chars"},
        )
        assert r.status_code == 400, r.text

    # And no user was written: the refusal is honest, not a soft skip.
    count = await db.pool.fetchval(
        "SELECT count(*) FROM users WHERE lower(email) = $1",
        "short@example.com",
    )
    assert count == 0


async def test_duplicate_email_refused_case_insensitively(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_first_user(
            client, email="User@Example.COM", password="a" * 16
        )

        r_dup = await client.post(
            "/auth/register",
            json={"email": "user@example.com", "password": "b" * 16},
            headers=_bearer(token),
        )
        assert r_dup.status_code == 409, r_dup.text
        assert r_dup.headers["content-type"].startswith("application/problem+json")

    # Exactly one row exists, regardless of the two casings offered.
    count = await db.pool.fetchval(
        "SELECT count(*) FROM users WHERE lower(email) = $1",
        "user@example.com",
    )
    assert count == 1


async def test_inactive_user_cannot_log_in(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r_setup = await client.post(
            "/auth/setup",
            json={"email": "frozen@example.com", "password": "a" * 16},
        )
        assert r_setup.status_code == 200
        user_id = r_setup.json()["user"]["id"]

    await db.pool.execute("UPDATE users SET active = FALSE WHERE id = $1", user_id)

    async with _Lifespan(app), TestClient(app) as client:
        r_login = await client.post(
            "/auth/login",
            json={"email": "frozen@example.com", "password": "a" * 16},
        )
    assert r_login.status_code == 401
    # No token is handed to a deactivated principal.
    assert "token" not in r_login.json()


async def test_token_issued_before_deactivation_is_refused(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_first_user(client, email="frozen@example.com")
        user_id = await db.pool.fetchval(
            "SELECT id FROM users WHERE email = 'frozen@example.com'"
        )
        before = await client.get("/auth/me", headers=_bearer(token))
        assert before.status_code == 200

        await db.pool.execute(
            "UPDATE users SET active = FALSE WHERE id = $1", user_id
        )
        after = await client.get("/auth/me", headers=_bearer(token))

    assert after.status_code == 401


async def test_change_password_rotates_then_new_password_logs_in(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_first_user(client, password="old-password-123")

        r = await client.post(
            "/auth/change-password",
            json={"old_password": "old-password-123", "new_password": "new-password-456"},
            headers=_bearer(token),
        )
        assert r.status_code == 204, r.text

        # Old password no longer works.
        r_old = await client.post(
            "/auth/login",
            json={"email": "op@example.com", "password": "old-password-123"},
        )
        assert r_old.status_code == 401
        # New one does.
        r_new = await client.post(
            "/auth/login",
            json={"email": "op@example.com", "password": "new-password-456"},
        )
        assert r_new.status_code == 200


async def test_change_password_wrong_old_password_is_refused(db, database_url):
    # A stolen token alone must not be enough to lock the operator out: the
    # current password is re-verified. The failure is indistinguishable from a
    # wrong login so the endpoint cannot confirm a password guess.
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_first_user(client, password="real-old-pass-1")

        r = await client.post(
            "/auth/change-password",
            json={"old_password": "wrong-old-pass-1", "new_password": "newpass123456"},
            headers=_bearer(token),
        )
    assert r.status_code == 401


async def test_change_password_weak_new_password_refused(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token = await _setup_first_user(client, password="real-old-pass-1")

        r = await client.post(
            "/auth/change-password",
            json={"old_password": "real-old-pass-1", "new_password": "tooshort"},
            headers=_bearer(token),
        )
    assert r.status_code == 400


async def test_change_password_requires_auth(db, database_url):
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.post(
            "/auth/change-password",
            json={"old_password": "x", "new_password": "y" * 16},
        )
    assert r.status_code == 401
