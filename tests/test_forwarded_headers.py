"""Forwarded-header parsing at the auth front door (audit finding A05).

A trusted peer providing garbage X-Forwarded-For used to crash the login
path (an all-empty chain reached ``candidates[0]`` of an empty list) or turn
arbitrary text into a throttle key. These tests pin the hardened contract:
malformed input anywhere in a relevant chain falls back to the socket peer,
canonicalization collapses equivalent address forms, and a broken proxy-trust
configuration fails startup instead of being rediscovered per request.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from omni.auth.forwarded import (
    canonical_ip,
    resolve_client_ip,
    validate_trusted_proxies,
)
from omni.main import create_app

SECRET = "f" * 48


class TestCanonicalIp:
    def test_ipv4_round_trips(self):
        assert canonical_ip("192.0.2.1") == "192.0.2.1"

    def test_brackets_are_unwrapped(self):
        assert canonical_ip("[2001:db8::1]") == "2001:db8::1"

    def test_ipv4_mapped_ipv6_collapses_to_the_ipv4(self):
        assert canonical_ip("::ffff:192.0.2.1") == "192.0.2.1"

    def test_scoped_addresses_are_refused(self):
        with pytest.raises(ValueError):
            canonical_ip("fe80::1%eth0")

    @pytest.mark.parametrize(
        "raw", ["", "   ", "not-an-address", "1.2.3.4.5", "1.2.3.4/32", "::ffff:"]
    )
    def test_garbage_raises(self, raw):
        with pytest.raises(ValueError):
            canonical_ip(raw)


class _Headers:
    """starlette-like header view for resolve_client_ip (getlist support)."""

    def __init__(self, values: list[str]):
        self._values = values

    def getlist(self, name):
        return self._values if name == "x-forwarded-for" else []

    def get(self, name, default=""):
        return self._values[0] if self._values else default


TRUSTED = "10.0.0.0/8"


class TestMalformedChainsFallBackToPeer:
    @pytest.mark.parametrize(
        "value", [", ,", ",", "  ", "garbage", "10.0.0.9, nan"]
    )
    def test_any_bad_entry_means_the_peer_answers(self, value):
        assert (
            resolve_client_ip(_Headers([value]), "10.0.0.9", TRUSTED) == "10.0.0.9"
        )

    def test_a_bad_entry_right_of_the_deciding_hop_poisons_the_chain(self):
        # The deciding hop is the rightmost untrusted address; garbage at or
        # right of it cannot be vouched for, so the peer answers.
        assert (
            resolve_client_ip(_Headers(["1.2.3.4, nan, 10.0.0.5"]), "10.0.0.9", TRUSTED)
            == "10.0.0.9"
        )

    def test_a_bad_entry_left_of_the_deciding_hop_is_irrelevant(self):
        # Entries left of the rightmost untrusted hop were supplied by
        # untrusted sources whatever they contain; the chain still resolves.
        assert (
            resolve_client_ip(_Headers(["1.2.3.4, , 198.51.100.7"]), "10.0.0.9", TRUSTED)
            == "198.51.100.7"
        )

    def test_empty_header_means_the_peer(self):
        assert resolve_client_ip(_Headers([""]), "10.0.0.9", TRUSTED) == "10.0.0.9"

    def test_absent_header_means_the_peer(self):
        assert resolve_client_ip(_Headers([]), "10.0.0.9", TRUSTED) == "10.0.0.9"

    def test_duplicate_forwarded_headers_are_ignored(self):
        # Two X-Forwarded-For headers are not a chain any proxy produces.
        assert (
            resolve_client_ip(
                _Headers(["198.51.100.7", "198.51.100.8"]), "10.0.0.9", TRUSTED
            )
            == "10.0.0.9"
        )

    def test_an_overlong_chain_is_refused_without_parsing(self):
        chain = ", ".join(["198.51.100.7"] * 64)
        assert resolve_client_ip(_Headers([chain]), "10.0.0.9", TRUSTED) == "10.0.0.9"

    def test_a_garbage_peer_is_one_bounded_key_not_attacker_text(self):
        assert resolve_client_ip(_Headers([]), "attacker-chosen-text", TRUSTED) == (
            "unknown-peer"
        )


class TestTrustedChainsStillResolve:
    def test_rightmost_untrusted_wins(self):
        assert (
            resolve_client_ip(
                _Headers(["6.6.6.6, 198.51.100.7, 10.0.0.5"]), "10.0.0.9", TRUSTED
            )
            == "198.51.100.7"
        )

    def test_mapped_v4_in_the_chain_is_canonicalized(self):
        assert (
            resolve_client_ip(
                _Headers(["::ffff:198.51.100.7"]), "10.0.0.5", TRUSTED
            )
            == "198.51.100.7"
        )

    def test_untrusted_peer_ignores_the_chain_entirely(self):
        assert (
            resolve_client_ip(_Headers(["198.51.100.7"]), "203.0.113.9", TRUSTED)
            == "203.0.113.9"
        )

    def test_no_trusted_proxies_means_peer_always(self):
        assert (
            resolve_client_ip(_Headers(["1.2.3.4"]), "10.0.0.5", "") == "10.0.0.5"
        )

    def test_plain_dict_headers_are_supported(self):
        assert (
            resolve_client_ip(
                {"x-forwarded-for": "198.51.100.7"}, "10.0.0.5", TRUSTED
            )
            == "198.51.100.7"
        )


class TestStartupValidation:
    async def test_a_broken_proxy_spec_fails_startup(self, database_url, monkeypatch):
        monkeypatch.setenv("OMNI_JWT_SECRET", SECRET)
        monkeypatch.setattr(
            "omni.config.settings.omni_trusted_proxies", "10.0.0.0/8, oops"
        )
        app = create_app(database_url)
        receive = asyncio.Queue()
        send = asyncio.Queue()
        task = asyncio.create_task(
            app({"type": "lifespan"}, receive.get, send.put)
        )
        await receive.put({"type": "lifespan.startup"})
        message = await send.get()
        assert message["type"] == "lifespan.startup.failed", message
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    def test_the_validator_is_loud_on_garbage(self):
        with pytest.raises(ValueError):
            validate_trusted_proxies("not-an-address")

    def test_a_valid_spec_passes(self):
        validate_trusted_proxies("10.0.0.0/8, 172.16.0.5, 2001:db8::/32")
