import asyncio
from datetime import UTC, datetime

import pytest
from neutron.test import TestClient

from omni.api.mcp import TOOL_ALLOWLIST
from omni.main import create_app

GOOD_SECRET = "m" * 48


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
def _secret(monkeypatch):
    monkeypatch.setenv("OMNI_JWT_SECRET", GOOD_SECRET)


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users, entity, loop_health, hypothesis_test CASCADE")


async def _setup(client):
    response = await client.post(
        "/auth/setup",
        json={"email": "operator@example.com", "password": "a" * 16},
    )
    assert response.status_code == 200, response.text
    return {
        "id": response.json()["user"]["id"],
        "headers": {"authorization": f"Bearer {response.json()['token']}"},
    }


async def _member(client, operator_headers):
    created = await client.post(
        "/auth/register",
        json={"email": "member@example.com", "password": "b" * 16},
        headers=operator_headers,
    )
    assert created.status_code == 201, created.text
    login = await client.post(
        "/auth/login",
        json={"email": "member@example.com", "password": "b" * 16},
    )
    assert login.status_code == 200, login.text
    return {
        "id": created.json()["id"],
        "headers": {"authorization": f"Bearer {login.json()['token']}"},
    }


async def test_every_mcp_path_requires_an_active_operator(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        operator = await _setup(client)
        member = await _member(client, operator["headers"])

        for method, path in (
            ("GET", "/mcp/"),
            ("GET", "/mcp/tools"),
            ("GET", "/mcp/resources"),
            ("POST", "/mcp/tools/search_entities"),
        ):
            body = {} if method == "POST" else None
            anonymous = await client.request(method, path, json=body)
            denied = await client.request(
                method,
                path,
                headers=member["headers"],
                json=body,
            )
            assert anonymous.status_code == 401, path
            assert anonymous.headers["www-authenticate"] == "Bearer", path
            assert denied.status_code == 403, path

        await app.db.pool.execute(
            "UPDATE users SET active = FALSE WHERE id = $1", operator["id"]
        )
        inactive = await client.get("/mcp/tools", headers=operator["headers"])

    assert inactive.status_code == 401


async def test_capability_contract_and_tools_match_the_allowlist(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        operator = await _setup(client)
        info = await client.get("/mcp/", headers=operator["headers"])
        listed = await client.get("/mcp/tools", headers=operator["headers"])
        resources = await client.get("/mcp/resources", headers=operator["headers"])

    assert info.status_code == 200, info.text
    contract = info.json()
    assert contract["authentication"] == {
        "type": "bearer",
        "required_role": "operator",
        "header": "Authorization: Bearer <token>",
        "setup_status_endpoint": "/auth/setup-status",
        "first_run_setup_endpoint": "/auth/setup",
        "token_endpoint": "/auth/login",
    }
    assert contract["tool_policy"]["allowlist"] == list(TOOL_ALLOWLIST)
    assert contract["tool_policy"]["read_only"] is True
    assert contract["capabilities"] == {"tools": True, "resources": False}
    assert resources.json() == {"resources": []}

    tools = listed.json()["tools"]
    assert tuple(tool["name"] for tool in tools) == TOOL_ALLOWLIST
    assert all("audience" not in tool["inputSchema"]["properties"] for tool in tools)
    forbidden_names = ("trade", "order", "credential", "secret", "private_key")
    assert all(
        forbidden not in tool["name"]
        for tool in tools
        for forbidden in forbidden_names
    )


async def test_coverage_tool_binds_visibility_to_the_operator(database_url, db):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        operator = await _setup(client)
        member = await _member(client, operator["headers"])
        entity_id = await app.db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('company', 'BOUND', 'Audience Bound') RETURNING id"
        )
        now = datetime(2026, 8, 14, tzinfo=UTC)
        for source, value, owner in (
            ("shared", 1, None),
            ("operator_private", 2, operator["id"]),
            ("member_private", 3, member["id"]),
        ):
            await app.db.pool.execute(
                """
                INSERT INTO claim (
                    entity_id, claim_type, key, value, source, event_date,
                    knowledge_date, confidence, redistributable, audience_user_id
                ) VALUES (
                    $1, 'fundamental_metric', 'Revenue', $2::jsonb, $3, $4, $4,
                    0.9, $5, $6
                )
                """,
                entity_id,
                str(value),
                source,
                now,
                "allowed" if owner is None else "byo_only",
                owner,
            )

        response = await client.post(
            "/mcp/tools/get_entity_coverage",
            headers=operator["headers"],
            json={"entity_id": str(entity_id)},
        )
        member_response = await client.post(
            "/mcp/tools/get_entity_coverage",
            headers=member["headers"],
            json={"entity_id": str(entity_id)},
        )

    assert response.status_code == 200, response.text
    claims = response.json()["result"]["claims"]
    assert {claim["source"] for claim in claims} == {"shared", "operator_private"}
    assert {claim["value"] for claim in claims} == {1, 2}
    assert member_response.status_code == 403


async def test_all_allowlisted_calls_leave_persisted_state_unchanged(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        operator = await _setup(client)
        entity_id = await app.db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('company', 'READ', 'Read Only') RETURNING id"
        )
        before = await app.db.pool.fetchval(
            """
            SELECT jsonb_build_object(
                'users', (SELECT count(*) FROM users),
                'entities', (SELECT count(*) FROM entity),
                'claims', (SELECT count(*) FROM claim),
                'demands', (SELECT count(*) FROM demand),
                'orders', (SELECT count(*) FROM trade_order)
            )
            """
        )

        calls = {
            "search_entities": {"query": "READ"},
            "get_entity_coverage": {"entity_id": str(entity_id)},
            "get_system_health": {},
            "get_research_record": {},
        }
        for tool in TOOL_ALLOWLIST:
            response = await client.post(
                f"/mcp/tools/{tool}",
                headers=operator["headers"],
                json=calls[tool],
            )
            assert response.status_code == 200, (tool, response.text)

        unknown = await client.post(
            "/mcp/tools/place_order",
            headers=operator["headers"],
            json={"symbol": "READ"},
        )
        after = await app.db.pool.fetchval(
            """
            SELECT jsonb_build_object(
                'users', (SELECT count(*) FROM users),
                'entities', (SELECT count(*) FROM entity),
                'claims', (SELECT count(*) FROM claim),
                'demands', (SELECT count(*) FROM demand),
                'orders', (SELECT count(*) FROM trade_order)
            )
            """
        )

    assert unknown.status_code == 404
    assert after == before
