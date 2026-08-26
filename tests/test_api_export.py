"""The export surface: the caller's own data, out, as files.

What these defend: everything exports through the same audience scoping as
the in-app views (a byo_only claim owned by A must not appear in B's export);
CSV is a real download with honest headers even when empty; the claim export
is per entity and capped, never a silent full-store dump.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from neutron.test import TestClient
from test_api_coverage import _Lifespan

from omni.api.export import build_router
from omni.main import create_app


async def _user(db) -> uuid4:
    uid = uuid4()
    return await db.pool.fetchval(
        "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'x') "
        "RETURNING id",
        uid, f"export-{uid.hex}@example.com",
    )


def _auth(user_id):
    import os

    from neutron.auth.jwt import create_token

    os.environ.setdefault("OMNI_JWT_SECRET", "t" * 48)
    token = create_token({"sub": str(user_id)}, os.environ["OMNI_JWT_SECRET"])
    return {"Authorization": f"Bearer {token}"}


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company', $1, $1) "
        "RETURNING id",
        symbol,
    )


async def _claim(
    db, entity_id, *, value, owner=None, source="src",
    knowledge_date=None,
):
    now = datetime.now(UTC)
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1, 'price_snapshot', 'T', $2::jsonb, $3, $4, $5, 0.9,
                $6, $7)
        RETURNING id
        """,
        entity_id, json.dumps(value), source,
        now - timedelta(days=1), knowledge_date or now,
        "allowed" if owner is None else "byo_only", owner,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, users CASCADE")
    yield


def _make_app(database_url):
    app = create_app(database_url)
    app.include_router(build_router(app))
    return app


class TestHoldingsExport:
    async def test_csv_is_a_download_with_real_rows(self, db, database_url):
        user = await _user(db)
        await db.pool.execute(
            "INSERT INTO manual_holding (user_id, symbol, quantity, currency) "
            "VALUES ($1, 'AAPL', 10, 'USD')",
            user,
        )
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.get("/export/holdings", headers=_auth(user))
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        assert r.headers["content-type"].startswith("text/csv")
        assert "AAPL" in r.text
        assert "symbol" in r.text

    async def test_empty_export_is_a_header_only_file_not_an_error(
        self, db, database_url
    ):
        user = await _user(db)
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.get("/export/holdings", headers=_auth(user))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "symbol" in r.text  # the header documents the shape

    async def test_anonymous_is_refused(self, db, database_url):
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.get("/export/holdings")
        assert r.status_code == 401


class TestClaimsExport:
    async def test_byo_claims_of_another_user_never_export(
        self, db, database_url
    ):
        owner = await _user(db)
        reader = await _user(db)
        entity = await _entity(db)
        await _claim(db, entity, value={"value": 100}, owner=owner)

        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.get(
                f"/export/claims/{entity}", headers=_auth(reader)
            )
        assert r.status_code == 200
        assert "100" not in r.text  # the private claim did not leak

            # The owner's own export does contain it.
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.get(
                f"/export/claims/{entity}", headers=_auth(owner)
            )
        assert "100" in r.text

    async def test_json_format_carries_object_values(self, db, database_url):
        user = await _user(db)
        entity = await _entity(db)
        await _claim(db, entity, value={"value": 42.5})
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.get(
                f"/export/claims/{entity}?format=json", headers=_auth(user)
            )
        body = json.loads(r.headers and r.text)
        assert body["claims"][0]["value"] == '{"value": 42.5}'

    async def test_unknown_entity_is_404(self, db, database_url):
        user = await _user(db)
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.get(
                f"/export/claims/{uuid4()}", headers=_auth(user)
            )
        assert r.status_code == 404


class TestScorecardExport:
    async def test_csv_or_json_for_an_authenticated_caller(
        self, db, database_url
    ):
        user = await _user(db)
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            csv_r = await client.get(
                "/export/scorecard", headers=_auth(user)
            )
            json_r = await client.get(
                "/export/scorecard?format=json", headers=_auth(user)
            )
        assert csv_r.status_code == 200
        assert "method" in csv_r.text
        assert json_r.status_code == 200
        assert "scorecard" in json_r.text
