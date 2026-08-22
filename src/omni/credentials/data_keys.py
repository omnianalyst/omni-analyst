"""Per-user data-provider keys, encrypted at rest, resolved at fill time.

Why per-user keys exist next to the deployment's env keys: the licensing rule
makes a byo_only fetch private to the credential owner, so the key that
fetched a claim and the audience that may see it are the same fact. A user
who pastes their own Polygon key gets prices attributed to them; the
deployment's env key remains the fallback so a solo operator who configures
once in .env changes nothing.

Storage follows the venue-manager pattern exactly: user_settings.data under
a `data_keys` object, secret fields encrypted via omni.credentials.keyring,
values never returned to the browser -- only their presence.
"""

from __future__ import annotations

import json

from omni.credentials.keyring import decrypt_fields, encrypt_fields

# The providers a user may key here. Deliberately explicit: only providers
# with a live adapter and a real key belong; anything else would be a
# Settings row promising something the code does not do.
KEYED_PROVIDERS: dict[str, str] = {
    "polygon": "Equity and ETF prices. Free tier exists.",
    "fred": "Macro series. Works without a key; a free key raises limits.",
    "etherscan": "On-chain flows and supply. Free key.",
    "coingecko": "Crypto prices. Demo tier works; a key raises limits.",
}

_SECRET_FIELDS = ("api_key",)


async def put_key(pool, user_id, provider_key: str, api_key: str) -> None:
    """Store (or, with an empty string, remove) one provider key."""
    entry = (
        {"credentials": encrypt_fields({"api_key": api_key.strip()}, _SECRET_FIELDS)}
        if api_key.strip()
        else {}
    )
    await pool.execute(
        """
        INSERT INTO user_settings (user_id, data)
        VALUES ($1, jsonb_build_object('data_keys', jsonb_build_object($2::text, $3::jsonb)))
        ON CONFLICT (user_id) DO UPDATE SET
            data = jsonb_set(
                user_settings.data,
                '{data_keys}',
                CASE WHEN $3::jsonb = '{}'::jsonb
                     THEN COALESCE(user_settings.data->'data_keys', '{}'::jsonb) - $2
                     ELSE COALESCE(user_settings.data->'data_keys', '{}'::jsonb)
                          || jsonb_build_object($2::text, $3::jsonb)
                END,
                true
            ),
            updated_at = now()
        """,
        user_id,
        provider_key,
        json.dumps(entry),
    )


async def get_keys(pool, user_id) -> dict[str, str]:
    """Decrypted {provider_key: api_key} for the user. Never logged, never served."""
    row = await pool.fetchrow(
        "SELECT data FROM user_settings WHERE user_id = $1", user_id
    )
    if row is None:
        return {}
    data = row["data"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return {}
    stored = (data or {}).get("data_keys") or {}
    out: dict[str, str] = {}
    for key, entry in stored.items():
        if key not in KEYED_PROVIDERS:
            continue
        creds = decrypt_fields((entry or {}).get("credentials") or {}, _SECRET_FIELDS)
        api_key = creds.get("api_key")
        if api_key:
            out[key] = api_key
    return out


async def configured(pool, user_id) -> dict[str, bool]:
    """Presence per keyed provider -- the shape the Settings UI shows."""
    keys = await get_keys(pool, user_id)
    return {key: key in keys for key in KEYED_PROVIDERS}


__all__ = ["KEYED_PROVIDERS", "configured", "get_keys", "put_key"]
