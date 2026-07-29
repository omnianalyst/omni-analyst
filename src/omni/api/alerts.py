"""HTTP CRUD over alerts, plus a fired-history endpoint.

An alert is private to its owner the way a watchlist is: an unauthenticated
caller cannot create, read or fire one, so every endpoint resolves the audience
from a verified token and refuses with 401 when there is none. A caller who
names another user's alert gets a 404 rather than a 403, for the same reason
watchlist does: confirming "that alert exists but is not yours" lets a caller
enumerate other users' attention.

The condition is validated against the closed set in omni.alerts.rules at
creation and at every update; an unknown kind is a 400 here, never a silent
never-fire row. This router does not evaluate alerts -- evaluation reads
coverage and writes firings, and belongs to a scheduler. It only exposes the
firings an evaluation has recorded.

The router closes over the Neutron ``App`` for ``app.db``, the same closure
trick the coverage and watchlist routers use, because the inner Starlette
request has no path back to the App or its pool.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from neutron import App, Router
from neutron.error import bad_request, not_found, unauthorized
from pydantic import BaseModel
from starlette.requests import Request

from omni.alerts.rules import InvalidCondition, validate_condition
from omni.auth import resolve_audience_from_request


def _require_user(request: Request) -> UUID:
    """The caller's user id from a verified token, or a 401.

    An alert has no anonymous case: ``None`` means the caller is nobody, and
    nobody owns an alert.
    """
    user = resolve_audience_from_request(request)
    if user is None:
        raise unauthorized("Authentication required")
    return user


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _loads_jsonb(value) -> object:
    """asyncpg returns jsonb columns as text unless a codec is set; decode here
    so the API speaks JSON, not a serialized string."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


async def _claim_type_or_400(pool, value: str) -> str:
    """Validate a claim_type against the DB enum, or 400.

    The enum is spread across migrations 001 and 003 and will drift, so the set
    is not duplicated here: a scalar cast is the source of truth. An unknown
    label is a 400, not the 500 a failed INSERT cast would yield.
    """
    try:
        await pool.fetchval("SELECT $1::claim_type", value)
    except Exception as exc:  # asyncpg raises on a bad enum label
        raise bad_request(f"Unknown claim_type: {value}") from exc
    return value


def _condition_or_400(value: dict | None) -> str:
    """Validate a condition and return it as a JSON string for the ::jsonb cast.

    An unknown or malformed kind is a 400 at the gate, so only conditions in the
    closed set are ever stored.
    """
    if value is None:
        raise bad_request("condition is required")
    try:
        return json.dumps(validate_condition(value))
    except InvalidCondition as exc:
        raise bad_request(str(exc)) from exc


_COLS = (
    "id, user_id, entity_id, claim_type::text AS claim_type, condition, "
    "active, created_at, last_fired_at"
)


async def _owned_alert(pool, alert_id: UUID, user_id: UUID):
    """An alert row, or None if it does not exist or is not this user's.

    Ownership is in the WHERE, not in application code, so a non-owner is
    indistinguishable from a missing id -- both become a 404 at the call site.
    """
    return await pool.fetchrow(
        f"SELECT {_COLS} FROM alert WHERE id = $1 AND user_id = $2",
        alert_id,
        user_id,
    )


def _serialize(row) -> dict:
    return {
        "id": str(row["id"]),
        "user_id": str(row["user_id"]),
        "entity_id": str(row["entity_id"]),
        "claim_type": row["claim_type"],
        "condition": _loads_jsonb(row["condition"]),
        "active": row["active"],
        "created_at": _iso(row["created_at"]),
        "last_fired_at": _iso(row["last_fired_at"]),
    }


class CreateAlertIn(BaseModel):
    entity_id: UUID
    claim_type: str
    condition: dict


class UpdateAlertIn(BaseModel):
    active: bool | None = None
    condition: dict | None = None


def build_router(app: App) -> Router:
    router = Router()

    @router.post("/alerts")
    async def create_alert(body: CreateAlertIn, request: Request) -> dict:
        user = _require_user(request)
        condition = _condition_or_400(body.condition)
        claim_type = await _claim_type_or_400(app.db.pool, body.claim_type)
        exists = await app.db.pool.fetchval("SELECT 1 FROM entity WHERE id = $1", body.entity_id)
        if not exists:
            raise not_found(f"No entity {body.entity_id}")
        row = await app.db.pool.fetchrow(
            "INSERT INTO alert (user_id, entity_id, claim_type, condition) "
            "VALUES ($1, $2, $3::claim_type, $4::jsonb) "
            f"RETURNING {_COLS}",
            user,
            body.entity_id,
            claim_type,
            condition,
        )
        return _serialize(row)

    @router.get("/alerts")
    async def list_alerts(request: Request) -> dict:
        user = _require_user(request)
        rows = await app.db.pool.fetch(
            f"SELECT {_COLS} FROM alert WHERE user_id = $1 ORDER BY created_at",
            user,
        )
        return {"alerts": [_serialize(r) for r in rows]}

    @router.get("/alerts/{alert_id}")
    async def get_alert(alert_id: UUID, request: Request) -> dict:
        user = _require_user(request)
        row = await _owned_alert(app.db.pool, alert_id, user)
        if row is None:
            raise not_found("Alert not found")
        return _serialize(row)

    @router.patch("/alerts/{alert_id}")
    async def update_alert(alert_id: UUID, body: UpdateAlertIn, request: Request) -> dict:
        user = _require_user(request)
        row = await _owned_alert(app.db.pool, alert_id, user)
        if row is None:
            raise not_found("Alert not found")

        sets: list[str] = []
        params: list = []
        if body.active is not None:
            params.append(body.active)
            sets.append(f"active = ${len(params)}")
        if body.condition is not None:
            # Re-validate on update: a PATCH that weakened the closed set would
            # let an arbitrary shape sit where evaluate expects a known one.
            params.append(_condition_or_400(body.condition))
            sets.append(f"condition = ${len(params)}::jsonb")
        if not sets:
            return _serialize(row)

        params.append(alert_id)
        params.append(user)
        id_idx = len(params) - 1
        user_idx = len(params)
        updated = await app.db.pool.fetchrow(
            "UPDATE alert SET " + ", ".join(sets) + " "
            f"WHERE id = ${id_idx} AND user_id = ${user_idx} "
            f"RETURNING {_COLS}",
            *params,
        )
        return _serialize(updated)

    @router.delete("/alerts/{alert_id}")
    async def delete_alert(alert_id: UUID, request: Request) -> dict:
        user = _require_user(request)
        status = await app.db.pool.execute(
            "DELETE FROM alert WHERE id = $1 AND user_id = $2",
            alert_id,
            user,
        )
        if not status.endswith("1"):
            raise not_found("Alert not found")
        return {"deleted": True}

    @router.get("/alerts/{alert_id}/firings")
    async def list_firings(alert_id: UUID, request: Request) -> dict:
        user = _require_user(request)
        rows = await app.db.pool.fetch(
            """
            SELECT f.claim_id, f.fired_at, c.key, c.event_date, c.value,
                   c.claim_type::text AS claim_type, c.source
            FROM alert_firing f
            JOIN claim c ON c.id = f.claim_id
            JOIN alert a ON a.id = f.alert_id
            WHERE f.alert_id = $1 AND a.user_id = $2
            ORDER BY f.fired_at DESC
            """,
            alert_id,
            user,
        )
        return {
            "alert_id": str(alert_id),
            "firings": [
                {
                    "claim_id": str(r["claim_id"]),
                    "fired_at": _iso(r["fired_at"]),
                    "claim_type": r["claim_type"],
                    "key": r["key"],
                    "event_date": _iso(r["event_date"]),
                    "value": _loads_jsonb(r["value"]),
                    "source": r["source"],
                }
                for r in rows
            ],
        }

    return router


__all__ = ["build_router"]
