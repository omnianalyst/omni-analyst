"""Settings API for reporting configuration and controlling venue state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from neutron import App, Router
from pydantic import BaseModel
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.config import settings
from omni.credentials.catalog import PROVIDER_CATALOG

__all__ = ["build_router"]


class NotifyIn(BaseModel):
    webhook_url: str | None = None
    email: str | None = None


async def _load_settings(pool, user_id) -> dict[str, Any]:
    """Load the operator's saved settings from the DB."""
    row = await pool.fetchrow(
        "SELECT data FROM user_settings WHERE user_id = $1", user_id
    )
    if row is None:
        return {"providers": {}, "venues": {}}
    return json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]


def _provider_catalog_payload() -> list[dict]:
    """Render the credential catalog for the UI."""
    out = []
    for key, entry in sorted(PROVIDER_CATALOG.items()):
        settings_field = entry.get("settings_field", "")
        out.append({
            "key": key,
            "label": entry.get("label", key),
            "category": entry.get("category", ""),
            "settings_field": settings_field,
            "key_required": entry.get("key_required", False),
            "wired": entry.get("wired", False),
            "configured": bool(
                settings_field and getattr(settings, settings_field, "")
            ),
        })
    return out


VENUE_CATALOG = [
    {
        "key": "hyperliquid",
        "label": "Hyperliquid (Crypto)",
        "type": "crypto",
        "connectable": False,
        "requires_process": False,
        "description": "The carry book is scheduler-managed. Its trading key never enters the API.",
        "fields": [],
    },
    {
        "key": "questrade",
        "label": "Questrade (Read-Only)",
        "type": "equity",
        "connectable": True,
        "requires_process": False,
        "description": "Canadian broker. Read-only (balance/positions) — trade scope is partner-gated.",
        "fields": [
            {"name": "refresh_token", "label": "Refresh Token", "type": "password", "required": True},
            {"name": "practice", "label": "Practice Mode", "type": "checkbox", "required": False},
        ],
    },
]


def _venue_catalog_payload(saved: dict) -> list[dict]:
    """Every venue, with how its credentials are held and whether it is usable.

    `configuration_source` is the honest part. Before an encrypted store existed
    this page could only report `legacy` (a plaintext row somebody wrote
    directly) or `unavailable`, and it told the operator to use deployment
    secrets because the browser had nowhere safe to put them. Now
    `omni.credentials.keyring` exists, so `encrypted` is a real state and the
    difference between the three matters:

      deployment  -- read from the environment, never through this page
      encrypted   -- stored by this page under the credential key
      legacy      -- a plaintext row predating the keyring; must be re-entered
      unavailable -- nothing stored
    """
    from omni.credentials.keyring import is_encrypted
    from omni.venue.manager import SECRET_FIELDS

    saved_venues = saved.get("venues", {})
    out = []
    for entry in VENUE_CATALOG:
        key = entry["key"]
        stored = saved_venues.get(key, {})
        credentials = stored.get("credentials") or {}

        if key == "hyperliquid":
            configured = False
            source = "deployment"
        elif credentials:
            secret_fields = SECRET_FIELDS.get(key, ())
            present = [f for f in secret_fields if credentials.get(f)]
            # `legacy` only when a secret field is stored WITHOUT the marker.
            # A partially-migrated row is legacy: one plaintext secret is enough
            # to make the record unsafe, so the weaker state wins.
            source = (
                "encrypted"
                if present and all(is_encrypted(credentials[f]) for f in present)
                else "legacy"
            )
            configured = True
        else:
            configured = False
            source = "unavailable"

        out.append({
            **entry,
            "configured": configured,
            "enabled": bool(stored.get("enabled")),
            "configuration_source": source,
        })
    return out


def _sanitized_venues(saved: dict) -> dict:
    return {
        entry["key"]: {
            "enabled": entry["enabled"],
            "configured": entry["configured"],
            "configuration_source": entry["configuration_source"],
        }
        for entry in _venue_catalog_payload(saved)
    }


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/settings/notifications")
    async def get_notifications(request: Request) -> dict:
        """Report the notify channels without returning the webhook URL.

        A webhook URL can embed a secret (a Discord/Slack token); reporting
        its mere existence is the same rule provider keys follow on this page.
        """
        user = resolve_audience_from_request(request)
        if user is None:
            from neutron.error import unauthorized

            raise unauthorized("Authentication required")
        row = await app.db.pool.fetchrow(
            "SELECT data FROM user_settings WHERE user_id = $1", user
        )
        data: dict = {}
        if row is not None:
            data = row["data"]
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (ValueError, TypeError):
                    data = {}
        notify = (data or {}).get("notify") or {}
        return {
            "webhook_configured": bool(notify.get("webhook_url")),
            "email": notify.get("email") or None,
            "smtp_available": bool(settings.smtp_host),
        }

    @router.put("/settings/notifications")
    async def put_notifications(body: NotifyIn, request: Request) -> dict:
        user = resolve_audience_from_request(request)
        if user is None:
            from neutron.error import unauthorized

            raise unauthorized("Authentication required")
        notify = {
            "webhook_url": (body.webhook_url or "").strip() or None,
            "email": (body.email or "").strip() or None,
        }
        await app.db.pool.execute(
            """
            INSERT INTO user_settings (user_id, data)
            VALUES ($1, jsonb_build_object('notify', $2::jsonb))
            ON CONFLICT (user_id) DO UPDATE SET
                data = user_settings.data || jsonb_build_object('notify', $2::jsonb)
            """,
            user,
            json.dumps(notify),
        )
        return {
            "webhook_configured": notify["webhook_url"] is not None,
            "email": notify["email"],
            "smtp_available": bool(settings.smtp_host),
        }

    @router.post("/settings/notifications/test")
    async def test_notifications(request: Request) -> dict:
        """Send one test event through every configured channel."""
        from omni.alerts.notify import send_test

        user = resolve_audience_from_request(request)
        if user is None:
            from neutron.error import unauthorized

            raise unauthorized("Authentication required")
        try:
            return await send_test(app.db.pool, user)
        except Exception as exc:  # noqa: BLE001 - the failure IS the payload
            from neutron.error import bad_request

            raise bad_request(str(exc)) from exc

    @router.get("/settings/config")
    async def get_settings(request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        if audience is None:
            saved = {"providers": {}, "venues": {}}
            providers = [
                {**entry, "configured": False}
                for entry in _provider_catalog_payload()
            ]
            venues = [
                {**entry, "configured": False, "enabled": False}
                for entry in _venue_catalog_payload(saved)
            ]
            return {
                "providers": {},
                "venues": {},
                "provider_catalog": providers,
                "venue_catalog": venues,
            }

        saved = await _load_settings(app.db.pool, audience)
        return {
            "providers": {},
            "venues": _sanitized_venues(saved),
            "provider_catalog": _provider_catalog_payload(),
            "venue_catalog": _venue_catalog_payload(saved),
        }

    @router.post("/settings/venue/{venue_key}/credentials")
    async def set_venue_credentials(venue_key: str, request: Request) -> dict:
        """Store a venue's credentials, encrypted at rest.

        The ONE door for secrets. There is no generic Settings mutation, so
        there is exactly one path into storage and it always encrypts -- a second
        writer that forgot would leave plaintext rows that read back perfectly
        and nothing would report it.

        Nothing is ever returned. An enabled venue reconnects immediately;
        credentials for a disabled venue are checked when it is enabled.
        """
        from neutron.error import bad_request, not_found, unauthorized

        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")

        entry = next((v for v in VENUE_CATALOG if v["key"] == venue_key), None)
        if entry is None:
            raise not_found(f"unknown venue {venue_key}")

        if not entry.get("connectable"):
            raise bad_request(
                "Hyperliquid credentials are deployment-managed and cannot be set "
                "from the browser. The api process does not hold trading keys."
            )

        body = await request.json()
        supplied = body.get("credentials")
        if not isinstance(supplied, dict) or not supplied:
            raise bad_request("credentials must be a non-empty object")

        known = {field["name"] for field in entry.get("fields", [])}
        unknown = set(supplied) - known
        if unknown:
            raise bad_request(
                f"unknown field(s) for {venue_key}: {', '.join(sorted(unknown))}"
            )
        missing = [
            field["name"]
            for field in entry.get("fields", [])
            if field.get("required") and not supplied.get(field["name"])
        ]
        if missing:
            raise bad_request(f"missing required field(s): {', '.join(missing)}")
        invalid_selects = [
            field["name"]
            for field in entry.get("fields", [])
            if field.get("type") == "select"
            and supplied.get(field["name"]) not in field.get("options", [])
        ]
        if invalid_selects:
            raise bad_request(
                f"invalid selection for field(s): {', '.join(invalid_selects)}"
            )

        from omni.venue.manager import (
            disconnect_venue,
            refresh_venues,
            store_venue_credentials,
        )

        await store_venue_credentials(app.db.pool, audience, venue_key, supplied)
        await disconnect_venue(audience, venue_key)
        status = await refresh_venues(app.db.pool, audience)

        return {
            "status": "stored",
            "encrypted": True,
            "venue_status": status.get(venue_key, "not enabled"),
        }

    @router.delete("/settings/venue/{venue_key}/credentials")
    async def clear_venue_credentials(venue_key: str, request: Request) -> dict:
        """Forget a venue's credentials and disconnect it.

        Needed as much for the legacy-plaintext case as for revocation: the only
        way to clear a pre-keyring row is to remove it.
        """
        from neutron.error import bad_request, not_found, unauthorized

        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")

        entry = next((v for v in VENUE_CATALOG if v["key"] == venue_key), None)
        if entry is None:
            raise not_found(f"unknown venue {venue_key}")
        if not entry.get("connectable"):
            raise bad_request("Hyperliquid is scheduler-managed")

        from omni.venue.manager import clear_venue_credentials as clear_stored_credentials

        await clear_stored_credentials(app.db.pool, audience, venue_key)

        from omni.venue.manager import refresh_venues
        await refresh_venues(app.db.pool, audience)
        return {"status": "cleared"}

    @router.post("/settings/venue/{venue_key}/toggle")
    async def toggle_venue(venue_key: str, request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        if audience is None:
            from neutron.error import unauthorized
            raise unauthorized("Authentication required")

        from neutron.error import bad_request, not_found

        entry = next((v for v in VENUE_CATALOG if v["key"] == venue_key), None)
        if entry is None:
            raise not_found(f"unknown venue {venue_key}")
        if not entry.get("connectable"):
            raise bad_request("Hyperliquid is scheduler-managed")

        body = await request.json()
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise bad_request("enabled must be a boolean")

        from omni.venue.manager import refresh_venues, set_venue_enabled

        changed = await set_venue_enabled(
            app.db.pool, audience, venue_key, enabled
        )
        if not changed:
            raise bad_request("Add credentials before enabling this venue")
        status = await refresh_venues(app.db.pool, audience)

        return {"status": "enabled" if enabled else "disabled", "venue_status": status.get(venue_key, "unknown")}

    @router.get("/settings/venues/status")
    async def venue_status(request: Request) -> dict:
        """Live, timestamped status of the caller's venue connections."""
        audience = resolve_audience_from_request(request)
        if audience is None:
            from neutron.error import unauthorized
            raise unauthorized("Authentication required")

        from omni.venue.manager import connected_venues, refresh_venues

        saved = await _load_settings(app.db.pool, audience)
        status = await refresh_venues(app.db.pool, audience)
        venues = connected_venues(audience)

        venue_data: list[dict] = []
        for catalog_entry in _venue_catalog_payload(saved):
            key = catalog_entry["key"]
            live = venues.get(key)
            errors: list[str] = []
            entry: dict[str, Any] = {
                "key": key,
                "positions": [],
                "balances": [],
                "error": None,
            }

            if not catalog_entry.get("connectable"):
                entry["status"] = "scheduler_only"
            elif not catalog_entry["configured"]:
                entry["status"] = "not_configured"
            elif not catalog_entry["enabled"]:
                entry["status"] = "disabled"
            elif status.get(key, "").startswith("error: "):
                entry["status"] = "error"
                entry["error"] = status[key][7:]
            elif status.get(key) != "connected" or live is None:
                entry["status"] = "error"
                entry["error"] = status.get(key, "connection unavailable")
            else:
                try:
                    positions = await live.positions()
                    entry["positions"] = [
                        {
                            "symbol": p.symbol,
                            "quantity": str(p.quantity),
                            "market_type": p.market_type.value
                            if hasattr(p.market_type, "value")
                            else str(p.market_type),
                            "average_entry": str(p.average_entry),
                        }
                        for p in positions
                    ]
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"positions: {str(exc)[:100]}")
                try:
                    balances = await live.balances()
                    entry["balances"] = [
                        {"asset": b.asset, "free": str(b.free), "locked": str(b.locked)}
                        for b in balances
                    ]
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"balances: {str(exc)[:100]}")
                entry["status"] = "error" if errors else "connected"
                entry["error"] = "; ".join(errors) or None

            entry["checked_at"] = datetime.now(UTC).isoformat()
            venue_data.append(entry)

        return {"checked_at": datetime.now(UTC).isoformat(), "venues": venue_data}

    return router
