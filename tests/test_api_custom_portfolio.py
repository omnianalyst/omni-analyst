"""HTTP contract for the custom mix comparator.

The arithmetic (_mix_history) has its own unit tests; these pin the endpoint's
promises: an anonymous caller is refused, positions are validated before any
expensive panel work, and a symbol the caller cannot measure is refused by
name rather than quietly dropped -- a comparison of a different portfolio
than the one asked about would be silently wrong.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

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
    from omni.api import scanner as mod
    mod._cache.clear()
    mod._panel_cache["prices"] = None
    mod._company_cache.clear()
    await db.pool.execute("TRUNCATE users CASCADE")
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield
    mod._cache.clear()
    mod._panel_cache["prices"] = None
    mod._company_cache.clear()


async def _setup(client):
    r = await client.post(
        "/auth/setup", json={"email": "operator@example.com", "password": "a" * 16}
    )
    assert r.status_code == 200, r.text
    return {"authorization": f"Bearer {r.json()['token']}"}


def _positions(*symbols):
    return {"positions": [{"symbol": symbol, "weight": 1} for symbol in symbols]}


async def test_an_anonymous_caller_is_refused_not_shrunk(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.post("/scanner/custom-portfolio", json=_positions("VTI", "GLD"))

    assert r.status_code == 401


async def test_positions_must_be_a_list_of_two_to_twelve(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        one = await client.post("/scanner/custom-portfolio", headers=headers,
                                json=_positions("VTI"))
        junk = await client.post("/scanner/custom-portfolio", headers=headers,
                                 json={"positions": "VTI"})
        thirteen = await client.post(
            "/scanner/custom-portfolio", headers=headers,
            json=_positions(*[f"S{i}" for i in range(13)]),
        )

    for response in (one, junk, thirteen):
        assert response.status_code == 400, response.text


async def test_a_non_json_body_is_a_400_not_a_500(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        r = await client.post("/scanner/custom-portfolio", headers=headers,
                              content=b"not json")

    assert r.status_code == 400


async def test_a_nonpositive_weight_is_refused_by_name(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        r = await client.post("/scanner/custom-portfolio", headers=headers,
                              json={"positions": [{"symbol": "VTI", "weight": 0},
                                                  {"symbol": "GLD", "weight": 1}]})

    assert r.status_code == 400
    assert "VTI" in r.json()["detail"]


async def test_an_unknown_symbol_is_refused_by_name_not_dropped(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        r = await client.post("/scanner/custom-portfolio", headers=headers,
                              json=_positions("VTI", "GLD", "NOTREAL"))

    assert r.status_code == 400
    assert "NOTREAL" in r.json()["detail"]
