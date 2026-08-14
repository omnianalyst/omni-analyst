from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from functools import wraps
from typing import Any
from uuid import UUID

from neutron import App
from neutron.ai.mcp import MCPServer
from neutron.error import forbidden, unauthorized
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from omni.capability.extracted import CLAIM_TYPES
from omni.coverage.visibility import visible_claims_cte
from omni.research.publish import read_history, summarise

TOOL_ALLOWLIST = (
    "search_entities",
    "get_entity_coverage",
    "get_system_health",
    "get_research_record",
)

_audience: ContextVar[UUID | None] = ContextVar("omni_mcp_audience", default=None)


def _current_audience() -> UUID:
    audience = _audience.get()
    if audience is None:
        raise RuntimeError("MCP operator context is unavailable")
    return audience


def _limit(value: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("limit must be an integer")
    return max(1, min(value, maximum))


def _jsonb(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _safe_tool(fn: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
    @wraps(fn)
    async def wrapped(*args: Any, **kwargs: Any) -> dict:
        try:
            return await fn(*args, **kwargs)
        except (TypeError, ValueError):
            raise
        except Exception as exc:  # Neutron otherwise returns raw exception details.
            raise RuntimeError("Tool data is unavailable") from exc

    return wrapped


class _OmniMCPServer(MCPServer):
    async def _handle_root(self, request) -> JSONResponse:
        return JSONResponse(
            {
                "name": self.name,
                "version": self.version,
                "protocol": "mcp",
                "capabilities": {"tools": True, "resources": False},
                "authentication": {
                    "type": "bearer",
                    "required_role": "operator",
                    "header": "Authorization: Bearer <token>",
                    "setup_status_endpoint": "/auth/setup-status",
                    "first_run_setup_endpoint": "/auth/setup",
                    "token_endpoint": "/auth/login",
                },
                "tool_policy": {
                    "allowlist": list(TOOL_ALLOWLIST),
                    "read_only": True,
                    "audience": "active operator from the bearer token; not caller-selectable",
                    "data": "stored observations only; missing data remains missing",
                    "excluded": [
                        "trading",
                        "orders",
                        "credentials",
                        "secrets",
                        "private_keys",
                    ],
                },
            }
        )


class _OperatorOnlyMCP:
    def __init__(self, server: MCPServer) -> None:
        self.server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.server(scope, receive, send)
            return

        state = scope.get("state", {})
        audience = state.get("_omni_audience")
        if audience is None:
            response = unauthorized("Authentication required").to_response(scope.get("path"))
            response.headers["WWW-Authenticate"] = "Bearer"
            await response(scope, receive, send)
            return
        if state.get("_omni_role") != "operator":
            response = forbidden("Operator access required").to_response(scope.get("path"))
            await response(scope, receive, send)
            return

        token = _audience.set(audience)
        try:
            await self.server(scope, receive, send)
        finally:
            _audience.reset(token)


class _MCPMount:
    def __init__(self, server: _OperatorOnlyMCP) -> None:
        self.server = server

    def get_routes(self, prefix: str = "") -> list[Mount]:
        return [Mount(f"{prefix}/mcp", app=self.server)]

    def get_handler_info(self, prefix: str = "") -> list[dict[str, Any]]:
        return []


def build_mount(app: App) -> _MCPMount:
    server = _OmniMCPServer(name="omni-analyst", version="0.1.0")

    @server.tool()
    @_safe_tool
    async def search_entities(query: str, limit: int = 20) -> dict:
        """Search stored entities by symbol or name. Read-only."""
        if not isinstance(query, str) or len(query) > 100:
            raise ValueError("query must be a string of at most 100 characters")
        effective_limit = _limit(limit, 25)
        rows = await app.db.pool.fetch(
            """
            SELECT id, kind, symbol, name
            FROM entity
            WHERE symbol ILIKE $1 OR name ILIKE $1
            ORDER BY symbol NULLS FIRST, name
            LIMIT $2
            """,
            f"%{query}%",
            effective_limit,
        )
        return {
            "query": query,
            "entities": [
                {
                    "id": str(row["id"]),
                    "kind": row["kind"],
                    "symbol": row["symbol"],
                    "name": row["name"],
                }
                for row in rows
            ],
        }

    @server.tool()
    @_safe_tool
    async def get_entity_coverage(
        entity_id: str,
        claim_type: str = "",
        key: str = "",
        limit: int = 100,
    ) -> dict:
        """Read stored claims visible to the authenticated operator. Read-only."""
        try:
            parsed_entity_id = UUID(entity_id)
        except (TypeError, ValueError):
            raise ValueError("entity_id must be a UUID") from None
        selected_type = claim_type or None
        if selected_type is not None and selected_type not in CLAIM_TYPES:
            raise ValueError(f"unknown claim_type: {selected_type}")
        if not isinstance(key, str) or len(key) > 200:
            raise ValueError("key must be a string of at most 200 characters")
        effective_limit = _limit(limit, 200)
        entity = await app.db.pool.fetchrow(
            "SELECT id, kind, symbol, name FROM entity WHERE id = $1",
            parsed_entity_id,
        )
        if entity is None:
            return {"entity": None, "claims": []}
        conditions = ["v.entity_id = $2"]
        params: list[Any] = [_current_audience(), parsed_entity_id]
        if selected_type is not None:
            params.append(selected_type)
            conditions.append(f"v.claim_type = ${len(params)}::claim_type")
        if key:
            params.append(key)
            conditions.append(f"v.key = ${len(params)}")
        params.append(effective_limit)
        claims = await app.db.pool.fetch(
            f"""
            SELECT v.id, v.claim_type, v.key, v.value, v.unit, v.source,
                   v.event_date, v.knowledge_date, v.confidence,
                   v.redistributable
            FROM ({visible_claims_cte("$1")}) v
            WHERE {" AND ".join(conditions)}
            ORDER BY v.knowledge_date DESC, v.event_date DESC
            LIMIT ${len(params)}
            """,
            *params,
        )
        return {
            "entity": {
                "id": str(entity["id"]),
                "kind": entity["kind"],
                "symbol": entity["symbol"],
                "name": entity["name"],
            },
            "claims": [
                {
                    "id": str(row["id"]),
                    "claim_type": str(row["claim_type"]),
                    "key": row["key"],
                    "value": _jsonb(row["value"]),
                    "unit": row["unit"],
                    "source": row["source"],
                    "event_date": row["event_date"].isoformat(),
                    "knowledge_date": row["knowledge_date"].isoformat(),
                    "confidence": row["confidence"],
                    "redistributable": str(row["redistributable"]),
                }
                for row in claims
            ],
        }

    @server.tool()
    @_safe_tool
    async def get_system_health(limit: int = 50) -> dict:
        """Read recorded scheduler loop health without changing system state."""
        effective_limit = _limit(limit, 100)
        rows = await app.db.pool.fetch(
            """
            SELECT loop_name, last_success_at, last_failure_at,
                   consecutive_failures, expected_interval_seconds
            FROM loop_health
            ORDER BY loop_name
            LIMIT $1
            """,
            effective_limit,
        )
        return {
            "loops": [
                {
                    "loop": row["loop_name"],
                    "last_success_at": (
                        row["last_success_at"].isoformat()
                        if row["last_success_at"] is not None
                        else None
                    ),
                    "last_failure_at": (
                        row["last_failure_at"].isoformat()
                        if row["last_failure_at"] is not None
                        else None
                    ),
                    "consecutive_failures": int(row["consecutive_failures"]),
                    "expected_interval_seconds": row["expected_interval_seconds"],
                }
                for row in rows
            ]
        }

    @server.tool()
    @_safe_tool
    async def get_research_record(limit: int = 50) -> dict:
        """Read the mirrored hypothesis record and its canonical summary."""
        effective_limit = _limit(limit, 100)
        history = await read_history(app.db.pool)
        return {"summary": summarise(history), "tests": history[:effective_limit]}

    if tuple(server._tools) != TOOL_ALLOWLIST:
        raise RuntimeError("Omni MCP tools do not match the explicit allowlist")
    return _MCPMount(_OperatorOnlyMCP(server))


__all__ = ["TOOL_ALLOWLIST", "build_mount"]
