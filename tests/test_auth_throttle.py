"""Durable login throttling and proxy-aware client resolution (audit findings 1).

The in-memory limiter answers "five a minute per IP, per process". These
tests pin the half that actually holds a brute force: the PostgreSQL streak
keyed by (email fingerprint, client IP) that every replica shares, with
cooldowns that escalate instead of resetting, plus the rule that forwarded
headers are honoured exactly when the socket peer is a configured proxy --
so the fix for shared-bucket throttling does not become a spoofing hole.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from neutron.test import TestClient

from omni.auth.forwarded import resolve_client_ip
from omni.auth.throttle import (
    check_login_throttle,
    cooldown_for,
    email_fingerprint,
    normalize_ip,
    record_login_failure,
    record_login_success,
)
from omni.main import create_app

SECRET = "t" * 48


class _Lifespan:
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


def _bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


class TestClientWithPeer:
    """httpx client bound to the app with a chosen socket peer address.

    neutron's TestClient fixes the ASGI peer at 127.0.0.1; the forwarded-IP
    tests need to speak as other peers.
    """

    __test__ = False  # not a test class, despite the name

    def __init__(self, app, peer: tuple[str, int]):
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=peer),
            base_url="http://test",
        )

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *exc):
        await self._client.aclose()


class TestIpNormalization:
    def test_ipv6_equivalents_share_one_key(self):
        assert normalize_ip("::1") == normalize_ip("0:0:0:0:0:0:0:1")
        assert normalize_ip("[::1]") == normalize_ip("::1")
        assert normalize_ip("2001:0db8:0000:0000:0000:0000:0000:0001") == (
            normalize_ip("2001:db8::1")
        )

    def test_non_ip_strings_pass_through_unchanged(self):
        # The ASGI peer can be a name ("testclient"); it must not be mangled
        # into something that silently merges distinct callers.
        assert normalize_ip("testclient") == "testclient"


class TestForwardedResolution:
    def test_untrusted_peer_makes_forwarded_for_ignored(self):
        # A direct client naming somebody else in X-Forwarded-For must not
        # shift the blame: the socket peer is the answer.
        assert (
            resolve_client_ip(
                {"x-forwarded-for": "1.2.3.4"}, "203.0.113.9", "10.0.0.0/8"
            )
            == "203.0.113.9"
        )

    def test_trusted_peer_honours_forwarded_for(self):
        assert (
            resolve_client_ip(
                {"x-forwarded-for": "198.51.100.7"}, "10.0.0.5", "10.0.0.0/8"
            )
            == "198.51.100.7"
        )

    def test_rightmost_untrusted_entry_wins_in_a_chain(self):
        # Each proxy appends the address it saw; everything left of the
        # rightmost untrusted entry was supplied by an untrusted hop and may
        # be forged.
        assert (
            resolve_client_ip(
                {"x-forwarded-for": "6.6.6.6, 198.51.100.7, 10.0.0.5"},
                "10.0.0.9",
                "10.0.0.0/8",
            )
            == "198.51.100.7"
        )

    def test_all_proxy_chain_falls_back_to_leftmost(self):
        assert (
            resolve_client_ip(
                {"x-forwarded-for": "10.0.0.1, 10.0.0.2"}, "10.0.0.9", "10.0.0.0/8"
            )
            == "10.0.0.1"
        )

    def test_no_configured_proxies_means_peer_always(self):
        assert (
            resolve_client_ip({"x-forwarded-for": "1.2.3.4"}, "10.0.0.5", "")
            == "10.0.0.5"
        )

    def test_malformed_proxy_spec_is_loud(self):
        with pytest.raises(ValueError):
            resolve_client_ip({}, "10.0.0.5", "not-an-address")


class TestEscalation:
    def test_cooldown_doubles_past_the_threshold(self):
        assert cooldown_for(4) == cooldown_for(0)
        assert cooldown_for(5).total_seconds() == 60
        assert cooldown_for(6).total_seconds() == 120
        assert cooldown_for(7).total_seconds() == 240

    def test_cooldown_is_capped(self):
        assert cooldown_for(30).total_seconds() == 3600

    async def test_five_failures_lock_the_key_then_age_out(self, db):
        email = f"streak-{uuid4().hex[:8]}@example.com"
        for _ in range(5):
            admitted, _ = await check_login_throttle(
                db.pool, email=email, ip="203.0.113.10"
            )
            assert admitted
            await record_login_failure(db.pool, email=email, ip="203.0.113.10")

        refused, retry_after = await check_login_throttle(
            db.pool, email=email, ip="203.0.113.10"
        )
        assert not refused
        assert 0 < retry_after <= 60

        # Age the streak past the base cooldown: backdate the last failure,
        # and the same history admits again -- the lockout is time-based, not
        # permanent.
        await db.pool.execute(
            "UPDATE auth_throttle_event SET at = at - interval '61 seconds' "
            "WHERE email_hash = $1 AND client_ip = '203.0.113.10'",
            email_fingerprint(email),
        )
        admitted, _ = await check_login_throttle(
            db.pool, email=email, ip="203.0.113.10"
        )
        assert admitted

    async def test_one_more_failure_after_lockout_escalates(self, db):
        email = f"esc-{uuid4().hex[:8]}@example.com"
        for _ in range(5):
            await record_login_failure(db.pool, email=email, ip="203.0.113.11")
        await db.pool.execute(
            "UPDATE auth_throttle_event SET at = at - interval '61 seconds' "
            "WHERE email_hash = $1 AND client_ip = '203.0.113.11'",
            email_fingerprint(email),
        )
        # Admitted after the base cooldown, and the failure doubles it.
        admitted, retry_after = await check_login_throttle(
            db.pool, email=email, ip="203.0.113.11"
        )
        assert admitted
        await record_login_failure(db.pool, email=email, ip="203.0.113.11")
        _, retry_after = await check_login_throttle(
            db.pool, email=email, ip="203.0.113.11"
        )
        assert 60 < retry_after <= 120

    async def test_success_clears_the_streak(self, db):
        email = f"ok-{uuid4().hex[:8]}@example.com"
        for _ in range(4):
            await record_login_failure(db.pool, email=email, ip="203.0.113.12")
        await record_login_success(db.pool, email=email, ip="203.0.113.12")
        admitted, _ = await check_login_throttle(
            db.pool, email=email, ip="203.0.113.12"
        )
        assert admitted
        remaining = await db.pool.fetchval(
            "SELECT count(*) FROM auth_throttle_event WHERE client_ip = '203.0.113.12'"
        )
        assert remaining == 0

    async def test_email_is_stored_only_as_a_fingerprint(self, db):
        email = f"private-{uuid4().hex[:8]}@example.com"
        await record_login_failure(db.pool, email=email, ip="203.0.113.13")
        fingerprinted = await db.pool.fetchval(
            "SELECT count(*) FROM auth_throttle_event WHERE email_hash = $1",
            email_fingerprint(email),
        )
        plaintext = await db.pool.fetchval(
            "SELECT count(*) FROM auth_throttle_event WHERE email_hash LIKE '%@%'"
        )
        assert fingerprinted == 1
        assert plaintext == 0


async def _login(client, email: str, password: str, headers: dict | None = None):
    return await client.post(
        "/auth/login", json={"email": email, "password": password}, headers=headers
    )


class TestEndpointThrottling:
    @pytest.fixture(autouse=True)
    async def _clean(self, db):
        await db.pool.execute("TRUNCATE users CASCADE")
        await db.pool.execute("TRUNCATE auth_throttle_event")
        yield

    async def test_two_app_instances_share_one_throttle(self, db, database_url, monkeypatch):
        # Two app objects = two processes' worth of in-memory state. The
        # database streak must be the shared authority: failures counted
        # through instance A refuse instance B.
        monkeypatch.setenv("OMNI_JWT_SECRET", SECRET)
        app_a = create_app(database_url)
        app_b = create_app(database_url)
        async with _Lifespan(app_a), _Lifespan(app_b), TestClient(app_a) as a, TestClient(app_b) as b:
            await a.post(
                "/auth/setup",
                json={"email": "shared@example.com", "password": "a" * 16},
            )
            from omni.auth.ratelimit import reset_for_test

            reset_for_test()
            for _ in range(5):
                r = await _login(a, "shared@example.com", "wrong-password-xx")
                assert r.status_code == 401, r.text
            reset_for_test()
            r_b = await _login(b, "shared@example.com", "another-wrong-1")
            assert r_b.status_code == 429, (
                "a second replica saw a fresh in-memory limiter; the database "
                "streak must refuse it"
            )

    async def test_forwarded_ip_from_trusted_proxy_gets_its_own_bucket(
        self, db, database_url, monkeypatch
    ):
        monkeypatch.setenv("OMNI_JWT_SECRET", SECRET)
        monkeypatch.setattr(
            "omni.config.settings.omni_trusted_proxies", "127.0.0.1"
        )
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            await client.post(
                "/auth/setup",
                json={"email": "proxied@example.com", "password": "a" * 16},
            )
            from omni.auth.ratelimit import reset_for_test

            reset_for_test()
            # Fail five times "from" 198.51.100.50 behind the trusted proxy.
            for _ in range(5):
                r = await client.post(
                    "/auth/login",
                    json={"email": "proxied@example.com", "password": "wrong-wrong-1"},
                    headers={"x-forwarded-for": "198.51.100.50"},
                )
                assert r.status_code == 401, r.text
            r_same = await client.post(
                "/auth/login",
                json={"email": "proxied@example.com", "password": "wrong-wrong-1"},
                headers={"x-forwarded-for": "198.51.100.50"},
            )
            assert r_same.status_code == 429
            # A different client behind the same proxy is a different key.
            reset_for_test()
            r_other = await client.post(
                "/auth/login",
                json={"email": "proxied@example.com", "password": "wrong-wrong-1"},
                headers={"x-forwarded-for": "198.51.100.51"},
            )
            assert r_other.status_code == 401

    async def test_direct_client_cannot_spoof_forwarded_for(
        self, db, database_url, monkeypatch
    ):
        # No trusted proxies configured: the peer (127.0.0.1 under the test
        # transport) is the bucket regardless of the header.
        monkeypatch.setenv("OMNI_JWT_SECRET", SECRET)
        app = create_app(database_url)
        async with _Lifespan(app), TestClientWithPeer(
            app, ("203.0.113.99", 40000)
        ) as client:
            await client.post(
                "/auth/setup",
                json={"email": "spoofer@example.com", "password": "a" * 16},
            )
            from omni.auth.ratelimit import reset_for_test

            reset_for_test()
            for _ in range(5):
                r = await client.post(
                    "/auth/login",
                    json={"email": "spoofer@example.com", "password": "wrong-wrong-1"},
                    headers={"x-forwarded-for": f"1.2.3.{_}"},
                )
                assert r.status_code == 401, r.text
            reset_for_test()
            r = await client.post(
                "/auth/login",
                json={"email": "spoofer@example.com", "password": "wrong-wrong-1"},
                headers={"x-forwarded-for": "9.9.9.9"},
            )
            assert r.status_code == 429, (
                "a fresh spoofed XFF must not buy a fresh bucket"
            )
