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
from neutron.error import bad_request, not_found, unauthorized
from pydantic import BaseModel
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.capability.extracted import CLAIM_TYPES
from omni.coverage.visibility import visible_claims_cte

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
    # Validate against omni.capability.extracted.CLAIM_TYPES, the canonical
    # Python mirror of the claim_type enum that
    # test_claim_types_frozenset_mirrors_the_migration_enum pins to the
    # migrations. A local copy here drifted to 6 values while the enum grew to
    # 19, so every earned claim type 400'd. One source, not three.
    if value is None:
        return None
    if value not in CLAIM_TYPES:
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


async def _entity_summary(pool, entity_id: UUID):
    return await pool.fetchrow(
        "SELECT id, kind, symbol, name FROM entity WHERE id = $1",
        entity_id,
    )


class CreateEntityIn(BaseModel):
    symbol: str
    kind: str
    name: str | None = None


# The kinds a user may will into existence. The seeded universe is deliberate
# (companies, sector ETFs, indices); anything outside it should enter because
# someone asked to track it, not by accident. A free-form kind would let a
# typo mint a new asset class.
_USER_CREATABLE_KINDS = frozenset({"company", "etf", "crypto_asset"})

_EXISTING_ACTIVE_DEMAND = """
SELECT 1 FROM demand
WHERE entity_id = $1 AND claim_type::text = $2 AND channel = 'direct'
  AND requested_by = $3 AND active
LIMIT 1
"""


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/coverage/{entity_id}")
    async def coverage_summary(entity_id: UUID, request: Request) -> dict:
        """Coverage grouped by claim_type, each group reporting its freshness."""
        entity = await _entity_summary(app.db.pool, entity_id)
        if entity is None:
            raise not_found(f"No entity {entity_id}")
        audience = _audience(request)
        sql = f"""
            SELECT v.claim_type::text AS claim_type,
                   count(*)::int                       AS count,
                   -- visible_claims_cte returns shared claims (audience_user_id
                   -- IS NULL) plus this viewer's own BYO claims (audience_user_id
                   -- = the audience). A non-null audience_user_id in the result
                   -- is therefore exactly "the viewer's private claim"; for an
                   -- anonymous caller the CTE returns none, so private_count is 0.
                   (count(*) FILTER (
                       WHERE v.audience_user_id IS NOT NULL
                   ))::int                             AS private_count,
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
                "private_count": r["private_count"],
                "newest_knowledge_date": _iso(r["newest_knowledge_date"]),
                "age_seconds": r["age_seconds"],
                "source_count": r["source_count"],
                "sources": list(r["sources"]),
                "mean_confidence": r["mean_confidence"],
            }
            for r in rows
        ]
        return {
            "entity_id": str(entity_id),
            "entity": {
                "id": str(entity["id"]),
                "kind": entity["kind"],
                "symbol": entity["symbol"],
                "name": entity["name"],
            },
            "groups": groups,
        }

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

    @router.post("/entities")
    async def create_entity(body: CreateEntityIn, request: Request) -> dict:
        """Create an entity outside the seeded universe and demand its coverage.

        This is the demand-driven path for a ticker search that honestly returned
        nothing (a thematic ETF like BOTZ, or the ETF sharing a token's name). The
        entity is created with the caller's spelling and attention; nothing about
        it is fabricated -- if the ticker does not exist at any provider, the fill
        attempts record `unfillable` with the reason, which is the correct and
        visible outcome. Authentication is required because demand without an
        owner cannot fill byo_only sources.
        """
        user = resolve_audience_from_request(request)
        if user is None:
            raise unauthorized("Authentication required")

        symbol = body.symbol.strip().upper()
        if not symbol or len(symbol) > 12:
            raise bad_request("symbol must be 1-12 characters")
        if body.kind not in _USER_CREATABLE_KINDS:
            raise bad_request(
                f"kind must be one of {sorted(_USER_CREATABLE_KINDS)}"
            )
        name = (body.name or "").strip() or symbol

        from omni.demand.ledger import direct_attention
        from omni.watchlist.lists import claim_types_for_kind

        async with app.db.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO entity (kind, symbol, name)
                VALUES ($1, $2, $3)
                ON CONFLICT (kind, symbol) DO UPDATE SET name = entity.name
                RETURNING id, kind, symbol, name, created_at
                """,
                body.kind, symbol, name,
            )
            # Demand is raised on every call only when this user has no active
            # row for it -- two users tracking the same name are two rows (the
            # ledger's own weight rule), but one user re-confirming a track is
            # not a second unit of demand.
            for claim_type in claim_types_for_kind(body.kind):
                existing = await conn.fetchval(
                    _EXISTING_ACTIVE_DEMAND, row["id"], claim_type, user,
                )
                if existing is None:
                    await direct_attention(
                        conn,
                        entity_id=row["id"],
                        claim_type=claim_type,
                        key=None,
                        requested_by=user,
                    )

        return {
            "id": str(row["id"]),
            "kind": row["kind"],
            "symbol": row["symbol"],
            "name": row["name"],
            "created_at": row["created_at"].isoformat(),
        }


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
