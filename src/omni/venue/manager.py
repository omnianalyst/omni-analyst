"""Venue manager: connects enabled venues from Settings on startup.

Reads the operator's venue configuration from user_settings, instantiates
the appropriate Venue adapter for each enabled venue, and holds them for
the portfolio API to query.

This is the bridge between the Settings page toggle and actual connections:
toggle ON in Settings -> this module connects on next cycle -> portfolio
data appears in the UI.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("omni.venue.manager")

_venues: dict[str, Any] = {}
_last_check: dict[str, float] = {}


async def _load_venue_config(pool, user_id) -> dict:
    row = await pool.fetchrow(
        "SELECT data FROM user_settings WHERE user_id = $1", user_id
    )
    if row is None:
        return {}
    data = row["data"]
    return json.loads(data) if isinstance(data, str) else data


async def refresh_venues(pool, user_id) -> dict[str, str]:
    """Check Settings for enabled venues and connect/disconnect as needed.

    Returns a status dict: {venue_key: 'connected' | 'disabled' | 'error: ...'}
    """
    config = await _load_venue_config(pool, user_id)
    venues_config = config.get("venues", {})
    status: dict[str, str] = {}

    for key, vc in venues_config.items():
        enabled = vc.get("enabled", False)
        credentials = vc.get("credentials", {})

        if not enabled:
            if key in _venues:
                old = _venues.pop(key)
                if hasattr(old, "aclose"):
                    await old.aclose()
                logger.info("venue %s disconnected (disabled)", key)
            status[key] = "disabled"
            continue

        if key in _venues:
            status[key] = "connected"
            continue

        try:
            venue = await _connect_venue(key, credentials)
            if venue is not None:
                _venues[key] = venue
                status[key] = "connected"
                logger.info("venue %s connected", key)
            else:
                status[key] = "no credentials"
        except Exception as exc:  # noqa: BLE001
            status[key] = f"error: {str(exc)[:100]}"
            logger.warning("venue %s failed: %s", key, exc)

    return status


async def _connect_venue(key: str, credentials: dict) -> Any | None:
    if key == "hyperliquid":
        return None  # handled by the carry loop directly

    if key == "questrade":
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            return None
        from omni.venue.questrade_venue import QuestradeVenue
        practice = credentials.get("practice", True)
        return await QuestradeVenue.connect(
            refresh_token=refresh_token,
            practice=practice,
        )

    if key == "ibkr":
        username = credentials.get("username")
        password = credentials.get("password")
        if not username or not password:
            return None
        from omni.venue.ibkr_venue import IBKRVenue
        mode = credentials.get("mode", "paper")
        return await IBKRVenue.connect(
            username=username,
            password=password,
            mode=mode,
        )

    return None


def get_venue(key: str) -> Any | None:
    return _venues.get(key)


def connected_venues() -> dict[str, Any]:
    return dict(_venues)


async def disconnect_all() -> None:
    for key, venue in list(_venues.items()):
        if hasattr(venue, "aclose"):
            await venue.aclose()
    _venues.clear()
