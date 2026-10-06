"""Notification configuration is validated before storage (audit finding 8).

Webhook safety used to be checked only at delivery time, so a private or
non-https target could sit saved in settings for weeks and fail exactly when
it was needed. The mutation that introduces an invalid channel must be the
request that hears about it.
"""

from __future__ import annotations

import asyncio

import pytest
from neutron.test import TestClient

from omni.main import create_app

SECRET = "s" * 48


class _Lifespan:
    def __init__(self, app):
        self.app = app
        self.receive = asyncio.Queue()
        self.send = asyncio.Queue()

    async def __aenter__(self):
        self.task = asyncio.create_task(
            self.app({"type": "lifespan"}, self.receive.get, self.send.put)
        )
        await self.receive.put({"type": "lifespan.startup"})
        assert (await self.send.get())["type"] == "lifespan.startup.complete"
        return self.app

    async def __aexit__(self, *exc):
        await self.receive.put({"type": "lifespan.shutdown"})
        await self.send.get()
        await self.task


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("OMNI_JWT_SECRET", SECRET)
    yield


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE notification_delivery")
    await db.pool.execute("TRUNCATE user_settings CASCADE")
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


async def _authed_client(database_url):
    app = create_app(database_url)
    lifespan = _Lifespan(app)
    await lifespan.__aenter__()
    client_cm = TestClient(app)
    client = await client_cm.__aenter__()
    try:
        r = await client.post(
            "/auth/setup",
            json={"email": "operator@example.com", "password": "a" * 16},
        )
        assert r.status_code == 200, r.text
        headers = {"authorization": f"Bearer {r.json()['token']}"}
    except BaseException:
        await client_cm.__aexit__(None, None, None)
        await lifespan.__aexit__(None, None, None)
        raise
    return client, client_cm, lifespan, headers


@pytest.mark.parametrize(
    "webhook",
    [
        "http://hooks.example.com/x",
        "https://127.0.0.1/x",
        "https://10.0.0.5/x",
        "https://hooks.example.com:8443/x",
        "https://user:pw@hooks.example.com/x",
        "not a url at all",
    ],
)
async def test_invalid_webhooks_are_refused_at_save_time(webhook, database_url):
    client, cm, lifespan, headers = await _authed_client(database_url)
    try:
        r = await client.put(
            "/settings/notifications",
            json={"webhook_url": webhook},
            headers=headers,
        )
        assert r.status_code == 400, r.text
    finally:
        await cm.__aexit__(None, None, None)
        await lifespan.__aexit__(None, None, None)


@pytest.mark.parametrize(
    "email", ["not-an-email", "missing@tld", "a b@example.com", ""]
)
async def test_malformed_emails_are_refused_at_save_time(email, database_url):
    client, cm, lifespan, headers = await _authed_client(database_url)
    try:
        r = await client.put(
            "/settings/notifications",
            json={"email": email},
            headers=headers,
        )
        if email == "":
            # Empty string means "clear the channel", not an invalid value.
            assert r.status_code == 200
        else:
            assert r.status_code == 400, r.text
    finally:
        await cm.__aexit__(None, None, None)
        await lifespan.__aexit__(None, None, None)


async def test_valid_configuration_persists_and_reports_delivery_state(
    database_url,
):
    client, cm, lifespan, headers = await _authed_client(database_url)
    try:
        r = await client.put(
            "/settings/notifications",
            json={"webhook_url": "https://hooks.example.com/x"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["webhook_configured"] is True
        assert r.json()["delivery"] == {"pending": 0, "failed": 0, "delivered": 0}
    finally:
        await cm.__aexit__(None, None, None)
        await lifespan.__aexit__(None, None, None)


async def test_invalid_value_is_not_partially_stored(database_url, db):
    client, cm, lifespan, headers = await _authed_client(database_url)
    try:
        await client.put(
            "/settings/notifications",
            json={"webhook_url": "https://hooks.example.com/x"},
            headers=headers,
        )
        refused = await client.put(
            "/settings/notifications",
            json={"webhook_url": "https://127.0.0.1/x"},
            headers=headers,
        )
        assert refused.status_code == 400
    finally:
        await cm.__aexit__(None, None, None)
        await lifespan.__aexit__(None, None, None)

    stored = await db.pool.fetchval(
        "SELECT data->'notify'->>'webhook_url' FROM user_settings"
    )
    assert stored == "https://hooks.example.com/x", (
        "a refused save mutated the stored configuration"
    )
