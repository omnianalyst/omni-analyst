"""Per-user data-provider keys: encrypted at rest, resolved at fill time."""

import json
from uuid import uuid4

import pytest

from omni.credentials import keyring
from neutron.auth.jwt import create_token
from neutron.test import TestClient

from omni.credentials.data_keys import configured, get_keys, put_key
from omni.api.settings import build_router
from omni.main import create_app

from test_api_coverage import _Lifespan


async def _user(db) -> uuid4:
    uid = uuid4()
    return await db.pool.fetchval(
        "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'x') "
        "RETURNING id",
        uid, f"keys-{uid.hex}@example.com",
    )


def _auth(user_id):
    import os

    os.environ.setdefault("OMNI_JWT_SECRET", "t" * 48)
    token = create_token({"sub": str(user_id)}, os.environ["OMNI_JWT_SECRET"])
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _keyring_path(tmp_path, monkeypatch):
    monkeypatch.delenv(keyring.KEY_ENV, raising=False)
    monkeypatch.setenv(keyring.KEY_PATH_ENV, str(tmp_path / "credential.key"))


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


class TestStore:
    async def test_roundtrip_is_encrypted_and_removable(self, db):
        user = await _user(db)
        await put_key(db.pool, user, "polygon", "pk_live_SECRET")
        keys = await get_keys(db.pool, user)
        assert keys == {"polygon": "pk_live_SECRET"}
        state = await configured(db.pool, user)
        assert state["polygon"] is True and state["fred"] is False

        # At rest, the stored value is ciphertext, not the key.
        raw = await db.pool.fetchval(
            "SELECT data #>> '{data_keys,polygon,credentials,api_key}' "
            "FROM user_settings WHERE user_id = $1",
            user,
        )
        assert raw is not None and "SECRET" not in raw

        # Empty string removes.
        await put_key(db.pool, user, "polygon", "")
        assert await get_keys(db.pool, user) == {}

    async def test_unknown_providers_are_never_returned(self, db):
        user = await _user(db)
        await db.pool.execute(
            """
            INSERT INTO user_settings (user_id, data)
            VALUES ($1, '{"data_keys": {"alpha_vantage": {"credentials": {}}}}')
            """,
            user,
        )
        assert await get_keys(db.pool, user) == {}


class TestEndpoints:
    async def test_save_and_report_without_returning_the_key(
        self, db, database_url
    ):
        user = await _user(db)
        app = create_app(database_url)
        app.include_router(build_router(app))
        async with _Lifespan(app), TestClient(app) as client:
            listed = await client.get("/settings/data-keys", headers=_auth(user))
            assert listed.status_code == 200
            keys = {p["key"] for p in listed.json()["providers"]}
            assert keys == {"polygon", "fred", "etherscan", "coingecko"}

            saved = await client.put(
                "/settings/data-keys/polygon",
                json={"api_key": "pk_live_SECRET"},
                headers=_auth(user),
            )
            assert saved.status_code == 200
            assert saved.json() == {"configured": True}
            assert "SECRET" not in json.dumps(saved.json())

            reread = await client.get("/settings/data-keys", headers=_auth(user))
            entry = next(
                p for p in reread.json()["providers"] if p["key"] == "polygon"
            )
            assert entry["configured"] is True

            removed = await client.delete(
                "/settings/data-keys/polygon", headers=_auth(user)
            )
            assert removed.json() == {"configured": False}

    async def test_anonymous_and_unknown_provider_are_refused(
        self, db, database_url
    ):
        user = await _user(db)
        app = create_app(database_url)
        app.include_router(build_router(app))
        async with _Lifespan(app), TestClient(app) as client:
            assert (await client.get("/settings/data-keys")).status_code == 401
            denied = await client.put(
                "/settings/data-keys/alpha_vantage",
                json={"api_key": "x"},
                headers=_auth(user),
            )
            assert denied.status_code == 400


class TestFillResolution:
    async def test_owner_key_overrides_env_credentials(self, db, monkeypatch):
        """The gap owner's stored key is what the adapter is constructed with.

        Proven with a patched adapter: the pipeline must hand the user's key
        through registration.call's kwargs, which override the env-bound
        credentials at construction.
        """
        from omni.capability.builtin import build_builtin_registry
        from omni.fill import pipeline
        from omni.ingest.fred import FredAdapter

        user = await _user(db)
        await put_key(db.pool, user, "fred", "user-key")
        entity = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('macro','M','m') "
            "RETURNING id"
        )
        await db.pool.execute(
            """
            INSERT INTO gap (entity_id, claim_type, key, gap_class, score, audience_user_id)
            VALUES ($1, 'macro_series_point', 'DGS10', 'missing', 1.0, $2)
            """,
            entity, user,
        )

        registry = build_builtin_registry(settings=None)
        seen: dict[str, object] = {}

        async def fake_fetch(self, key):
            seen["api_key"] = getattr(self, "_api_key", None)
            return []  # empty is a real answer; the assertion is about the key

        monkeypatch.setattr(FredAdapter, "fetch", fake_fetch)
        gap = await pipeline.claim_next_gap(db.pool, worker_id="t")
        assert gap is not None
        await pipeline.fill_gap(
            db.pool, gap, registry=registry, credential_owner=user
        )
        assert seen.get("api_key") == "user-key", (
            "the owner's stored key must reach the adapter constructor"
        )
