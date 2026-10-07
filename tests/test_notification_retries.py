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
from collections import Counter
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
        assert first == {
            "delivered": 0, "retried": 1, "failed": 0, "expired": 0, "purged": 0
        }

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
                # The retry is anchored to the row's outcome time (pass-B
                # F4), which trails this driver's `moment` by the pass's own
                # execution time, so advance a second past each boundary
                # rather than exactly onto it.
                moment += (
                    notify.DELIVERY_BACKOFF_BASE * (2**i) + timedelta(seconds=1)
                )
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


class TestNoReplayAfterAbortedBatch:
    """A10: sends no longer happen inside the claiming transaction.

    One aborted pass used to roll back every already-sent row's ``delivered``
    update, and the next pass re-sent the whole batch. Outcomes are now
    committed per row as the batch progresses.
    """

    async def _two_due_rows(self, db):
        user_id = await _user_with_webhook(db)
        alert = _fake_alert(user_id)
        await dispatch(db.pool, alert, [])
        await db.pool.execute(
            "DELETE FROM notification_delivery WHERE channel = 'email'"
        )
        await db.pool.execute(
            """
            INSERT INTO notification_delivery (user_id, alert_id, channel, payload)
            VALUES ($1, $2, 'webhook', '{"kind":"webhook","url":"https://x.example/", "body": {}}')
            """,
            user_id,
            alert["id"],
        )
        await db.pool.execute(
            "UPDATE notification_delivery SET next_attempt_at = now() "
            "WHERE next_attempt_at > now()"
        )
        return user_id

    async def test_a_crash_mid_batch_does_not_resend_the_finished_row(
        self, db, monkeypatch
    ):
        await self._two_due_rows(db)
        sends: list[dict] = []

        async def _send(url, payload):
            sends.append(payload)
            if len(sends) == 2:
                # The second send completes, then the worker dies before
                # writing its outcome (cancellation aborts the loop).
                raise asyncio.CancelledError

        monkeypatch.setattr(notify, "_send_webhook", _send)

        with pytest.raises(asyncio.CancelledError):
            await process_delivery_queue(db.pool)

        states = {
            r["id"]: r["status"]
            for r in await db.pool.fetch("SELECT id, status FROM notification_delivery")
        }
        assert "delivered" in states.values(), (
            "the first row's committed outcome was lost with the batch"
        )

        async def _ok(url, payload):
            sends.append(payload)

        monkeypatch.setattr(notify, "_send_webhook", _ok)
        # Enough time passes for the crashed row's claim lease to lapse.
        await db.pool.execute(
            "UPDATE notification_delivery SET next_attempt_at = now() "
            "WHERE status = 'pending'"
        )
        outcomes = await process_delivery_queue(db.pool)
        assert outcomes["delivered"] == 1, "only the unfinished row is resent"
        assert len(sends) == 3, f"sent {len(sends)} times for two notifications"

    async def test_a_failed_row_does_not_rollback_its_siblings(self, db, monkeypatch):
        await self._two_due_rows(db)

        async def _first_ok_then_broken(url, payload):
            if "x.example" in url:
                raise RuntimeError("webhook gone")

        monkeypatch.setattr(notify, "_send_webhook", _first_ok_then_broken)
        outcomes = await process_delivery_queue(db.pool)
        assert outcomes["delivered"] == 1
        assert outcomes["retried"] == 1

        delivered = await db.pool.fetchval(
            "SELECT count(*) FROM notification_delivery WHERE status = 'delivered'"
        )
        assert delivered == 1, (
            "a sibling's failure rolled back a delivered row's outcome"
        )


class TestConcurrentDeliveryWorkers:
    """Pass-B F1/F4: each row is claimed immediately before its own send,
    and every outcome write is fenced on the claim it answers.

    The old shape claimed the whole batch up front with a lease shorter than
    the batch's worst-case send time, so a second worker re-sent rows the
    first was still sending, and an unconditional outcome write let the
    stale worker flip a delivered row to ``failed``."""

    async def _due_rows(self, db, n: int):
        user_id = await _user_with_webhook(db)
        for i in range(n):
            await db.pool.execute(
                """
                INSERT INTO notification_delivery (user_id, alert_id, channel, payload)
                VALUES ($1, $2, 'webhook', $3::jsonb)
                """,
                user_id,
                uuid4(),
                json.dumps(
                    {
                        "kind": "webhook",
                        "url": f"https://hooks.example.com/h{i}",
                        "body": {},
                    }
                ),
            )
        return user_id

    async def test_two_workers_send_every_row_exactly_once(self, db, monkeypatch):
        await self._due_rows(db, 6)
        sends: list[str] = []

        async def _slow(queued):
            sends.append(queued["url"])
            await asyncio.sleep(0.4)

        monkeypatch.setattr(notify, "_attempt_delivery", _slow)
        # Shorter than a whole batch's send time (6 rows x 0.4s = 2.4s) but
        # longer than a single row's send: exactly the window in which the
        # old batch-wide claim had lapsed mid-pass and a second worker
        # re-sent rows the first was still sending.
        monkeypatch.setattr(notify, "DELIVERY_CLAIM_LEASE", timedelta(seconds=1.2))

        async def worker(delay: float):
            await asyncio.sleep(delay)
            return await process_delivery_queue(db.pool)

        first, second = await asyncio.gather(worker(0.0), worker(1.5))

        assert first["delivered"] + second["delivered"] == 6
        counts = Counter(sends)
        assert len(counts) == 6, f"only {len(counts)} distinct rows were sent"
        assert set(counts.values()) == {1}, (
            f"rows were sent more than once: {counts.most_common(3)}"
        )

    async def test_a_stale_worker_cannot_flip_a_delivered_row_to_failed(
        self, db, monkeypatch
    ):
        user_id = await self._due_rows(db, 1)
        calls = {"n": 0}

        async def _stale_send_fails_late(queued):
            calls["n"] += 1
            if calls["n"] == 1:
                # The stale worker's send outlives its lease, then fails.
                await asyncio.sleep(0.5)
                raise RuntimeError("stale worker error")

        monkeypatch.setattr(notify, "_attempt_delivery", _stale_send_fails_late)
        monkeypatch.setattr(notify, "DELIVERY_CLAIM_LEASE", timedelta(seconds=0.05))

        async def stale_worker():
            return await process_delivery_queue(db.pool, max_attempts=1)

        async def fresh_worker():
            # Wait until the stale worker has claimed the row, then past its
            # (tiny) lease: the fresh worker re-claims, delivers, and writes
            # the winning outcome first.
            while (
                await db.pool.fetchval(
                    "SELECT attempts FROM notification_delivery"
                )
                < 1
            ):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            return await process_delivery_queue(db.pool, max_attempts=1)

        stale, fresh = await asyncio.gather(stale_worker(), fresh_worker())

        row = await db.pool.fetchrow(
            "SELECT status, attempts, last_error, delivered_at "
            "FROM notification_delivery"
        )
        assert row["status"] == "delivered", (
            "a delivered row was flipped by a stale worker's late outcome"
        )
        assert row["attempts"] == 2
        assert row["delivered_at"] is not None
        assert stale == {
            "delivered": 0, "retried": 0, "failed": 0, "expired": 0, "purged": 0
        }, "the stale worker counted a row it no longer owned"
        assert fresh["delivered"] == 1
        assert await delivery_status(db.pool, user_id) == {
            "pending": 0, "failed": 0, "delivered": 1,
        }

    async def test_retry_backoff_is_anchored_to_the_outcome_not_pass_start(
        self, db, monkeypatch
    ):
        await self._due_rows(db, 1)
        finished_send: list[datetime] = []

        async def _slow_failure(queued):
            await asyncio.sleep(1.5)
            finished_send.append(datetime.now(UTC))
            raise RuntimeError("slow transient failure")

        monkeypatch.setattr(notify, "_attempt_delivery", _slow_failure)
        monkeypatch.setattr(notify, "DELIVERY_BACKOFF_BASE", timedelta(seconds=1.0))

        start = datetime.now(UTC)
        outcomes = await process_delivery_queue(db.pool, now=start)
        assert outcomes["retried"] == 1

        row = await db.pool.fetchrow(
            "SELECT next_attempt_at FROM notification_delivery"
        )
        # The backoff clock starts when the outcome is written, not when the
        # pass began: a row whose own send consumed the whole backoff window
        # must not come back due immediately.
        assert row["next_attempt_at"] >= finished_send[0]
        assert row["next_attempt_at"] - finished_send[0] >= timedelta(seconds=0.9)

    async def test_each_row_outcome_reports_progress(self, db, monkeypatch):
        await self._due_rows(db, 3)
        progress: list[int] = []

        async def _ok(queued):
            pass

        monkeypatch.setattr(notify, "_attempt_delivery", _ok)
        outcomes = await process_delivery_queue(
            db.pool, on_progress=lambda: progress.append(1)
        )
        assert outcomes["delivered"] == 3
        assert len(progress) == 3

    async def test_the_delivery_loop_touches_its_heartbeat_per_row(
        self, db, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("OMNI_SCHEDULER_HEARTBEAT", str(tmp_path / "hb"))
        from omni.scheduler import heartbeat
        from omni.scheduler.worker import drain_delivery_queue_once

        await self._due_rows(db, 2)

        async def _ok(queued):
            pass

        monkeypatch.setattr(notify, "_attempt_delivery", _ok)
        delivered = await drain_delivery_queue_once(db.pool)
        assert delivered == 2
        age = heartbeat.heartbeat_age_seconds("notification_delivery")
        assert age is not None and age < 5.0, (
            "a slow-but-working delivery pass read as never having progressed"
        )


class TestRetention:
    """A11: terminal rows carry destinations and payloads; they do not
    accumulate forever."""

    async def test_old_terminal_rows_are_purged(self, db, monkeypatch):
        user_id = await _user_with_webhook(db)
        await dispatch(db.pool, _fake_alert(user_id), [])
        await db.pool.execute(
            "UPDATE notification_delivery SET status = 'delivered', "
            "delivered_at = now() - interval '8 days', "
            "created_at = now() - interval '8 days'"
        )
        await db.pool.execute(
            "INSERT INTO notification_delivery "
            "(user_id, alert_id, channel, payload, status, created_at) VALUES "
            "($1, $2, 'webhook', '{}', 'failed', now() - interval '9 days')",
            user_id,
            uuid4(),
        )
        await db.pool.execute(
            "INSERT INTO notification_delivery "
            "(user_id, alert_id, channel, payload, status, created_at) VALUES "
            "($1, $2, 'webhook', '{}', 'delivered', now() - interval '1 hour')",
            user_id,
            uuid4(),
        )

        async def _no_send(url, payload):
            raise AssertionError("retention purge attempted a send")

        monkeypatch.setattr(notify, "_send_webhook", _no_send)
        outcomes = await process_delivery_queue(db.pool)
        assert outcomes["purged"] == 3
        remaining = await db.pool.fetchval(
            "SELECT count(*) FROM notification_delivery"
        )
        assert remaining == 1, "recent terminal rows must survive the purge"

    async def test_pending_rows_are_never_purged_by_retention(self, db, monkeypatch):
        user_id = await _user_with_webhook(db)
        await dispatch(db.pool, _fake_alert(user_id), [])
        await db.pool.execute(
            "UPDATE notification_delivery SET created_at = now() - interval '30 days'"
        )
        async def _no_send(url, payload):
            raise AssertionError("an expired row was attempted")

        monkeypatch.setattr(notify, "_send_webhook", _no_send)
        outcomes = await process_delivery_queue(db.pool)
        assert outcomes["expired"] == 2
        # Expired-to-failed this pass; a later pass purges them by age.


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
