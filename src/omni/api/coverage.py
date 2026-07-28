"""HTTP read API over coverage.

Every claim this API returns comes through omni.coverage.visibility -- an
endpoint that queries ``claim`` directly is a data leak with a URL. The router
closes over the Neutron ``App`` to reach ``app.db`` (the NucleusClient): the
request a handler receives belongs to the inner Starlette app, which has no path
back to the App or its db (recorded in the X2 report). That is the same closure
trick the framework's own health endpoint uses.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from neutron import App, Query, Router
from neutron.error import bad_request, not_found
from pydantic import BaseModel
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.coverage.visibility import visible_claims_cte

# Mirrors the claim_type enum in migrations/001_core_schema.sql. Kept here so an
# unknown value is a 400, not a 500 from a failed SQL cast.
_CLAIM_TYPES = {
    "price_snapshot",
    "fundamental_metric",
    "filing_event",
    "macro_series_point",
    "news_event",
    "manipulation_signal",
}

PAGE_SIZE_DEFAULT = 100
PAGE_SIZE_MAX = 1000


class ClaimsQuery(BaseModel):
    claim_type: str | None = None
    #: The series or concept, e.g. "GDP" or "Revenues". Without it, asking
    #: "what was GDP for Q1 1994 as of 1996" means paging through every other
    #: claim the entity holds — the answer sat at rank 210 of a 100-row page.
    key: str | None = None
    as_of: datetime | None = None
    limit: int = PAGE_SIZE_DEFAULT
    # Keyset cursor: the (knowledge_date, id) of the last row of the previous
    # page. OFFSET on this table is not indexed, so callers page forward with it.
    before_knowledge_date: datetime | None = None
    before_id: UUID | None = None


def _audience(request: Request) -> UUID | None:
    """Who is asking, from a verified token — never from a header.

    This read X-User-Id, so any caller could name any user and read their
    licensed claims. The store's constraints were sound and the identity
    in front of them was a claim. An absent or invalid token is an
    anonymous caller, which means shared coverage only.
    """
    return resolve_audience_from_request(request)


def _claim_type_or_400(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in _CLAIM_TYPES:
        raise bad_request(f"Unknown claim_type: {value}")
    return value


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _jsonb(value: Any) -> Any:
    """asyncpg returns jsonb columns as their text form unless a codec is set;
    decode here so the API speaks JSON, not serialized strings."""
    if isinstance(value, str):
        return json.loads(value)
    return value


async def _entity_exists(pool, entity_id: UUID) -> bool:
    return await pool.fetchval(
        "SELECT 1 FROM entity WHERE id = $1", entity_id
    ) is not None


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/coverage/{entity_id}")
    async def coverage_summary(entity_id: UUID, request: Request) -> dict:
        """Coverage grouped by claim_type, each group reporting its freshness."""
        if not await _entity_exists(app.db.pool, entity_id):
            raise not_found(f"No entity {entity_id}")
        audience = _audience(request)
        sql = f"""
            SELECT v.claim_type::text AS claim_type,
                   count(*)::int                       AS count,
                   max(v.knowledge_date)               AS newest_knowledge_date,
                   extract(epoch FROM (now() - max(v.knowledge_date))
                           )::double precision         AS age_seconds,
                   count(DISTINCT v.source)::int       AS source_count,
                   coalesce(
                       array_agg(DISTINCT v.source ORDER BY v.source)
                       FILTER (WHERE v.source IS NOT NULL),
                       ARRAY[]::text[]
                   )                                   AS sources,
                   avg(v.confidence)                   AS mean_confidence
            FROM ({visible_claims_cte("$1")}) v
            WHERE v.entity_id = $2
            GROUP BY v.claim_type
            ORDER BY v.claim_type
        """
        rows = await app.db.pool.fetch(sql, audience, entity_id)
        groups = [
            {
                "claim_type": r["claim_type"],
                "count": r["count"],
                "newest_knowledge_date": _iso(r["newest_knowledge_date"]),
                "age_seconds": r["age_seconds"],
                "source_count": r["source_count"],
                "sources": list(r["sources"]),
                "mean_confidence": r["mean_confidence"],
            }
            for r in rows
        ]
        return {"entity_id": str(entity_id), "groups": groups}

    @router.get("/coverage/{entity_id}/claims")
    async def list_claims(
        entity_id: UUID, request: Request, query: Query[ClaimsQuery]
    ) -> dict:
        """The claims themselves, filterable and point-in-time queryable."""
        if not await _entity_exists(app.db.pool, entity_id):
            raise not_found(f"No entity {entity_id}")
        audience = _audience(request)
        claim_type = _claim_type_or_400(query.claim_type)
        limit = max(1, min(query.limit, PAGE_SIZE_MAX))

        before_kd = query.before_knowledge_date
        before_id = query.before_id
        if (before_kd is None) != (before_id is None):
            raise bad_request(
                "before_knowledge_date and before_id must be provided together"
            )

        cols = (
            "v.id, v.claim_type::text AS claim_type, v.key, v.value, v.unit, "
            "v.source, v.event_date, v.knowledge_date, v.confidence, "
            "v.redistributable::text AS redistributable"
        )
        visible = f"({visible_claims_cte('$1')}) v"

        if query.as_of is not None:
            # Point-in-time: what was knowable at as_of, one vintage per period.
            # "Period" is (claim_type, key, event_date); the latest knowable
            # knowledge_date for each wins, which is what a backtest needs.
            conditions = ["v.entity_id = $2", "v.knowledge_date <= $3"]
            params: list[Any] = [audience, entity_id, query.as_of]
            if claim_type is not None:
                conditions.append(f"v.claim_type = ${len(params) + 1}::claim_type")
                params.append(claim_type)
            if query.key is not None:
                conditions.append(f"v.key = ${len(params) + 1}")
                params.append(query.key)
            sql = f"""
                WITH ranked AS (
                    SELECT {cols}, ROW_NUMBER() OVER (
                        PARTITION BY v.claim_type, v.key, v.event_date
                        ORDER BY v.knowledge_date DESC, v.id DESC
                    ) AS rn
                    FROM {visible}
                    WHERE {" AND ".join(conditions)}
                )
                SELECT * FROM ranked
                WHERE rn = 1
                ORDER BY knowledge_date DESC, id
                LIMIT ${len(params) + 1}
            """
        else:
            conditions = ["v.entity_id = $2"]
            params = [audience, entity_id]
            if claim_type is not None:
                conditions.append(f"v.claim_type = ${len(params) + 1}::claim_type")
                params.append(claim_type)
            if query.key is not None:
                conditions.append(f"v.key = ${len(params) + 1}")
                params.append(query.key)
            if before_kd is not None:
                conditions.append(
                    f"(v.knowledge_date, v.id) < (${len(params) + 1},"
                    f" ${len(params) + 2})"
                )
                params.extend([before_kd, before_id])
            sql = (
                f"SELECT {cols} FROM {visible} "
                f"WHERE {' AND '.join(conditions)} "
                f"ORDER BY v.knowledge_date DESC, v.id "
                f"LIMIT ${len(params) + 1}"
            )
        params.append(limit)

        rows = await app.db.pool.fetch(sql, *params)
        claims = [
            {
                "id": str(r["id"]),
                "claim_type": r["claim_type"],
                "key": r["key"],
                "value": _jsonb(r["value"]),
                "unit": r["unit"],
                "source": r["source"],
                "event_date": _iso(r["event_date"]),
                "knowledge_date": _iso(r["knowledge_date"]),
                "confidence": r["confidence"],
                "redistributable": r["redistributable"],
            }
            for r in rows
        ]
        return {"entity_id": str(entity_id), "limit": limit, "claims": claims}

    @router.get("/entities")
    async def search_entities(q: str = "") -> dict:
        pattern = f"%{q}%"
        rows = await app.db.pool.fetch(
            """
            SELECT id, kind, symbol, name
            FROM entity
            WHERE symbol ILIKE $1 OR name ILIKE $1
            ORDER BY symbol NULLS FIRST, name
            LIMIT 50
            """,
            pattern,
        )
        entities = [
            {
                "id": str(r["id"]),
                "kind": r["kind"],
                "symbol": r["symbol"],
                "name": r["name"],
            }
            for r in rows
        ]
        return {"query": q, "entities": entities}

    @router.get("/gaps/{entity_id}")
    async def list_gaps(entity_id: UUID, request: Request) -> dict:
        """Open gaps, scoped to the audience for the same reason claims are."""
        if not await _entity_exists(app.db.pool, entity_id):
            raise not_found(f"No entity {entity_id}")
        audience = _audience(request)
        rows = await app.db.pool.fetch(
            """
            SELECT id, claim_type::text AS claim_type, key,
                   gap_class::text AS gap_class, audience_user_id,
                   score, attempts, detail, detected_at
            FROM gap
            WHERE entity_id = $1 AND resolved_at IS NULL
              AND (audience_user_id IS NULL OR audience_user_id = $2)
            ORDER BY score DESC, detected_at
            LIMIT 500
            """,
            entity_id,
            audience,
        )
        gaps = [
            {
                "id": str(r["id"]),
                "claim_type": r["claim_type"],
                "key": r["key"],
                "gap_class": r["gap_class"],
                "audience_user_id": (
                    str(r["audience_user_id"]) if r["audience_user_id"] else None
                ),
                "score": r["score"],
                "attempts": r["attempts"],
                "detail": _jsonb(r["detail"]),
                "detected_at": _iso(r["detected_at"]),
            }
            for r in rows
        ]
        return {"entity_id": str(entity_id), "gaps": gaps}

    return router
