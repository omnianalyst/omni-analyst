"""HTTP CRUD over watchlists.

Unlike coverage, a watchlist is nobody's shared asset. An unauthenticated
caller cannot read or write one at all: every endpoint resolves the audience
from a verified token and refuses with 401 when there is none. Coverage serves
the shared network to anonymous callers; a watchlist is a private list of
demand, so anonymous access would be either a leak or a no-op, and a 401 is the
honest answer either way.

The router closes over the Neutron ``App`` for ``app.db``, the same closure
trick the coverage and briefing routers use, because the inner Starlette
request has no path back to the App or its pool.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from neutron import App, Router
from neutron.error import not_found, unauthorized
from pydantic import BaseModel
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.watchlist import lists as wl


def _require_user(request: Request) -> UUID:
    """The caller's user id from a verified token, or a 401.

    A watchlist has no anonymous case. This is the one place that differs from
    the coverage router, where ``None`` means the shared network: here ``None``
    means the caller is nobody, and nobody owns a watchlist.
    """
    user = resolve_audience_from_request(request)
    if user is None:
        raise unauthorized("Authentication required")
    return user


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _change_30d(latest, month_ago) -> float | None:
    """Fractional change over ~30 days, or None when either side is missing.

    A missing base is not a zero -- dividing by an absent price would report a
    nonsense change for freshly tracked names. None renders as no-change-shown.
    """
    if latest is None or month_ago is None or month_ago == 0:
        return None
    return float(latest - month_ago) / float(month_ago)


class CreateWatchlistIn(BaseModel):
    name: str


class AddEntryIn(BaseModel):
    entity_id: UUID


def build_router(app: App) -> Router:
    router = Router()

    @router.post("/watchlists")
    async def create_watchlist(body: CreateWatchlistIn, request: Request) -> dict:
        user = _require_user(request)
        row = await wl.create(app.db.pool, user_id=user, name=body.name)
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "created_at": _iso(row["created_at"]),
        }

    @router.get("/watchlists")
    async def list_watchlists(request: Request) -> dict:
        user = _require_user(request)
        rows = await wl.lists_for_user(app.db.pool, user_id=user)
        return {
            "watchlists": [
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "created_at": _iso(r["created_at"]),
                }
                for r in rows
            ]
        }

    @router.post("/watchlists/{watchlist_id}/entries")
    async def add_entry(
        watchlist_id: UUID, body: AddEntryIn, request: Request
    ) -> dict:
        user = _require_user(request)
        entry = await wl.add_entity(
            app.db.pool,
            watchlist_id=watchlist_id,
            entity_id=body.entity_id,
            user_id=user,
        )
        if entry is None:
            # A 404, not a 403: confirming the list exists but belongs to
            # someone else would let a caller enumerate other users' lists.
            raise not_found("Watchlist or entity not found")
        return {
            "watchlist_id": str(entry["watchlist_id"]),
            "entity_id": str(entry["entity_id"]),
            "added_at": _iso(entry["added_at"]),
        }

    @router.delete("/watchlists/{watchlist_id}/entries/{entity_id}")
    async def remove_entry(
        watchlist_id: UUID, entity_id: UUID, request: Request
    ) -> dict:
        user = _require_user(request)
        ok = await wl.remove_entity(
            app.db.pool,
            watchlist_id=watchlist_id,
            entity_id=entity_id,
            user_id=user,
        )
        if not ok:
            raise not_found("Entry not found")
        return {"removed": True}

    @router.get("/watchlists/{watchlist_id}/entries")
    async def list_entries(watchlist_id: UUID, request: Request) -> dict:
        user = _require_user(request)
        rows = await wl.entries(
            app.db.pool, watchlist_id=watchlist_id, user_id=user
        )
        if rows is None:
            raise not_found("Watchlist not found")
        return {
            "entries": [
                {
                    "entity_id": str(r["entity_id"]),
                    "kind": r["kind"],
                    "symbol": r["symbol"],
                    "name": r["name"],
                    "added_at": _iso(r["added_at"]),
                    "latest_price": float(r["latest_value"])
                    if r["latest_value"] is not None
                    else None,
                    "latest_as_of": r["latest_as_of"].isoformat()
                    if r["latest_as_of"] is not None
                    else None,
                    "change_30d": _change_30d(r["latest_value"], r["month_ago_value"]),
                }
                for r in rows
            ]
        }

    return router


__all__ = ["build_router"]
