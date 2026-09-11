"""Delivery-side contracts for alert notification: webhook and email.

B19/B20 of the 2026-09-11 audit. The webhook target is member-supplied input
the API process makes a network request to, so these tests pin the containment
(not just https: a private literal or a credentialed URL is refused before any
connection exists) and the honesty contract (an endpoint answering 404 is a
failed delivery, not a sent one). The SMTP test pins the STARTTLS context: an
unverified handshake hands the relay credentials to whoever sits between the
process and the relay.
"""

from __future__ import annotations

import ipaddress
import ssl
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from omni.alerts import notify


class TestWebhookUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "http://hooks.example.com/x",
            "https://hooks.example.com:8443/x",
            "https://user:pw@hooks.example.com/x",
            "https://hooks.example.com/x#frag",
            "https://127.0.0.1/x",
            "https://169.254.169.254/latest/meta-data",
            "https://[::1]/x",
            "https://[::ffff:127.0.0.1]/x",
            "https://[2002:7f00:1::]/x",
            "https://10.0.0.5/x",
        ],
    )
    async def test_non_public_or_malformed_targets_are_refused_before_any_call(
        self, url
    ):
        with pytest.raises(RuntimeError):
            notify._validated_webhook_url(url)

    @pytest.mark.parametrize(
        "url",
        ["https://hooks.example.com/x", "https://1.1.1.1/x"],
        ids=["hostname", "public literal"],
    )
    async def test_public_https_443_targets_pass(self, url):
        assert notify._validated_webhook_url(url) == url

    def test_the_embedded_address_forms_are_judged_by_what_they_carry(self):
        assert notify._destination_allowed(ipaddress.ip_address("1.1.1.1"))
        assert not notify._destination_allowed(ipaddress.ip_address("127.0.0.1"))
        assert not notify._destination_allowed(
            ipaddress.ip_address("::ffff:169.254.169.254")
        )
        assert not notify._destination_allowed(ipaddress.ip_address("2002:7f00:1::"))


class _Response404:
    status = 404

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Post404:
    def post(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        return _Response404()


class _Session404:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return SimpleNamespace(post=_Post404().post)

    async def __aexit__(self, *exc):
        return False


class TestDeliveryHonesty:
    async def test_a_404_answer_is_a_failed_delivery_not_a_sent_one(
        self, monkeypatch
    ):
        import aiohttp

        monkeypatch.setattr(aiohttp, "ClientSession", _Session404)
        with pytest.raises(RuntimeError, match="HTTP 404"):
            await notify._send_webhook("https://hooks.example.com/x", {"test": True})


class TestStarttlsVerification:
    def test_starttls_receives_a_verifying_context(self, monkeypatch):
        received: dict = {}

        class _SMTP:
            def __init__(self, host, port, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self, *, context=None):
                received["context"] = context

            def ehlo(self):
                pass

            def send_message(self, msg):
                pass

        monkeypatch.setattr(notify.smtplib, "SMTP", _SMTP)
        monkeypatch.setattr(notify.settings, "smtp_host", "relay.example")
        monkeypatch.setattr(notify.settings, "smtp_user", "")

        msg = EmailMessage()
        msg["Subject"] = "test"

        notify._send_email_message("who@example.com", msg)

        context = received["context"]
        assert isinstance(context, ssl.SSLContext)
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED
