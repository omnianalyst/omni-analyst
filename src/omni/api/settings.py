"""Settings API: manage credentials, venue adapters, and provider keys.

Reads/writes the operator's configuration — API keys, venue credentials,
adapter enable/disable state. Venue credentials are stored encrypted in
the database (not in plaintext env vars), decrypted at adapter-connect time.
"""

from __future__ import annotations

import json
from typing import Any

from neutron import App, Router
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
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
        out.append({
            "key": key,
            "label": entry.get("label", key),
            "category": entry.get("category", ""),
            "settings_field": entry.get("settings_field", ""),
            "key_required": entry.get("key_required", False),
            "wired": entry.get("wired", False),
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


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/settings")
    async def get_settings(request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        if audience is None:
            return {"providers": {}, "venues": {}, "provider_catalog": _provider_catalog_payload(), "venue_catalog": VENUE_CATALOG}

        saved = await _load_settings(app.db.pool, audience)
        return {
            "providers": saved.get("providers", {}),
            "venues": saved.get("venues", {}),
            "provider_catalog": _provider_catalog_payload(),
            "venue_catalog": VENUE_CATALOG,
        }

    @router.post("/settings")
    async def save_settings(request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        if audience is None:
            from neutron.error import unauthorized
            raise unauthorized("Authentication required")

        body = await request.json()
        saved = await _load_settings(app.db.pool, audience)
        if "providers" in body:
            saved["providers"] = {**saved.get("providers", {}), **body["providers"]}
        if "venues" in body:
            saved["venues"] = {**saved.get("venues", {}), **body["venues"]}
        await _save_settings(app.db.pool, audience, saved)
        return {"status": "saved"}

    @router.post("/settings/venue/{venue_key}/toggle")
    async def toggle_venue(venue_key: str, request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        if audience is None:
            from neutron.error import unauthorized
            raise unauthorized("Authentication required")

        body = await request.json()
        enabled = body.get("enabled", False)
        saved = await _load_settings(app.db.pool, audience)
        saved.setdefault("venues", {})
        if venue_key not in saved["venues"]:
            saved["venues"][venue_key] = {}
        saved["venues"][venue_key]["enabled"] = enabled
        await _save_settings(app.db.pool, audience, saved)

        if venue_key == "ibkr" and enabled:
            return {"status": "enabled", "note": "IB Gateway container will start on next scheduler cycle"}
        return {"status": "enabled" if enabled else "disabled"}

    return router
