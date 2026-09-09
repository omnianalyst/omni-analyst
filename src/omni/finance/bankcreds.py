"""Per-user bank-sync credentials, encrypted at rest.

Follows the data_keys pattern exactly: user_settings.data under
`finance_bank_keys`, secret fields encrypted via omni.credentials.keyring,
values never returned to the browser -- only their presence. Bank
credentials are BYO by definition: the user's own aggregator keys fetching
the user's own accounts.
"""

from __future__ import annotations

import json

from omni.credentials.keyring import decrypt_fields, encrypt_fields

BANK_PROVIDERS: dict[str, dict] = {
    "gocardless": {
        "label": "GoCardless Bank Account Data (EU/UK)",
        "fields": ("secret_id", "secret_key"),
    },
    "simplefin": {
        "label": "SimpleFIN (US/Canada)",
        "fields": ("access_url",),
    },
}

_SECRET_FIELDS = ("secret_id", "secret_key", "access_url")
_STORAGE_KEY = "finance_bank_keys"


async def put_bank_key(pool, user_id, provider: str, fields: dict) -> None:
    if provider not in BANK_PROVIDERS:
        raise ValueError(f"unsupported bank provider {provider!r}")
    clean = {
        name: (fields.get(name) or "").strip()
        for name in BANK_PROVIDERS[provider]["fields"]
    }
    missing = [n for n, v in clean.items() if not v]
    if missing:
        raise ValueError(f"{provider} needs {', '.join(missing)}")
    entry = {"credentials": encrypt_fields(clean, _SECRET_FIELDS)}
    await pool.execute(
        """
        INSERT INTO user_settings (user_id, data)
        VALUES ($1, jsonb_build_object($2::text, jsonb_build_object($3::text, $4::jsonb)))
        ON CONFLICT (user_id) DO UPDATE SET
            data = jsonb_set(
                user_settings.data,
                ARRAY[$2::text],
                COALESCE(user_settings.data->$2, '{}'::jsonb)
                     || jsonb_build_object($3::text, $4::jsonb),
                true
            ),
            updated_at = now()
        """,
        user_id,
        _STORAGE_KEY,
        provider,
        json.dumps(entry),
    )


async def remove_bank_key(pool, user_id, provider: str) -> None:
    await pool.execute(
        """
        UPDATE user_settings SET
            data = jsonb_set(
                user_settings.data,
                ARRAY[$2::text],
                COALESCE(user_settings.data->$2, '{}'::jsonb) - $3,
                true
            ),
            updated_at = now()
        WHERE user_id = $1
        """,
        user_id,
        _STORAGE_KEY,
        provider,
    )


async def get_bank_keys(pool, user_id) -> dict[str, dict[str, str]]:
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
    stored = (data or {}).get(_STORAGE_KEY) or {}
    out: dict[str, dict[str, str]] = {}
    for provider, entry in stored.items():
        if provider not in BANK_PROVIDERS:
            continue
        creds = decrypt_fields((entry or {}).get("credentials") or {}, _SECRET_FIELDS)
        creds = {k: v for k, v in creds.items() if v}
        if creds:
            out[provider] = creds
    return out


async def bank_configured(pool, user_id) -> dict[str, bool]:
    keys = await get_bank_keys(pool, user_id)
    return {provider: provider in keys for provider in BANK_PROVIDERS}


__all__ = [
    "BANK_PROVIDERS",
    "bank_configured",
    "get_bank_keys",
    "put_bank_key",
    "remove_bank_key",
]
