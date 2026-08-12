"""Authenticated, read-only consumer wallet accounts."""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from neutron import App, Router
from neutron.error import bad_request, conflict, not_found, unauthorized
from pydantic import BaseModel
from starlette.requests import Request

from omni import wallets
from omni.auth import resolve_audience_from_request


def _require_user(request: Request) -> UUID:
    user = resolve_audience_from_request(request)
    if user is None:
        raise unauthorized("Authentication required")
    return user


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _payload(row) -> dict:
    balance = row["balance"]
    if isinstance(balance, str):
        balance = json.loads(balance)
    return {
        "id": str(row["id"]),
        "address_family": row["address_family"],
        "address": row["address"],
        "source": row["source"],
        "label": row["label"],
        "discovered_by": row["discovered_by"],
        "balance": balance,
        "refreshed_at": _iso(row["refreshed_at"]),
        "refresh_error": row["refresh_error"],
        "created_at": _iso(row["created_at"]),
    }


class AddWalletIn(BaseModel):
    address_family: str
    address: str
    source: str
    label: str
    discovered_by: str = "manual"


class RenameWalletIn(BaseModel):
    label: str


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/wallets")
    async def list_wallets(request: Request) -> dict:
        user = _require_user(request)
        rows = await wallets.accounts_for_user(app.db.pool, user_id=user)
        return {
            "accounts": [_payload(row) for row in rows],
            "security": {
                "read_only": True,
                "stores_private_keys": False,
                "stores_seed_phrases": False,
            },
        }

    @router.post("/wallets")
    async def add_wallet(body: AddWalletIn, request: Request) -> dict:
        user = _require_user(request)
        try:
            row = await wallets.add_account(
                app.db.pool,
                user_id=user,
                address_family=body.address_family,
                address=body.address,
                source=body.source,
                label=body.label,
                discovered_by=body.discovered_by,
            )
        except wallets.DuplicateWallet as exc:
            raise conflict(str(exc)) from exc
        except ValueError as exc:
            raise bad_request(str(exc)) from exc
        return _payload(row)

    @router.patch("/wallets/{account_id}")
    async def rename_wallet(
        account_id: UUID, body: RenameWalletIn, request: Request,
    ) -> dict:
        user = _require_user(request)
        try:
            row = await wallets.rename_account(
                app.db.pool, user_id=user, account_id=account_id, label=body.label,
            )
        except ValueError as exc:
            raise bad_request(str(exc)) from exc
        if row is None:
            raise not_found("Wallet account not found")
        return _payload(row)

    @router.delete("/wallets/{account_id}")
    async def remove_wallet(account_id: UUID, request: Request) -> dict:
        user = _require_user(request)
        removed = await wallets.remove_account(
            app.db.pool, user_id=user, account_id=account_id,
        )
        if not removed:
            raise not_found("Wallet account not found")
        return {"removed": True}

    @router.post("/wallets/refresh")
    async def refresh_wallets(request: Request) -> dict:
        user = _require_user(request)
        current = await wallets.accounts_for_user(app.db.pool, user_id=user)
        refreshed = []
        for account in current[:20]:
            row = await wallets.refresh_account(
                app.db.pool, user_id=user, account_id=account["id"],
            )
            if row is not None:
                refreshed.append(_payload(row))
        return {"accounts": refreshed}

    @router.post("/wallets/{account_id}/refresh")
    async def refresh_wallet(account_id: UUID, request: Request) -> dict:
        user = _require_user(request)
        row = await wallets.refresh_account(
            app.db.pool, user_id=user, account_id=account_id,
        )
        if row is None:
            raise not_found("Wallet account not found")
        return _payload(row)

    return router


__all__ = ["build_router"]
