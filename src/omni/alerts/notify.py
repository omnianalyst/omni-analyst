"""Delivery for alert firings: webhook and email, through a durable queue.

A firing nobody hears about is a row in a table. This module is the second
half of alerting -- getting the event to the person who asked for it -- with
two channels, each opt-in per user:

  * webhook: a single JSON POST per firing batch, to a URL the user set.
    Whatever sits at the other end (a bridge script, ntfy, a Discord hook) is
    the user's business; Omni only promises the payload shape.
  * email: plain text, through the deployment's SMTP configuration. The
    address is the user's; the relay is the operator's.

Failure discipline: ``dispatch`` ENQUEUES one row per channel and never
touches the network -- a dead webhook cannot stop the next alert from being
evaluated, and a five-second outage no longer permanently loses the
notification. ``process_delivery_queue`` (the scheduler's delivery loop)
retries pending rows with bounded exponential backoff, marks rows failed
after max attempts, and expires rows older than the delivery TTL so a retry
dump hours after the event cannot happen. The firing record remains the
source of truth; the queue is the delivery status, visible per user as
pending/failed/delivered counts.

Configuration lives in user_settings.data.notify: {"webhook_url": ...,
"email": ...}. Per-user, because alerts are per-user; SMTP is per-deployment
because the relay is infrastructure.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import smtplib
import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

from omni.config import settings

logger = logging.getLogger("omni.alerts.notify")

_NOTIFY_SETTINGS = "SELECT data FROM user_settings WHERE user_id = $1"

MAX_DELIVERY_ATTEMPTS = 5
DELIVERY_BACKOFF_BASE = timedelta(seconds=60.0)
DELIVERY_TTL = timedelta(hours=24.0)
DELIVERY_BATCH = 20
#: How long a claimed row is invisible to other workers. Each row is claimed
#: immediately before its own send, so this covers ONE bounded send, not a
#: batch: a webhook is the aiohttp total timeout (5s), while SMTP is
#: timeout=10s per socket operation across a whole session (connect, ehlo,
#: starttls, ehlo, login, send, quit -- on the order of a dozen operations).
#: After the lease lapses a crashed worker's row becomes due again and is
#: retried -- at-least-once, not at-most-once.
DELIVERY_CLAIM_LEASE = timedelta(seconds=120.0)
#: Terminal rows (delivered/failed) are queue status, not an archive: the
#: payload carries the destination (a webhook URL can embed a secret token)
#: and the message body, so holding them forever is holding plaintext
#: destinations indefinitely (audit A11). A delivery window is 24h, so a
#: row is terminal within ~24h of creation; anything still around a week
#: later is history nobody can act on, and maintenance deletes it.
DELIVERY_RETENTION = timedelta(days=7.0)


def _destination_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for global unicast addresses a webhook may be sent to.

    The embedded-address forms are unwrapped or rejected explicitly because
    their parent /8 or /32 prefixes read as global: an IPv4-mapped address is
    judged by the IPv4 it carries, and 6to4/teredo are refused outright --
    their embedded payload is attacker-chosen and not worth unwinding.
    """
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return _destination_allowed(ip.ipv4_mapped)
        if ip.sixtofour is not None or ip.teredo is not None:
            return False
    return (
        ip.is_global
        and not ip.is_private
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_unspecified
    )


def _validated_webhook_url(url: str) -> str:
    """Accept only an https URL on port 443 whose host is a public destination.

    The webhook target is member-supplied input that the deployment's API
    process will make a network request to, so it is treated as an SSRF
    vector, not as a convenience: no userinfo, no fragment, no redirects, no
    proxy env, and every address the host resolves to must be global unicast
    before a connection is opened.

    ``urlsplit`` defers parsing the port until ``.port`` is touched, and a
    non-numeric or out-of-range port makes THAT access raise ValueError --
    which used to escape this function as a 500 on save and an unclassified
    error per delivery attempt (audit A15). The port is read inside the
    guard so a bad port is an ordinary refused URL.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise RuntimeError(f"webhook url is not parseable: {exc}") from exc
    if parts.scheme != "https":
        raise RuntimeError(f"webhook url must be https, got scheme {parts.scheme!r}")
    if parts.username is not None or parts.password is not None:
        raise RuntimeError("webhook url must not carry credentials in the host")
    if parts.fragment:
        raise RuntimeError("webhook url must not carry a fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise RuntimeError(f"webhook url port is invalid: {exc}") from exc
    if port is not None and port != 443:
        raise RuntimeError(f"webhook url must use port 443, got {port}")
    if not parts.hostname:
        raise RuntimeError("webhook url has no host")
    try:
        literal = ipaddress.ip_address(parts.hostname.strip("[]"))
    except ValueError:
        return url
    if not _destination_allowed(literal):
        raise RuntimeError(
            f"webhook host {parts.hostname} is not a public destination"
        )
    return url


async def _post_webhook(session, url: str, payload: dict) -> None:
    """POST once, never follow a redirect, and raise on anything but 2xx.

    Extracted from `_send_webhook` so the status contract is testable without
    a network: a delivery that the endpoint did not accept is a failure, not
    a logged footnote.
    """
    async with session.post(
        url,
        json=payload,
        headers={"content-type": "application/json"},
        allow_redirects=False,
    ) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"alert webhook returned HTTP {response.status}")


async def _send_webhook(url: str, payload: dict) -> None:
    import aiohttp

    url = _validated_webhook_url(url)

    class _PublicOnlyResolver(aiohttp.abc.AbstractResolver):
        """Resolves through the default resolver and keeps public answers only.

        The connector connects to exactly the addresses returned here, so a
        hostname that answers with any non-public address is refused before a
        socket is opened, and DNS rebinding between check and connect has no
        window: there is only one resolution.
        """

        def __init__(self) -> None:
            self._inner = aiohttp.DefaultResolver()

        async def resolve(self, host, port=0, family=0):
            answers = await self._inner.resolve(host, port, family)
            allowed = []
            for answer in answers:
                ip = ipaddress.ip_address(answer["host"])
                if not _destination_allowed(ip):
                    raise RuntimeError(
                        f"webhook host {host} resolves to the non-public "
                        f"address {answer['host']}"
                    )
                allowed.append(answer)
            return allowed

        async def close(self) -> None:
            await self._inner.close()

    connector = aiohttp.TCPConnector(
        resolver=_PublicOnlyResolver(),
        use_dns_cache=False,
        ssl=ssl.create_default_context(),
    )
    timeout = aiohttp.ClientTimeout(total=5.0)
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, trust_env=False
    ) as session:
        await _post_webhook(session, url, payload)


def _describe_condition(condition) -> str:
    """One human line for the alert's condition, for message subjects."""
    try:
        if isinstance(condition, str):
            condition = json.loads(condition)
        kind = condition.get("kind")
        if kind == "value_above":
            return f"{condition.get('field', 'value')} rises above {condition.get('threshold')}"
        if kind == "value_below":
            return f"{condition.get('field', 'value')} falls below {condition.get('threshold')}"
        if kind == "pct_change_above":
            return (f"up {condition.get('pct')}% over "
                    f"{condition.get('window_days')} days")
        if kind == "pct_change_below":
            return (f"down {condition.get('pct')}% over "
                    f"{condition.get('window_days')} days")
        if kind == "staleness_exceeds":
            return f"data older than {condition.get('seconds')}s"
        if kind == "contradiction":
            return "sources disagree"
    except Exception:  # noqa: BLE001, S110 - a subject line never justifies a raise
        pass
    return "condition met"


def _payload(alert, firings: list, entity_symbol: str | None) -> dict[str, Any]:
    condition = alert["condition"]
    if isinstance(condition, str):
        try:
            condition = json.loads(condition)
        except (ValueError, TypeError):
            pass
    return {
        "alert_id": str(alert["id"]),
        "entity_id": str(alert["entity_id"]),
        "entity_symbol": entity_symbol,
        "claim_type": str(alert["claim_type"]),
        "condition": condition,
        "condition_line": _describe_condition(condition),
        "firings": [
            {
                "claim_id": str(c["id"]),
                "event_date": c["event_date"].isoformat()
                if c.get("event_date")
                else None,
                "knowledge_date": c["knowledge_date"].isoformat()
                if c.get("knowledge_date")
                else None,
                "value": c.get("value"),
                "source": c.get("source"),
            }
            for c in firings
        ],
    }


def _email_body(alert, firings: list, entity_symbol: str | None) -> str:
    condition = alert["condition"]
    line = _describe_condition(condition)
    subject_name = entity_symbol or str(alert["entity_id"])[:8]
    lines = [
        f"Omni alert fired: {subject_name} -- {line}",
        "",
        f"Claim type: {alert['claim_type']}",
        "",
        "Firings:",
    ]
    for c in firings:
        lines.append(
            f"  {c['knowledge_date']}  {c['source']}  {c['value']}"
        )
    lines += [
        "",
        "Unacknowledged firings are also visible in the app under Discover > Alerts.",
    ]
    return "\n".join(lines)


async def _notify_config(pool, user_id) -> dict:
    row = await pool.fetchrow(_NOTIFY_SETTINGS, user_id)
    if row is None:
        return {}
    data = row["data"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            data = {}
    return (data or {}).get("notify") or {}


async def send_test(pool, user_id) -> dict:
    """Send one test event through every configured channel.

    Used by the settings UI so a self-hoster can prove the pipe works before
    relying on it. Raises on failure -- unlike dispatch, a test exists to
    surface the failure.
    """
    notify = await _notify_config(pool, user_id)
    payload = {
        "test": True,
        "message": "Omni alert delivery test -- if you can read this, the channel works.",
    }
    sent: list[str] = []
    webhook_url = notify.get("webhook_url")
    if webhook_url:
        await _send_webhook(webhook_url, payload)
        sent.append("webhook")
    email_to = notify.get("email")
    if email_to:
        if not settings.smtp_host:
            raise RuntimeError(
                "email channel requires the deployment's SMTP configuration "
                "(OMNI_SMTP_HOST)"
            )
        msg = EmailMessage()
        msg["Subject"] = "Omni alert delivery test"
        msg["From"] = settings.smtp_from
        msg["To"] = email_to
        msg.set_content(payload["message"])
        await asyncio.to_thread(
            _send_email_message, email_to, msg
        )
        sent.append("email")
    if not sent:
        raise RuntimeError("no delivery channel is configured")
    return {"sent": sent}


def _send_email_message(to_address: str, msg: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        # A verifying context, never the stdlib fallback: an unverified
        # STARTTLS hands the relay credentials and every alert body to whoever
        # is between the process and the relay.
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


async def dispatch(pool, alert, firings: list, *, conn=None) -> None:
    """Enqueue delivery of one alert's new firings through every channel.

    No network happens here: rows in notification_delivery carry everything a
    retry needs (full message content and destination), and the scheduler's
    delivery loop does the sending with bounded retries. A channel that is
    down costs its row a retry, not the notification.

    With ``conn`` given, every write runs on that caller's connection and
    inside the caller's transaction: firing and enqueue commit or roll back
    as one unit (audit A09). A firing whose notification rows were lost to a
    crash between the two transactions could never be re-detected -- the
    firing record is exactly what evaluation skips as already-done.
    """
    executor = conn if conn is not None else pool

    notify = await _notify_config(executor, alert["user_id"])

    webhook_url = notify.get("webhook_url")
    email_to = notify.get("email")
    if not webhook_url and not email_to:
        return

    entity_symbol = await executor.fetchval(
        "SELECT symbol FROM entity WHERE id = $1", alert["entity_id"]
    )
    payload = _payload(alert, firings, entity_symbol)
    rows = []
    if webhook_url:
        rows.append(
            ("webhook", {"kind": "webhook", "url": webhook_url, "body": payload})
        )
    if email_to:
        rows.append(
            (
                "email",
                {
                    "kind": "email",
                    "to": email_to,
                    "subject": (
                        f"Omni alert: {entity_symbol or str(alert['entity_id'])[:8]} -- "
                        f"{_describe_condition(alert['condition'])}"
                    ),
                    "body": _email_body(alert, firings, entity_symbol),
                },
            )
        )
    for channel, queued in rows:
        await executor.execute(
            """
            INSERT INTO notification_delivery
                (user_id, alert_id, channel, payload)
            VALUES ($1, $2, $3, $4::jsonb)
            """,
            alert["user_id"],
            alert["id"],
            channel,
            json.dumps(queued),
        )


async def delivery_status(pool, user_id) -> dict[str, int]:
    """Pending/failed/delivered counts for a user's delivery queue.

    Deliberately excludes the destination column: a webhook URL can embed a
    secret token, and status is not a reason to hand it back out.
    """
    rows = await pool.fetch(
        """
        SELECT status, count(*) AS n
        FROM notification_delivery
        WHERE user_id = $1
        GROUP BY status
        """,
        user_id,
    )
    counts = {row["status"]: int(row["n"]) for row in rows}
    return {
        "pending": counts.get("pending", 0),
        "failed": counts.get("failed", 0),
        "delivered": counts.get("delivered", 0),
    }


async def _attempt_delivery(queued: dict) -> None:
    if queued.get("kind") == "webhook":
        await _send_webhook(queued["url"], queued["body"])
        return
    if queued.get("kind") == "email":
        if not settings.smtp_host:
            raise RuntimeError(
                "email channel requires the deployment's SMTP configuration "
                "(OMNI_SMTP_HOST)"
            )
        msg = EmailMessage()
        msg["Subject"] = queued["subject"]
        msg["From"] = settings.smtp_from
        msg["To"] = queued["to"]
        msg.set_content(queued["body"])
        await asyncio.to_thread(_send_email_message, queued["to"], msg)
        return
    raise RuntimeError(f"unknown delivery channel payload kind: {queued.get('kind')!r}")


async def _claim_one_due_row(pool, due_from: datetime):
    """Claim the oldest due row for this worker and return it, or None.

    One short transaction (SKIP LOCKED, so two delivery workers -- or a
    manual run against a live scheduler -- each take a different row, never
    the same one) that counts the attempt and hides the row for the lease
    window. The lease is stamped from this claim, not from the pass start:
    a row claimed after nineteen siblings already sent still gets a full
    lease of its own.
    """
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT id, channel, payload, attempts
            FROM notification_delivery
            WHERE status = 'pending' AND next_attempt_at <= $1
            ORDER BY created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            due_from,
        )
        if row is None:
            return None
        await conn.execute(
            """
            UPDATE notification_delivery
            SET attempts = attempts + 1, next_attempt_at = $2
            WHERE id = $1
            """,
            row["id"],
            datetime.now(UTC) + DELIVERY_CLAIM_LEASE,
        )
        return row


async def process_delivery_queue(
    pool,
    *,
    now: datetime | None = None,
    batch: int = DELIVERY_BATCH,
    max_attempts: int = MAX_DELIVERY_ATTEMPTS,
    ttl: timedelta = DELIVERY_TTL,
    retention: timedelta = DELIVERY_RETENTION,
    on_progress: Callable[[], None] | None = None,
) -> dict[str, int]:
    """Work the due part of the delivery queue once. Returns outcome counts.

    Each row is claimed in its own short transaction immediately before its
    send (SKIP LOCKED, so two delivery workers -- or a manual run against a
    live scheduler -- never send the same notification twice inside a pass),
    sent OUTSIDE any transaction, and its outcome written as its own
    committed statement fenced on the claim -- ``attempts`` must still match
    and the row must still be ``pending``. A worker whose lease lapsed
    mid-send and lost the row to a fresher one therefore cannot overwrite
    the fresher outcome: a delivered row cannot be flipped to ``failed`` by
    a stale writer. An outcome write that matches no row means the row was
    lost, and is not counted.

    The sends used to happen inside one batch-wide claiming transaction: one
    aborted transaction (a later row's UPDATE failing, a dropped connection)
    rolled every already-sent row's ``delivered`` update back, and the next
    pass re-sent the whole batch (audit A10). A crash between a successful
    send and its outcome write still re-sends that one row when its claim
    lease lapses -- at-least-once delivery, the honest minimum for a queue
    whose consumers are webhooks and mailboxes.

    Timestamps are stamped per row, not from the pass start: the claim lease
    and the retry backoff are anchored to when the row is actually claimed
    and when its outcome is written, so a late row in a slow batch waits out
    its full backoff instead of one already half-elapsed.

    ``on_progress`` (if given) is called after each row's outcome, so the
    scheduler's delivery loop can refresh its heartbeat mid-pass -- a batch
    of slow sends is a slow pass, not a wedged loop.

    Old pending rows expire to failed without an attempt: a retry landing
    hours after the event is noise, not alerting. Terminal rows older than
    ``retention`` are deleted in the same bounded pass (audit A11).
    """
    moment = now or datetime.now(UTC)
    expired = await pool.execute(
        """
        UPDATE notification_delivery
        SET status = 'failed', last_error = 'expired: delivery window elapsed'
        WHERE status = 'pending' AND created_at < $1
        """,
        moment - ttl,
    )
    purged_rows = await pool.fetch(
        """
        WITH old AS (
            SELECT ctid FROM notification_delivery
            WHERE status IN ('delivered', 'failed')
              AND created_at < $1
            ORDER BY created_at
            LIMIT 1000
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM notification_delivery n USING old WHERE n.ctid = old.ctid
        RETURNING 1
        """,
        moment - retention,
    )
    outcomes = {
        "delivered": 0,
        "retried": 0,
        "failed": 0,
        "expired": max(0, int(expired.split()[-1])),
        "purged": len(purged_rows),
    }

    for _ in range(batch):
        row = await _claim_one_due_row(pool, moment)
        if row is None:
            break
        queued = row["payload"]
        if isinstance(queued, str):
            queued = json.loads(queued)
        attempts = int(row["attempts"]) + 1
        try:
            await _attempt_delivery(queued)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one dead row must not stop the batch
            error = f"{type(exc).__name__}: {exc}"[:500]
            outcome_at = datetime.now(UTC)
            if attempts >= max_attempts:
                applied = await pool.execute(
                    """
                    UPDATE notification_delivery
                    SET status = 'failed', last_error = $2
                    WHERE id = $1 AND status = 'pending' AND attempts = $3
                    """,
                    row["id"],
                    error,
                    attempts,
                )
                if applied != "UPDATE 0":
                    outcomes["failed"] += 1
            else:
                backoff = DELIVERY_BACKOFF_BASE * (2 ** (attempts - 1))
                applied = await pool.execute(
                    """
                    UPDATE notification_delivery
                    SET last_error = $2, next_attempt_at = $3
                    WHERE id = $1 AND status = 'pending' AND attempts = $4
                    """,
                    row["id"],
                    error,
                    outcome_at + backoff,
                    attempts,
                )
                if applied != "UPDATE 0":
                    outcomes["retried"] += 1
            logger.warning(
                "notification delivery %s failed (attempt %d): %s",
                row["id"],
                attempts,
                error,
            )
        else:
            applied = await pool.execute(
                """
                UPDATE notification_delivery
                SET status = 'delivered', delivered_at = $2, last_error = NULL
                WHERE id = $1 AND status = 'pending' AND attempts = $3
                """,
                row["id"],
                datetime.now(UTC),
                attempts,
            )
            if applied != "UPDATE 0":
                outcomes["delivered"] += 1
        if on_progress is not None:
            on_progress()
    return outcomes


__all__ = [
    "delivery_status",
    "dispatch",
    "process_delivery_queue",
    "send_test",
]
