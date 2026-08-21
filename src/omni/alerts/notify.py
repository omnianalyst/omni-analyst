"""Delivery for alert firings: webhook and email.

A firing nobody hears about is a row in a table. This module is the second
half of alerting -- getting the event to the person who asked for it -- with
two channels, each opt-in per user:

  * webhook: a single JSON POST per firing batch, to a URL the user set.
    Whatever sits at the other end (a bridge script, ntfy, a Discord hook) is
    the user's business; Omni only promises the payload shape.
  * email: plain text, through the deployment's SMTP configuration. The
    address is the user's; the relay is the operator's.

Failure discipline: a delivery that cannot be sent is LOGGED and swallowed. A
firing is recorded before delivery is attempted, and a dead webhook must never
stop the next alert from being evaluated -- the record is the source of truth
and the inbox (unacknowledged firings) is the fallback view. There is no retry
queue on purpose: a personal instance with a flapping webhook would silently
accumulate a backlog that retries then dump at 3am. One attempt, one log line,
the record stands.

Configuration lives in user_settings.data.notify: {"webhook_url": ...,
"email": ...}. Per-user, because alerts are per-user; SMTP is per-deployment
because the relay is infrastructure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from omni.config import settings

logger = logging.getLogger("omni.alerts.notify")

_NOTIFY_SETTINGS = "SELECT data FROM user_settings WHERE user_id = $1"


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
    except Exception:  # noqa: BLE001 - a subject line never justifies a raise
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


async def _send_webhook(url: str, payload: dict) -> None:
    import httpx

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(
            url, json=payload, headers={"content-type": "application/json"}
        )
        if response.status_code >= 400:
            logger.warning(
                "alert webhook returned HTTP %d", response.status_code
            )


def _send_email(to_address: str, alert, firings: list, entity_symbol) -> None:
    """Blocking SMTP send; called via to_thread from dispatch."""
    subject_name = entity_symbol or str(alert["entity_id"])[:8]
    msg = EmailMessage()
    msg["Subject"] = (
        f"Omni alert: {subject_name} -- "
        f"{_describe_condition(alert['condition'])}"
    )
    msg["From"] = settings.smtp_from
    msg["To"] = to_address
    msg.set_content(_email_body(alert, firings, entity_symbol))
    _send_email_message(to_address, msg)


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
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


async def dispatch(pool, alert, firings: list) -> None:
    """Deliver one alert's new firings through every configured channel."""
    notify = await _notify_config(pool, alert["user_id"])

    webhook_url = notify.get("webhook_url")
    email_to = notify.get("email")
    if not webhook_url and not (email_to and settings.smtp_host):
        return

    entity_symbol = await pool.fetchval(
        "SELECT symbol FROM entity WHERE id = $1", alert["entity_id"]
    )
    payload = _payload(alert, firings, entity_symbol)

    if webhook_url:
        try:
            await _send_webhook(webhook_url, payload)
        except Exception:  # noqa: BLE001 - logged, never blocking
            logger.warning("alert webhook delivery failed", exc_info=True)

    if email_to and settings.smtp_host:
        try:
            await asyncio.to_thread(
                _send_email, email_to, alert, firings, entity_symbol
            )
        except Exception:  # noqa: BLE001 - logged, never blocking
            logger.warning("alert email delivery failed", exc_info=True)


__all__ = ["dispatch", "send_test"]
