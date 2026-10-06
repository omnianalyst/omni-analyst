"""Client address resolution for the auth front door.

``request.client.host`` is the socket peer -- in this deployment. Behind
Caddy or any reverse proxy the peer is the proxy, so every user shares one
throttle bucket, and naively switching to ``X-Forwarded-For`` lets any
direct client name somebody else's address and evade the throttle. Both
failure modes are real, so the rule is explicit:

* forwarded headers are read only when the immediate peer is a configured
  trusted proxy;
* within a trusted ``X-Forwarded-For`` chain the client is the rightmost
  address that is not itself a trusted proxy (the chain is appended to by
  every hop, so anything left of that was supplied by an earlier, untrusted
  hop and may be forged);
* with no trusted proxies configured, the socket peer is always the answer.

Malformed input never becomes a throttle key (audit A05): a chain entry
that is not exactly one IP address (empty, scoped, garbage text, an
overlong chain) makes the whole header untrusted and the peer wins. An
earlier version crashed on an all-empty chain (``", ,"`` reached
``candidates[0]`` of an empty list) -- a trusted peer could 500 the login
path; now every entry is parsed with ``canonical_ip`` before use.

``OMNI_TRUSTED_PROXIES`` is validated at startup (see
``validate_trusted_proxies``), so a typo'd spec fails the process loudly
instead of being rediscovered per request.
"""

from __future__ import annotations

import ipaddress

from starlette.requests import Request

from omni.auth.throttle import normalize_ip

_proxy_cache: dict[str, tuple] = {}

#: A chain longer than any legitimate proxy stack; beyond this the header is
#: garbage, not topology, and the peer wins without parsing 1000 entries.
MAX_FORWARDED_ENTRIES = 32


def canonical_ip(raw: str) -> str:
    """Parse one exact IP address; raise ValueError on anything else.

    Bracketed IPv6 literals are unwrapped, IPv4-mapped IPv6 collapses to the
    IPv4 it carries (they are the same client and must share a bucket), and
    scoped addresses (``fe80::1%eth0``) are refused: a scope is interface
    noise, not an identity.
    """
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if "%" in value:
        raise ValueError("scoped addresses are not accepted here")
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.compressed


def _parse_trusted_proxies(spec: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    cached = _proxy_cache.get(spec)
    if cached is not None:
        return cached
    networks = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        # A malformed entry raises: silently skipping it would quietly narrow
        # the trusted set and re-introduce the shared-bucket bug this module
        # exists to fix, invisibly. Misconfigured proxy trust must be loud.
        if "/" in part:
            networks.append(ipaddress.ip_network(part, strict=False))
        else:
            suffix = "/128" if ":" in part else "/32"
            networks.append(ipaddress.ip_network(part + suffix, strict=False))
    parsed = tuple(networks)
    _proxy_cache[spec] = parsed
    return parsed


def validate_trusted_proxies(spec: str) -> None:
    """Fail loudly on an unusable proxy-trust configuration.

    Called from the app lifespan so a typo in OMNI_TRUSTED_PROXIES is a
    startup error, not a per-request exception racing the first login.
    """
    _parse_trusted_proxies(spec)


def _is_trusted(address: str, trusted) -> bool:
    try:
        ip = ipaddress.ip_address(address.strip("[]"))
    except ValueError:
        return False
    return any(ip in net for net in trusted)


def resolve_client_ip(headers, peer: str | None, trusted_proxies: str = "") -> str:
    """The client address for throttling, from headers and the socket peer."""
    try:
        peer = canonical_ip(peer or "")
    except ValueError:
        # A peer that is not an address (a test transport's name, a unix
        # socket) is one bounded fallback key, not attacker-supplied text.
        return "unknown-peer"
    trusted = _parse_trusted_proxies(trusted_proxies)
    if not trusted or not _is_trusted(peer, trusted):
        return peer

    values = (
        headers.getlist("x-forwarded-for")
        if hasattr(headers, "getlist")
        else [headers.get("x-forwarded-for", "")]
    )
    # Duplicate forwarded headers are not a chain any proxy produces; treat
    # them as absent rather than guessing an order.
    if len(values) != 1 or not values[0].strip():
        return peer
    candidates = values[0].split(",")
    if len(candidates) > MAX_FORWARDED_ENTRIES:
        return peer
    leftmost = peer
    for raw in reversed(candidates):
        try:
            candidate = canonical_ip(raw)
        except ValueError:
            # A malformed relevant hop poisons the chain: nothing in it can
            # be trusted to speak for the client, so the peer answers.
            return peer
        leftmost = candidate
        if not _is_trusted(candidate, trusted):
            return candidate
    # Every hop in the chain is itself a trusted proxy; the leftmost is then
    # the originating proxy's view of the client.
    return leftmost


def client_ip_from_request(request: Request) -> str:
    from omni.config import settings

    peer = request.client.host if request.client else None
    return normalize_ip(
        resolve_client_ip(request.headers, peer, settings.omni_trusted_proxies)
    )


__all__ = [
    "canonical_ip",
    "client_ip_from_request",
    "resolve_client_ip",
    "validate_trusted_proxies",
]
