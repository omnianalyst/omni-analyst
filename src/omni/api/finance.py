"""Authenticated finance endpoints. Every query is user-scoped: finance
data is BYO by construction and never crosses users."""

from __future__ import annotations

from uuid import UUID

from neutron import App, Router
from neutron.error import bad_request, not_found, unauthorized
from pydantic import BaseModel
from starlette.requests import Request

from omni.finance import bankcreds, banksync, parsers, rules, schedules_service, service
from omni.finance import reports as service_reports
from omni.finance import budget as budget_mod
from omni.auth import resolve_audience_from_request
from omni.finance.normalisation import ImportRowError
from omni.finance.schedules import ScheduleError
from omni.finance.service import FinanceError


def _require_user(request: Request) -> UUID:
    user = resolve_audience_from_request(request)
    if user is None:
        raise unauthorized("Authentication required")
    return user


async def _run(coro):
    try:
        return await coro
    except (FinanceError, ImportRowError, ScheduleError) as exc:
        raise bad_request(str(exc))


class AccountIn(BaseModel):
    name: str
    type: str
    offbudget: bool = False
    opening_balance_cents: int | None = None


class CategoryIn(BaseModel):
    name: str
    is_income: bool = False


class TransactionIn(BaseModel):
    account_id: str
    date: str
    amount: int
    payee: str | None = None
    category_id: str | None = None
    notes: str | None = None
    cleared: bool = True
    splits: list[dict] | None = None


class TransactionPatch(BaseModel):
    category_id: str | None = None
    notes: str | None = None
    cleared: bool | None = None
    reconciled: bool | None = None


class ImportIn(BaseModel):
    account_id: str
    csv: str
    preview: bool = False
    format: str | None = None


class BankCredentialsIn(BaseModel):
    provider: str
    fields: dict


class BankLinkIn(BaseModel):
    institution_id: str


class SimpleFinClaimIn(BaseModel):
    email: str
    password: str


class BankSyncIn(BaseModel):
    bank_account_id: str | None = None


class BudgetIn(BaseModel):
    month: str
    category_id: str
    amount: int
    rollover_mode: str | None = None


class ScheduleIn(BaseModel):
    name: str
    config: dict
    payee: str | None = None
    account_id: str | None = None
    amount: int | None = None
    notes: str | None = None


class ScheduleActiveIn(BaseModel):
    active: bool


class GoalIn(BaseModel):
    goal_type: str | None = None
    goal_amount: int | None = None
    goal_date: str | None = None


class BaseCurrencyIn(BaseModel):
    currency: str


class MigrateActualIn(BaseModel):
    zip_base64: str


class RuleIn(BaseModel):
    conditions: list[dict]
    actions: list[dict]
    rank: int | None = None
    enabled: bool = True


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise bad_request(f"invalid uuid {value!r}")


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/finance/accounts")
    async def accounts(request: Request) -> dict:
        user = _require_user(request)
        return {"accounts": await _run(service.list_accounts(app.db.pool, user))}

    @router.post("/finance/accounts")
    async def add_account(request: Request, body: AccountIn) -> dict:
        user = _require_user(request)
        return await _run(service.create_account(
            app.db.pool,
            user,
            name=body.name,
            type=body.type,
            offbudget=body.offbudget,
            opening_balance_cents=body.opening_balance_cents,
        ))

    @router.delete("/finance/accounts/{account_id}")
    async def close_account(request: Request, account_id: str) -> dict:
        user = _require_user(request)
        row = await app.db.pool.fetchval(
            "SELECT id FROM finance_account WHERE id = $1 AND user_id = $2 AND NOT closed",
            _uuid(account_id),
            user,
        )
        if row is None:
            raise not_found("account not found")
        await _run(service.close_account(app.db.pool, user, _uuid(account_id), True))
        return {"closed": True}

    @router.get("/finance/categories")
    async def categories(request: Request) -> dict:
        user = _require_user(request)
        return {"categories": await _run(service.list_categories(app.db.pool, user))}

    @router.post("/finance/categories")
    async def add_category(request: Request, body: CategoryIn) -> dict:
        user = _require_user(request)
        return await _run(service.create_category(
            app.db.pool, user, name=body.name, is_income=body.is_income
        ))

    @router.delete("/finance/categories/{category_id}")
    async def remove_category(request: Request, category_id: str) -> dict:
        user = _require_user(request)
        await _run(service.delete_category(app.db.pool, user, _uuid(category_id)))
        return {"deleted": True}

    @router.get("/finance/transactions")
    async def transactions(
        request: Request,
        month: str,
        account_id: str | None = None,
        q: str | None = None,
    ) -> dict:
        user = _require_user(request)
        return {
            "transactions": await _run(service.list_transactions(
                app.db.pool,
                user,
                month=f"{month[:7]}-01",
                account_id=_uuid(account_id) if account_id else None,
                query=q,
            ))
        }

    @router.post("/finance/transactions")
    async def add_transaction(request: Request, body: TransactionIn) -> dict:
        user = _require_user(request)
        return await _run(service.create_manual_transaction(
            app.db.pool,
            user,
            account_id=_uuid(body.account_id),
            date=body.date,
            amount=body.amount,
            payee_name=body.payee,
            category_id=_uuid(body.category_id) if body.category_id else None,
            notes=body.notes,
            cleared=body.cleared,
            splits=body.splits,
        ))

    @router.patch("/finance/transactions/{tx_id}")
    async def patch_transaction(request: Request, tx_id: str, body: TransactionPatch) -> dict:
        user = _require_user(request)
        await _run(service.update_transaction(
            app.db.pool,
            user,
            _uuid(tx_id),
            category_id=_uuid(body.category_id) if body.category_id else None,
            notes=body.notes,
            cleared=body.cleared,
            reconciled=body.reconciled,
        ))
        return {"updated": True}

    @router.delete("/finance/transactions/{tx_id}")
    async def remove_transaction(request: Request, tx_id: str) -> dict:
        user = _require_user(request)
        await _run(service.delete_transaction(app.db.pool, user, _uuid(tx_id)))
        return {"deleted": True}

    @router.post("/finance/import")
    async def import_csv(request: Request, body: ImportIn) -> dict:
        user = _require_user(request)
        rows = parsers.parse_any(body.csv, body.format)
        return await _run(service.import_transactions(
            app.db.pool,
            user,
            account_id=_uuid(body.account_id),
            raw_rows=rows,
            preview=body.preview,
        ))

    @router.get("/finance/budget")
    async def budget(request: Request, month: str) -> dict:
        user = _require_user(request)
        return await _run(service.budget_month(app.db.pool, user, f"{month[:7]}-01"))

    @router.put("/finance/budget")
    async def set_budget(request: Request, body: BudgetIn) -> dict:
        user = _require_user(request)
        if body.rollover_mode is not None and body.rollover_mode not in {"rollover", "reset", "hold"}:
            raise bad_request("rollover_mode must be one of rollover, reset, hold")
        await _run(service.set_budget_amount(
            app.db.pool,
            user,
            month=body.month,
            category_id=_uuid(body.category_id),
            amount=body.amount,
            rollover_mode=body.rollover_mode,
        ))
        return {"set": True}

    @router.get("/finance/schedules")
    async def schedules(request: Request) -> dict:
        user = _require_user(request)
        return {"schedules": await _run(schedules_service.list_schedules(app.db.pool, user))}

    @router.post("/finance/schedules")
    async def add_schedule(request: Request, body: ScheduleIn) -> dict:
        user = _require_user(request)
        return await _run(schedules_service.create_schedule(
            app.db.pool,
            user,
            name=body.name,
            config=body.config,
            payee=body.payee,
            account_id=_uuid(body.account_id) if body.account_id else None,
            amount=body.amount,
            notes=body.notes,
        ))

    @router.patch("/finance/schedules/{schedule_id}")
    async def toggle_schedule(
        request: Request, schedule_id: str, body: ScheduleActiveIn
    ) -> dict:
        user = _require_user(request)
        await _run(schedules_service.set_schedule_active(
            app.db.pool, user, _uuid(schedule_id), body.active
        ))
        return {"updated": True}

    @router.delete("/finance/schedules/{schedule_id}")
    async def remove_schedule(request: Request, schedule_id: str) -> dict:
        user = _require_user(request)
        await _run(schedules_service.delete_schedule(app.db.pool, user, _uuid(schedule_id)))
        return {"deleted": True}

    @router.get("/finance/reports/cash-flow")
    async def report_cash_flow(request: Request, months: int = 6) -> dict:
        user = _require_user(request)
        return await _run(service_reports.cash_flow(app.db.pool, user, months=months))

    @router.get("/finance/reports/spending")
    async def report_spending(request: Request, month: str) -> dict:
        user = _require_user(request)
        return {
            "month": month[:7],
            "categories": await _run(
                service_reports.spending_by_category(app.db.pool, user, month)
            ),
        }

    @router.get("/finance/reports/net-worth")
    async def report_net_worth(request: Request, months: int = 12) -> dict:
        user = _require_user(request)
        return await _run(service_reports.net_worth(app.db.pool, user, months=months))

    @router.put("/finance/categories/{category_id}/goal")
    async def set_goal(request: Request, category_id: str, body: GoalIn) -> dict:
        user = _require_user(request)
        if body.goal_type is not None and body.goal_type not in {"monthly", "save_by_date"}:
            raise bad_request("goal_type must be monthly or save_by_date")
        if body.goal_type is not None and not body.goal_amount:
            raise bad_request("a goal needs an amount")
        from datetime import date as _date

        goal_date = _date.fromisoformat(body.goal_date[:10]) if body.goal_date else None
        await app.db.pool.execute(
            """
            UPDATE finance_category SET
                goal_type = $3,
                goal_amount = $4,
                goal_date = $5
            WHERE id = $1 AND user_id = $2 AND NOT tombstone
            """,
            _uuid(category_id),
            user,
            body.goal_type,
            body.goal_amount,
            goal_date,
        )
        return {"set": True}

    @router.delete("/finance/categories/{category_id}/goal")
    async def clear_goal(request: Request, category_id: str) -> dict:
        user = _require_user(request)
        await app.db.pool.execute(
            """
            UPDATE finance_category SET goal_type = NULL, goal_amount = NULL, goal_date = NULL
            WHERE id = $1 AND user_id = $2
            """,
            _uuid(category_id),
            user,
        )
        return {"cleared": True}

    @router.get("/finance/base-currency")
    async def get_base_currency(request: Request) -> dict:
        user = _require_user(request)
        return {"currency": await budget_mod.base_currency(app.db.pool, user)}

    @router.put("/finance/base-currency")
    async def put_base_currency(request: Request, body: BaseCurrencyIn) -> dict:
        user = _require_user(request)
        try:
            await budget_mod.set_base_currency(app.db.pool, user, body.currency)
        except ValueError as exc:
            raise bad_request(str(exc))
        return {"set": body.currency.upper()}

    @router.post("/finance/migrate/actual")
    async def migrate_actual(request: Request, body: MigrateActualIn) -> dict:
        import base64 as _b64

        user = _require_user(request)
        try:
            archive = _b64.b64decode(body.zip_base64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise bad_request("zip_base64 is not valid base64") from exc
        from omni.finance import migrate_actual

        return await _run(migrate_actual.import_actual_backup(app.db.pool, user, archive))

    @router.get("/finance/rules")
    async def list_rules(request: Request) -> dict:
        user = _require_user(request)
        rows = await app.db.pool.fetch(
            """
            SELECT id, rank, conditions, actions, enabled
            FROM finance_rule WHERE user_id = $1 ORDER BY rank
            """,
            user,
        )
        return {
            "rules": [
                {
                    "id": str(r["id"]),
                    "rank": r["rank"],
                    "conditions": r["conditions"],
                    "actions": r["actions"],
                    "enabled": r["enabled"],
                }
                for r in rows
            ]
        }

    @router.post("/finance/rules")
    async def add_rule(request: Request, body: RuleIn) -> dict:
        user = _require_user(request)
        rules.validate_rule_payload(body.conditions, body.actions)
        rank = body.rank
        if rank is None:
            rank = (await app.db.pool.fetchval(
                "SELECT coalesce(max(rank), 0) + 1 FROM finance_rule WHERE user_id = $1",
                user,
            )) or 1
        row = await app.db.pool.fetchrow(
            """
            INSERT INTO finance_rule (user_id, rank, conditions, actions, enabled)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5) RETURNING id
            """,
            user,
            rank,
            _dumps(body.conditions),
            _dumps(body.actions),
            body.enabled,
        )
        return {"id": str(row["id"]), "rank": rank}

    @router.delete("/finance/rules/{rule_id}")
    async def remove_rule(request: Request, rule_id: str) -> dict:
        user = _require_user(request)
        await app.db.pool.execute(
            "DELETE FROM finance_rule WHERE id = $1 AND user_id = $2",
            _uuid(rule_id),
            user,
        )
        return {"deleted": True}

    @router.get("/finance/bank/credentials")
    async def bank_credentials(request: Request) -> dict:
        user = _require_user(request)
        configured = await bankcreds.bank_configured(app.db.pool, user)
        return {
            "providers": [
                {
                    "key": key,
                    "label": meta["label"],
                    "fields": list(meta["fields"]),
                    "configured": configured.get(key, False),
                }
                for key, meta in bankcreds.BANK_PROVIDERS.items()
            ]
        }

    @router.put("/finance/bank/credentials")
    async def put_bank_credentials(request: Request, body: BankCredentialsIn) -> dict:
        user = _require_user(request)
        if body.provider == "simplefin":
            email = (body.fields.get("email") or "").strip()
            password = (body.fields.get("password") or "").strip()
            if not email or not password:
                raise bad_request("simplefin needs email and password")
            access_url = await banksync.simplefin_claim(email, password)
            await _run(bankcreds.put_bank_key(
                app.db.pool, user, "simplefin", {"access_url": access_url}
            ))
            return {"stored": "simplefin"}
        try:
            await bankcreds.put_bank_key(
                app.db.pool, user, body.provider, body.fields
            )
        except ValueError as exc:
            raise bad_request(str(exc))
        return {"stored": body.provider}

    @router.delete("/finance/bank/credentials/{provider}")
    async def delete_bank_credentials(request: Request, provider: str) -> dict:
        user = _require_user(request)
        await bankcreds.remove_bank_key(app.db.pool, user, provider)
        return {"removed": provider}

    @router.get("/finance/bank/institutions")
    async def bank_institutions(
        request: Request, country: str = ""
    ) -> dict:
        user = _require_user(request)
        client = await run_gc(app.db.pool, user)
        try:
            return {"institutions": await client.institutions(country)}
        finally:
            await client.aclose()

    @router.post("/finance/bank/link")
    async def create_bank_link(request: Request, body: BankLinkIn) -> dict:
        user = _require_user(request)
        from uuid import uuid4 as _uuid4

        link_id = _uuid4()
        redirect = f"{str(request.base_url).rstrip('/')}/finance?link={link_id}"
        client = await run_gc(app.db.pool, user)
        try:
            requisition = await client.create_requisition(
                body.institution_id, redirect
            )
        finally:
            await client.aclose()
        await app.db.pool.execute(
            """
            INSERT INTO finance_bank_link
                (id, user_id, provider, institution_id, institution_name,
                 requisition_id, status)
            VALUES ($1, $2, 'gocardless', $3, $4, $5, $6)
            """,
            link_id,
            user,
            body.institution_id,
            requisition.get("institution_id") or "",
            requisition.get("id"),
            requisition.get("status", "pending"),
        )
        return {
            "link_id": str(link_id),
            "url": requisition.get("link"),
            "status": requisition.get("status", "pending"),
        }

    @router.get("/finance/bank/link/{link_id}")
    async def poll_bank_link(request: Request, link_id: str) -> dict:
        user = _require_user(request)
        client = await run_gc(app.db.pool, user)
        try:
            return await _run(banksync.complete_gocardless_link(
                app.db.pool, user, client, _uuid(link_id)
            ))
        finally:
            await client.aclose()

    @router.post("/finance/bank/simplefin/link")
    async def link_simplefin(request: Request) -> dict:
        user = _require_user(request)
        try:
            accounts = await banksync.link_simplefin_accounts(
                app.db.pool, user
            )
        except banksync.BankSyncError as exc:
            raise bad_request(str(exc))
        return {"accounts": accounts}

    @router.get("/finance/bank/accounts")
    async def bank_accounts(request: Request) -> dict:
        user = _require_user(request)
        rows = await app.db.pool.fetch(
            """
            SELECT b.id, b.provider, b.provider_account_id, b.last_synced_at,
                   b.sync_error, a.name AS account_name, a.id AS account_id
            FROM finance_bank_account b
            JOIN finance_account a ON a.id = b.account_id
            WHERE b.user_id = $1
            ORDER BY a.name
            """,
            user,
        )
        return {
            "accounts": [
                {
                    "id": str(r["id"]),
                    "provider": r["provider"],
                    "name": r["account_name"],
                    "account_id": str(r["account_id"]),
                    "last_synced_at": r["last_synced_at"].isoformat()
                    if r["last_synced_at"]
                    else None,
                    "sync_error": r["sync_error"],
                }
                for r in rows
            ]
        }

    @router.post("/finance/bank/sync")
    async def sync_banks(request: Request, body: BankSyncIn) -> dict:
        user = _require_user(request)
        targets = []
        if body.bank_account_id:
            targets = [_uuid(body.bank_account_id)]
        else:
            rows = await app.db.pool.fetch(
                "SELECT id FROM finance_bank_account WHERE user_id = $1", user
            )
            targets = [r["id"] for r in rows]
        if not targets:
            raise bad_request("no bank accounts linked yet")
        results = {}
        errors = {}
        for target in targets:
            try:
                result = await banksync.sync_bank_account(
                    app.db.pool, user, target
                )
                results[str(target)] = {
                    "added": len(result["added"]),
                    "updated": len(result["updated"]),
                }
            except banksync.BankSyncError as exc:
                errors[str(target)] = str(exc)
        return {"synced": results, "errors": errors}

    return router


async def run_gc(pool, user):
    try:
        return await banksync.client_for(pool, user, "gocardless")
    except banksync.BankSyncError as exc:
        raise bad_request(str(exc))


def _dumps(value) -> str:
    import json

    return value if isinstance(value, str) else json.dumps(value)
