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
import ipaddress
import json
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

from omni.config import settings

logger = logging.getLogger("omni.alerts.notify")

_NOTIFY_SETTINGS = "SELECT data FROM user_settings WHERE user_id = $1"


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
    if parts.port is not None and parts.port != 443:
        raise RuntimeError(
            f"webhook url must use port 443, got {parts.port}"
        )
    if not parts.hostname:
        raise RuntimeError("webhook url has no host")
    try:
        literal = ipaddress.ip_address(parts.hostname)
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


async def _send_email(to_address: str, alert, firings: list, entity_symbol) -> None:
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
        # A verifying context, never the stdlib fallback: an unverified
        # STARTTLS hands the relay credentials and every alert body to whoever
        # is between the process and the relay.
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
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
        except Exception:
            logger.warning("alert webhook delivery failed", exc_info=True)

    if email_to and settings.smtp_host:
        try:
            await asyncio.to_thread(
                _send_email, email_to, alert, firings, entity_symbol
            )
        except Exception:
            logger.warning("alert email delivery failed", exc_info=True)


__all__ = ["dispatch", "send_test"]
