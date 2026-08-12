"""Authenticated private notes and links for the header bulletin."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse
from uuid import UUID

from neutron import App, Router
from neutron.error import bad_request, not_found, unauthorized
from pydantic import BaseModel
from starlette.requests import Request

from omni.auth import resolve_audience_from_request

MAX_ITEMS = 50


class BulletinIn(BaseModel):
    kind: str
    title: str
    body: str | None = None
    url: str | None = None


def _user(request: Request) -> UUID:
    user = resolve_audience_from_request(request)
    if user is None:
        raise unauthorized("Authentication required")
    return user


def _clean(body: BulletinIn) -> tuple[str, str, str | None, str | None]:
    kind = body.kind.strip().lower()
    title = body.title.strip()
    text = body.body.strip() if body.body else None
    url = body.url.strip() if body.url else None
    if kind not in {"note", "link"}:
        raise ValueError("kind must be note or link")
    if not title or len(title) > 100:
        raise ValueError("title must contain 1 to 100 characters")
    if text and len(text) > 1000:
        raise ValueError("note text must be at most 1000 characters")
    if kind == "link":
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("link must be a complete http or https URL")
        if len(url or "") > 2000:
            raise ValueError("link must be at most 2000 characters")
    else:
        url = None
    return kind, title, text, url


def _payload(row) -> dict:
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "url": row["url"],
        "created_at": iso(row["created_at"]),
        "updated_at": iso(row["updated_at"]),
    }


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/bulletin")
    async def list_items(request: Request) -> dict:
        user = _user(request)
        rows = await app.db.pool.fetch(
            "SELECT * FROM bulletin_item WHERE user_id = $1 "
            "ORDER BY updated_at DESC, id LIMIT $2",
            user, MAX_ITEMS,
        )
        return {"items": [_payload(row) for row in rows], "limit": MAX_ITEMS}

    @router.post("/bulletin")
    async def add_item(body: BulletinIn, request: Request) -> dict:
        user = _user(request)
        count = await app.db.pool.fetchval(
            "SELECT count(*) FROM bulletin_item WHERE user_id = $1", user,
        )
        if count >= MAX_ITEMS:
            raise bad_request(f"Bulletin is limited to {MAX_ITEMS} items")
        try:
            kind, title, text, url = _clean(body)
        except ValueError as exc:
            raise bad_request(str(exc)) from exc
        row = await app.db.pool.fetchrow(
            "INSERT INTO bulletin_item (user_id, kind, title, body, url) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING *",
            user, kind, title, text, url,
        )
        return _payload(row)

    @router.patch("/bulletin/{item_id}")
    async def update_item(item_id: UUID, body: BulletinIn, request: Request) -> dict:
        user = _user(request)
        try:
            kind, title, text, url = _clean(body)
        except ValueError as exc:
            raise bad_request(str(exc)) from exc
        row = await app.db.pool.fetchrow(
            "UPDATE bulletin_item SET kind=$3, title=$4, body=$5, url=$6, "
            "updated_at=now() WHERE id=$1 AND user_id=$2 RETURNING *",
            item_id, user, kind, title, text, url,
        )
        if row is None:
            raise not_found("Bulletin item not found")
        return _payload(row)

    @router.delete("/bulletin/{item_id}")
    async def delete_item(item_id: UUID, request: Request) -> dict:
        user = _user(request)
        status = await app.db.pool.execute(
            "DELETE FROM bulletin_item WHERE id=$1 AND user_id=$2", item_id, user,
        )
        if status != "DELETE 1":
            raise not_found("Bulletin item not found")
        return {"removed": True}

    return router


__all__ = ["build_router"]
