"""Notification delivery queue: enqueue, bounded retry, cap, expiry (audit
finding 9).

A transient five-second outage used to permanently lose the notification.
Delivery is now a durable row per channel: failures retry with exponential
backoff, exhaust into a visible failed state, and never dump hours later
because the delivery window expires them.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.alerts import notify
from omni.alerts.notify import delivery_status, dispatch, process_delivery_queue

WEBHOOK = "https://hooks.example.com/d/abc"
SECRET = "n" * 48


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


def _fake_alert(user_id):
    return {
        "id": uuid4(),
        "user_id": user_id,
        "entity_id": uuid4(),
        "claim_type": "price.close",
        "condition": {"kind": "value_above", "field": "price", "threshold": 100},
    }


async def _user_with_webhook(db) -> str:
    user_id = uuid4()
    await db.pool.execute(
        "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'x')",
        user_id,
        f"queue-{uuid4().hex[:8]}@example.com",
    )
    await db.pool.execute(
        """
        INSERT INTO user_settings (user_id, data)
        VALUES ($1, jsonb_build_object('notify', jsonb_build_object(
            'webhook_url', $2::text, 'email', 'person@example.com'
        )))
        """,
        user_id,
        WEBHOOK,
    )
    return user_id


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE notification_delivery")
    await db.pool.execute("TRUNCATE user_settings CASCADE")
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


class TestEnqueue:
    async def test_dispatch_enqueues_one_row_per_channel_instead_of_sending(
        self, db, monkeypatch
    ):
        def _no_network(*a, **kw):
            raise AssertionError("dispatch attempted network I/O inline")

        monkeypatch.setattr(notify, "_send_webhook", _no_network)
        user_id = await _user_with_webhook(db)

        await dispatch(db.pool, _fake_alert(user_id), [])

        rows = await db.pool.fetch(
            "SELECT channel, status, attempts FROM notification_delivery "
            "WHERE user_id = $1 ORDER BY channel",
            user_id,
        )
        assert [(r["channel"], r["status"], r["attempts"]) for r in rows] == [
            ("email", "pending", 0),
            ("webhook", "pending", 0),
        ]

    async def test_no_channels_configured_enqueues_nothing(self, db):
        user_id = uuid4()
        await db.pool.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'x')",
            user_id,
            f"bare-{uuid4().hex[:8]}@example.com",
        )
        await dispatch(db.pool, _fake_alert(user_id), [])
        count = await db.pool.fetchval("SELECT count(*) FROM notification_delivery")
        assert count == 0

    async def test_queued_payload_carries_everything_a_retry_needs(self, db):
        user_id = await _user_with_webhook(db)
        firings = [
            {
                "id": uuid4(),
                "event_date": None,
                "knowledge_date": None,
                "value": 101.5,
                "source": "test",
            }
        ]
        await dispatch(db.pool, _fake_alert(user_id), firings)
        row = await db.pool.fetchrow(
            "SELECT payload FROM notification_delivery WHERE channel = 'webhook'"
        )
        queued = row["payload"]
        if isinstance(queued, str):
            queued = json.loads(queued)
        assert queued["kind"] == "webhook"
        assert queued["url"] == WEBHOOK
        assert queued["body"]["firings"][0]["value"] == 101.5


class TestRetryLifecycle:
    async def test_failed_delivery_is_retried_with_backoff_then_succeeds(
        self, db, monkeypatch
    ):
        user_id = await _user_with_webhook(db)
        await dispatch(db.pool, _fake_alert(user_id), [])
        await db.pool.execute(
            "DELETE FROM notification_delivery WHERE channel = 'email'"
        )

        attempts: list[str] = []

        async def _flaky(url, payload):
            attempts.append(url)
            if len(attempts) < 3:
                raise RuntimeError("transient outage")

        monkeypatch.setattr(notify, "_send_webhook", _flaky)

        start = datetime.now(UTC)
        first = await process_delivery_queue(db.pool, now=start)
        assert first == {"delivered": 0, "retried": 1, "failed": 0, "expired": 0}

        row = await db.pool.fetchrow(
            "SELECT status, attempts, next_attempt_at, last_error "
            "FROM notification_delivery"
        )
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert "transient outage" in row["last_error"]
        assert row["next_attempt_at"] > start

        # Not due yet: an immediate pass must leave it alone (no 3am dump --
        # and no tight retry loop either).
        early = await process_delivery_queue(db.pool, now=start + timedelta(seconds=30))
        assert early["delivered"] + early["retried"] + early["failed"] == 0

        # Due after the backoff, fails again, doubles the wait.
        second = await process_delivery_queue(
            db.pool, now=start + timedelta(seconds=61)
        )
        assert second["retried"] == 1

        # Third attempt delivers.
        async def _ok(url, payload):
            pass

        monkeypatch.setattr(notify, "_send_webhook", _ok)
        third = await process_delivery_queue(
            db.pool, now=start + timedelta(seconds=61 + 121)
        )
        assert third["delivered"] == 1

        row = await db.pool.fetchrow(
            "SELECT status, attempts, last_error, delivered_at "
            "FROM notification_delivery"
        )
        assert row["status"] == "delivered"
        assert row["attempts"] == 3
        assert row["last_error"] is None
        assert row["delivered_at"] is not None

    async def test_exhausted_deliveries_end_failed_not_pending(self, db, monkeypatch):
        user_id = await _user_with_webhook(db)
        await dispatch(db.pool, _fake_alert(user_id), [])
        await db.pool.execute(
            "DELETE FROM notification_delivery WHERE channel = 'email'"
        )

        async def _dead(url, payload):
            raise RuntimeError("webhook is gone")

        monkeypatch.setattr(notify, "_send_webhook", _dead)

        moment = datetime.now(UTC)
        for i in range(notify.MAX_DELIVERY_ATTEMPTS):
            outcomes = await process_delivery_queue(db.pool, now=moment)
            if i < notify.MAX_DELIVERY_ATTEMPTS - 1:
                assert outcomes["retried"] == 1
                moment += notify.DELIVERY_BACKOFF_BASE * (2**i)
            else:
                assert outcomes["failed"] == 1

        row = await db.pool.fetchrow(
            "SELECT status, attempts FROM notification_delivery"
        )
        assert row["status"] == "failed"
        assert row["attempts"] == notify.MAX_DELIVERY_ATTEMPTS

    async def test_old_pending_rows_expire_without_an_attempt(self, db, monkeypatch):
        user_id = await _user_with_webhook(db)
        await dispatch(db.pool, _fake_alert(user_id), [])
        await db.pool.execute(
            "DELETE FROM notification_delivery WHERE channel = 'email'"
        )
        await db.pool.execute(
            "UPDATE notification_delivery SET created_at = now() - interval '25 hours'"
        )

        async def _no_send(url, payload):
            raise AssertionError("an expired row was still attempted")

        monkeypatch.setattr(notify, "_send_webhook", _no_send)
        outcomes = await process_delivery_queue(db.pool)
        assert outcomes["expired"] == 1
        assert outcomes["delivered"] + outcomes["retried"] + outcomes["failed"] == 0

        row = await db.pool.fetchrow(
            "SELECT status, last_error FROM notification_delivery"
        )
        assert row["status"] == "failed"
        assert "expired" in row["last_error"]

    async def test_email_channel_delivers_through_the_queue(self, db, monkeypatch):
        user_id = await _user_with_webhook(db)
        await dispatch(db.pool, _fake_alert(user_id), [])
        await db.pool.execute(
            "DELETE FROM notification_delivery WHERE channel = 'webhook'"
        )

        sent: list[tuple] = []

        def _fake_smtp(host, port, timeout=None):
            return _FakeSMTP(sent)

        monkeypatch.setattr(notify.smtplib, "SMTP", _fake_smtp)
        monkeypatch.setattr(notify.settings, "smtp_host", "relay.example")
        monkeypatch.setattr(notify.settings, "smtp_user", "")

        outcomes = await process_delivery_queue(db.pool)
        assert outcomes["delivered"] == 1
        assert sent and sent[0][0] == "person@example.com"

    async def test_status_counts_are_visible_per_user(self, db):
        user_id = await _user_with_webhook(db)
        await db.pool.execute(
            "INSERT INTO notification_delivery "
            "(user_id, alert_id, channel, payload, status) VALUES "
            "($1, $2, 'webhook', '{}', 'pending'), "
            "($1, $2, 'webhook', '{}', 'failed'), "
            "($1, $2, 'webhook', '{}', 'delivered')",
            user_id,
            uuid4(),
        )
        assert await delivery_status(db.pool, user_id) == {
            "pending": 1,
            "failed": 1,
            "delivered": 1,
        }


class _FakeSMTP:
    def __init__(self, sent):
        self._sent = sent
        self._message = None

    def __enter__(self):
        import ssl

        self.starttls(context=ssl.create_default_context())
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, *, context=None):
        pass

    def ehlo(self):
        pass

    def send_message(self, msg):
        self._sent.append((msg["To"], msg["Subject"]))
