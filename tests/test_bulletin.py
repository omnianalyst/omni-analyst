"""Header bulletin items are private and links cannot carry active schemes."""

import asyncio

import pytest
from neutron.test import TestClient

from omni.main import create_app


class _Lifespan:
    def __init__(self, app):
        self.app = app
        self.receive = asyncio.Queue()
        self.send = asyncio.Queue()

    async def __aenter__(self):
        self.task = asyncio.create_task(
            self.app({"type": "lifespan"}, self.receive.get, self.send.put)
        )
        await self.receive.put({"type": "lifespan.startup"})
        assert (await self.send.get())["type"] == "lifespan.startup.complete"
        return self.app

    async def __aexit__(self, *exc):
        await self.receive.put({"type": "lifespan.shutdown"})
        await self.send.get()
        await self.task


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


async def _setup(client):
    response = await client.post(
        "/auth/setup",
        json={"email": "operator@example.com", "password": "a" * 16},
    )
    assert response.status_code == 200, response.text
    return {"authorization": f"Bearer {response.json()['token']}"}


async def test_bulletin_requires_authentication(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        listed = await client.get("/bulletin")
        added = await client.post(
            "/bulletin", json={"kind": "note", "title": "Private"},
        )
    assert listed.status_code == 401
    assert added.status_code == 401


async def test_note_and_link_crud(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        note = await client.post(
            "/bulletin",
            headers=headers,
            json={"kind": "note", "title": "Check thesis", "body": "Review Friday"},
        )
        link = await client.post(
            "/bulletin",
            headers=headers,
            json={"kind": "link", "title": "Research", "url": "https://example.com/a"},
        )
        listed = await client.get("/bulletin", headers=headers)
        changed = await client.patch(
            f"/bulletin/{note.json()['id']}",
            headers=headers,
            json={"kind": "note", "title": "Updated", "body": "Done"},
        )
        removed = await client.delete(f"/bulletin/{link.json()['id']}", headers=headers)

    assert note.status_code == 201, note.text
    assert link.status_code == 201, link.text
    assert {item["title"] for item in listed.json()["items"]} == {"Check thesis", "Research"}
    assert changed.json()["title"] == "Updated"
    assert removed.json() == {"removed": True}


async def test_active_and_incomplete_link_schemes_are_refused(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        javascript = await client.post(
            "/bulletin",
            headers=headers,
            json={"kind": "link", "title": "Bad", "url": "javascript:alert(1)"},
        )
        incomplete = await client.post(
            "/bulletin",
            headers=headers,
            json={"kind": "link", "title": "Bad", "url": "example.com"},
        )
    assert javascript.status_code == 400
    assert incomplete.status_code == 400
