"""Settings API for reporting configuration and controlling venue state.

Secret values are deployment-managed. The API reports whether they are
configured without returning them to the browser.
"""

from __future__ import annotations

import json
from typing import Any

from neutron import App, Router
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.config import settings
from omni.credentials.catalog import PROVIDER_CATALOG

__all__ = ["build_router"]


async def _load_settings(pool, user_id) -> dict[str, Any]:
    """Load the operator's saved settings from the DB."""
    row = await pool.fetchrow(
        "SELECT data FROM user_settings WHERE user_id = $1", user_id
    )
    if row is None:
        return {"providers": {}, "venues": {}}
    return json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]


async def _save_settings(pool, user_id, data: dict) -> None:
    """Persist settings, upserting."""
    await pool.execute(
        """
        INSERT INTO user_settings (user_id, data) VALUES ($1, $2::jsonb)
        ON CONFLICT (user_id) DO UPDATE SET data = $2::jsonb
        """,
        user_id, json.dumps(data),
    )


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
        "requires_process": False,
        "description": "Perpetual + spot crypto. The carry book runs here.",
        "fields": [
            {"name": "wallet_address", "label": "Wallet Address", "type": "text", "required": True},
            {"name": "private_key", "label": "Private Key", "type": "password", "required": True},
        ],
    },
    {
        "key": "questrade",
        "label": "Questrade (Read-Only)",
        "type": "equity",
        "requires_process": False,
        "description": "Canadian broker. Read-only (balance/positions) — trade scope is partner-gated.",
        "fields": [
            {"name": "refresh_token", "label": "Refresh Token", "type": "password", "required": True},
            {"name": "practice", "label": "Practice Mode", "type": "checkbox", "required": False},
        ],
    },
    {
        "key": "ibkr",
        "label": "Interactive Brokers (Full Trading)",
        "type": "equity",
        "requires_process": True,
        "description": "Full equity trading. Requires IB Gateway container (managed by this system). Paper trading recommended for always-on.",
        "fields": [
            {"name": "username", "label": "IBKR Username", "type": "text", "required": True},
            {"name": "password", "label": "IBKR Password", "type": "password", "required": True},
            {"name": "mode", "label": "Trading Mode", "type": "select", "options": ["paper", "live"], "required": True},
            {"name": "account_id", "label": "Account ID (optional)", "type": "text", "required": False},
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
            # The carry book's keys are deployment-managed on purpose: the
            # scheduler holds them and the api never receives them.
            configured = bool(
                settings.hyperliquid_wallet_address
                and settings.hyperliquid_private_key
            )
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


def _body_contains_secrets(body: dict) -> bool:
    if body.get("providers"):
        return True
    venues = body.get("venues", {})
    return any(
        isinstance(value, dict) and "credentials" in value
        for value in venues.values()
    )


def build_router(app: App) -> Router:
    router = Router()

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

    @router.post("/settings")
    async def save_settings(request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        if audience is None:
            from neutron.error import unauthorized
            raise unauthorized("Authentication required")

        body = await request.json()
        if _body_contains_secrets(body):
            from neutron.error import bad_request

            raise bad_request(
                "Secret values cannot be saved through the browser; configure "
                "them in the deployment environment"
            )
        saved = await _load_settings(app.db.pool, audience)
        if "providers" in body:
            saved["providers"] = {**saved.get("providers", {}), **body["providers"]}
        if "venues" in body:
            saved["venues"] = {**saved.get("venues", {}), **body["venues"]}
        await _save_settings(app.db.pool, audience, saved)
        return {"status": "saved"}

    @router.post("/settings/venue/{venue_key}/credentials")
    async def set_venue_credentials(venue_key: str, request: Request) -> dict:
        """Store a venue's credentials, encrypted at rest.

        The ONE door for secrets. `POST /settings` still refuses them, so there
        is exactly one path into storage and it always encrypts -- a second
        writer that forgot would leave plaintext rows that read back perfectly
        and nothing would report it.

        Nothing is ever returned. The response says what was stored and whether
        the venue then connected, and the connection attempt runs immediately so
        a wrong credential is reported now rather than at the next scheduler
        cycle.
        """
        from neutron.error import bad_request, not_found, unauthorized

        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")

        entry = next((v for v in VENUE_CATALOG if v["key"] == venue_key), None)
        if entry is None:
            raise not_found(f"unknown venue {venue_key}")

        if venue_key == "hyperliquid":
            # Refusing is the security boundary, not an inconvenience. These keys
            # can move the carry book, they live only in the scheduler's
            # environment, and the api process is deliberately never given them.
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

        from omni.venue.manager import refresh_venues, store_venue_credentials

        await store_venue_credentials(app.db.pool, audience, venue_key, supplied)
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
        from neutron.error import unauthorized

        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")

        saved = await _load_settings(app.db.pool, audience)
        venues = saved.setdefault("venues", {})
        entry = venues.get(venue_key)
        if entry is None:
            return {"status": "not stored"}

        entry.pop("credentials", None)
        entry["enabled"] = False
        await _save_settings(app.db.pool, audience, saved)

        from omni.venue.manager import refresh_venues

        await refresh_venues(app.db.pool, audience)
        return {"status": "cleared"}

    @router.post("/settings/venue/{venue_key}/toggle")
    async def toggle_venue(venue_key: str, request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        if audience is None:
            from neutron.error import unauthorized
            raise unauthorized("Authentication required")

        body = await request.json()
        if "credentials" in body:
            from neutron.error import bad_request

            raise bad_request(
                "Venue credentials cannot be saved through the browser; "
                "configure them in the deployment environment"
            )
        enabled = body.get("enabled", False)
        saved = await _load_settings(app.db.pool, audience)
        saved.setdefault("venues", {})
        if venue_key not in saved["venues"]:
            saved["venues"][venue_key] = {}
        saved["venues"][venue_key]["enabled"] = enabled

        await _save_settings(app.db.pool, audience, saved)

        from omni.venue.manager import refresh_venues
        status = await refresh_venues(app.db.pool, audience)

        if venue_key == "ibkr" and enabled:
            return {"status": "enabled", "note": "IB Gateway container will start on next scheduler cycle"}
        return {"status": "enabled" if enabled else "disabled", "venue_status": status.get(venue_key, "unknown")}

    @router.get("/settings/venues/status")
    async def venue_status(request: Request) -> dict:
        """Live status of all connected venues — positions and balances."""
        audience = resolve_audience_from_request(request)
        if audience is None:
            from neutron.error import unauthorized
            raise unauthorized("Authentication required")

        from omni.venue.manager import connected_venues, refresh_venues
        status = await refresh_venues(app.db.pool, audience)
        venues = connected_venues()

        venue_data: list[dict] = []
        for key, venue in venues.items():
            entry: dict[str, Any] = {"key": key, "name": getattr(venue, "name", key)}
            try:
                positions = await venue.positions()
                entry["positions"] = [
                    {"symbol": p.symbol, "quantity": str(p.quantity),
                     "market_type": p.market_type.value if hasattr(p.market_type, "value") else str(p.market_type),
                     "average_entry": str(p.average_entry)}
                    for p in positions
                ]
            except Exception:  # noqa: BLE001
                entry["positions"] = []
            try:
                balances = await venue.balances()
                entry["balances"] = [
                    {"asset": b.asset, "free": str(b.free), "locked": str(b.locked)}
                    for b in balances
                ]
            except Exception:  # noqa: BLE001
                entry["balances"] = []
            venue_data.append(entry)

        return {"venues": venue_data, "status": status}

    return router
