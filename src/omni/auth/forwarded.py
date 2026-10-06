"""Client address resolution for the auth front door.

``request.client.host`` is the socket peer. Behind Caddy or any reverse
proxy the peer is the proxy, so every user shares one throttle bucket -- and
naively switching to ``X-Forwarded-For`` lets any direct client name somebody
else's address and evade the throttle. Both failure modes are real, so the
rule is explicit:

* forwarded headers are read only when the immediate peer is a configured
  trusted proxy;
* within a trusted ``X-Forwarded-For`` chain the client is the rightmost
  address that is not itself a trusted proxy (the chain is appended to by
  every hop, so anything left of that was supplied by an earlier, untrusted
  hop and may be forged);
* with no trusted proxies configured, the socket peer is always the answer.
"""

from __future__ import annotations

import ipaddress

from starlette.requests import Request

from omni.auth.throttle import normalize_ip

_proxy_cache: dict[str, tuple] = {}


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


def _is_trusted(address: str, trusted) -> bool:
    try:
        ip = ipaddress.ip_address(address.strip("[]"))
    except ValueError:
        return False
    return any(ip in net for net in trusted)


def resolve_client_ip(headers, peer: str | None, trusted_proxies: str = "") -> str:
    """The client address for throttling, from headers and the socket peer."""
    peer = peer or "anonymous"
    trusted = _parse_trusted_proxies(trusted_proxies)
    if not trusted or not _is_trusted(peer, trusted):
        return peer

    forwarded = headers.get("x-forwarded-for", "")
    if not forwarded.strip():
        return peer
    candidates = [c.strip() for c in forwarded.split(",") if c.strip()]
    for candidate in reversed(candidates):
        if not _is_trusted(candidate, trusted):
            return candidate.strip("[]")
    # Every hop in the chain is itself a trusted proxy; the leftmost is then
    # the originating proxy's view of the client.
    return candidates[0].strip("[]")


def client_ip_from_request(request: Request) -> str:
    from omni.config import settings

    peer = request.client.host if request.client else None
    return normalize_ip(
        resolve_client_ip(request.headers, peer, settings.omni_trusted_proxies)
    )


__all__ = ["client_ip_from_request", "resolve_client_ip"]
