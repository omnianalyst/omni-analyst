import asyncio

from neutron.test import TestClient

from omni.db import migrate
from omni.main import create_app


async def test_connects_over_the_postgres_wire_protocol(db):
    assert await db.pool.fetchval("SELECT 1") == 1


async def test_nucleus_detection_reports_a_real_backend(db):
    assert db.features is not None
    assert isinstance(db.features.is_nucleus, bool)


async def test_migrations_are_idempotent(db):
    assert await migrate(db) == []


class _Lifespan:
    """Drive the ASGI lifespan protocol, which httpx's ASGITransport skips."""

    def __init__(self, app):
        self._app = app
        self._receive = asyncio.Queue()
        self._send = asyncio.Queue()
        self._task = None

    async def __aenter__(self):
        self._task = asyncio.create_task(
            self._app(
                {"type": "lifespan"},
                self._receive.get,
                self._send.put,
            )
        )
        await self._receive.put({"type": "lifespan.startup"})
        message = await self._send.get()
        assert message["type"] == "lifespan.startup.complete", message
        return self._app

    async def __aexit__(self, *exc):
        await self._receive.put({"type": "lifespan.shutdown"})
        await self._send.get()
        await self._task


async def test_app_boots_and_serves_health(database_url):
    app = create_app(database_url)
    async with _Lifespan(app):
        assert app.db is not None
        async with TestClient(app) as client:
            response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == "0.1.0"


async def test_lifespan_releases_the_connection_pool(database_url):
    app = create_app(database_url)
    async with _Lifespan(app):
        pass
    assert app.db is None
