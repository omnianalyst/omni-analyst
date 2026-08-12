"""Venue manager: connects enabled venues from Settings.

Reads the operator's venue configuration from user_settings, instantiates the
appropriate Venue adapter for each enabled venue, and holds them for the
portfolio API to query.

This is the bridge between the Settings page toggle and actual connections:
toggle ON in Settings -> this module connects on the next reconcile -> data
appears in the UI.

Two boundaries this module must not blur:

- **Credentials are encrypted at rest.** Secrets arrive from `user_settings` as
  ciphertext and are decrypted here, in the process that needs them, and never
  returned to a caller. See `omni.credentials.keyring` for what that does and
  does not protect against.
- **Connecting is not trading.** A connected venue can be read. Placing an
  order goes through the trading tier's own gates, and nothing here may widen
  them -- a Settings toggle that enabled live orders by implication would be
  the most dangerous bug this file could carry.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from omni.credentials.keyring import decrypt_fields

logger = logging.getLogger("omni.venue.manager")

# Which fields of each venue's credential blob are secret. Anything named here
# is stored encrypted and decrypted only at connect time. A field absent from
# this map is stored in the clear, so adding a venue means deciding this
# explicitly rather than inheriting a default.
SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "questrade": ("refresh_token",),
    "ibkr": ("username", "password"),
    "hyperliquid": ("api_secret", "private_key", "agent_key"),
}

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
            credentials = decrypt_fields(
                vc.get("credentials", {}) or {}, SECRET_FIELDS.get(key, ())
            )
            venue = await _connect_venue(key, credentials)
            if venue is not None:
                _venues[key] = venue
                status[key] = "connected"
                logger.info("venue %s connected", key)
            else:
                status[key] = "no credentials"
        except Exception as exc:  # noqa: BLE001
            # The message is surfaced to the operator's Settings page, so it
            # must never carry the secret it failed on. Adapter errors quote
            # the endpoint and the failure class, not the credential.
            status[key] = f"error: {str(exc)[:100]}"
            logger.warning("venue %s failed: %s", key, exc)

    return status


async def store_venue_credentials(pool, user_id, venue_key: str, credentials: dict) -> None:
    """Write a venue's credentials, encrypting the secret fields first.

    The encryption happens here rather than at the API edge so there is exactly
    one path into storage. A second writer that forgot to encrypt would leave
    plaintext rows that read back perfectly, and nothing would report it.
    """
    from omni.credentials.keyring import encrypt_fields

    protected = encrypt_fields(credentials or {}, SECRET_FIELDS.get(venue_key, ()))
    config = await _load_venue_config(pool, user_id)
    venues = dict(config.get("venues", {}))
    entry = dict(venues.get(venue_key, {}))
    entry["credentials"] = protected
    venues[venue_key] = entry
    config["venues"] = venues

    await pool.execute(
        """
        INSERT INTO user_settings (user_id, data) VALUES ($1, $2::jsonb)
        ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data
        """,
        user_id,
        json.dumps(config),
    )


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


#: How often the scheduler re-reads venue configuration. Six minutes is short
#: enough that a Settings toggle takes effect while the operator is still
#: looking at the page, and long enough that a venue whose adapter is failing
#: is not retried in a tight loop.
RECONCILE_INTERVAL_SECONDS = 360.0


async def _operator_user(pool):
    """The single operator, matching how AutonomousRunner resolves one.

    Returns None on an empty users table -- a fresh deployment before setup,
    which is not an error and must not stop the scheduler booting.
    """
    return await pool.fetchval("SELECT id FROM users ORDER BY created_at LIMIT 1")


async def reconcile_once(pool) -> dict[str, str]:
    """Bring live connections in line with stored settings, once.

    Every failure is contained. A venue that will not connect must not prevent
    the scheduler from starting, because the scheduler's job is the coverage
    loops and the venues are an adjunct to it.
    """
    user_id = await _operator_user(pool)
    if user_id is None:
        return {}
    try:
        return await refresh_venues(pool, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("venue reconcile failed: %s", exc)
        return {}


async def reconcile_forever(pool, stopping, interval: float = RECONCILE_INTERVAL_SECONDS) -> None:
    """Reconcile at boot and on a bounded interval until asked to stop.

    Previously `refresh_venues` ran only when a Settings endpoint was called,
    so a venue enabled in the UI stayed disconnected until someone reloaded
    that page, and a restart silently dropped every connection. Comments in
    this module implied otherwise, which is worse than the gap itself.
    """
    import asyncio

    while not stopping.is_set():
        status = await reconcile_once(pool)
        if status:
            logger.info(
                "venues reconciled: %s",
                ", ".join(f"{key}={state}" for key, state in sorted(status.items())),
            )
        try:
            await asyncio.wait_for(stopping.wait(), timeout=interval)
        except TimeoutError:
            continue


def get_venue(key: str) -> Any | None:
    return _venues.get(key)


def connected_venues() -> dict[str, Any]:
    return dict(_venues)


async def disconnect_all() -> None:
    for key, venue in list(_venues.items()):
        if hasattr(venue, "aclose"):
            await venue.aclose()
    _venues.clear()
