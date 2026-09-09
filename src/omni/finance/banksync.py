"""BYO bank sync: goCardless (Berlin Group) and SimpleFin, direct.

Normalization ported from Actual Budget (MIT):
packages/loot-core/src/server/accounts/sync.ts
(normalizeBankSyncTransactions, getAccountSyncStartDate). Copyright (c)
James Long and Actual Budget contributors, MIT license.

Kept: cleared = booked, pending rows optional, amount falls back to
transactionAmount, imported_id falls back to account-internalTransactionId
for cleared rows, notes trimmed and hash-escaped, ~90-day window from the
account's oldest transaction. Dropped: per-account custom field mappings
(the default mapping is what real accounts use); the hosted Actual proxy
(we talk to the provider directly with the user's own keys).
"""

from __future__ import annotations

import base64
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx

from omni.finance import service
from omni.finance.bankcreds import get_bank_keys

GOCARDLESS_BASE = "https://bankaccountdata.gocardless.com/api/v2"
SIMPLEFIN_AUTH = "https://beta-bridge.simplefin.org/auth"
SYNC_WINDOW_DAYS = 90


class BankSyncError(Exception):
    def __init__(self, provider: str, message: str):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message


def normalize_gocardless(transactions: list[dict], account_ref: str) -> list[dict]:
    """Berlin Group transaction JSON -> raw import rows."""
    rows = []
    for trans in transactions:
        amount = trans.get("amount")
        if amount is None:
            inner = trans.get("transactionAmount") or {}
            amount = inner.get("amount")
        if amount is None:
            raise BankSyncError("gocardless", "transaction without amount")
        payee = (
            trans.get("payeeName")
            or trans.get("creditorName")
            or trans.get("debtorName")
            or (trans.get("remittanceInformationUnstructured") or None)
        )
        imported_id = trans.get("transactionId")
        if not imported_id and trans.get("internalTransactionId"):
            imported_id = f"{account_ref}-{trans['internalTransactionId']}"
        rows.append({
            "date": (trans.get("date") or trans.get("bookingDate") or "")[:10] or None,
            "payee_name": payee,
            "amount": str(amount),
            "notes": trans.get("notes") or trans.get("remittanceInformationUnstructured"),
            "imported_id": imported_id,
            "cleared": bool(trans.get("booked", True)),
            "raw": {"provider": "gocardless", "transaction": trans},
        })
    return rows


def normalize_simplefin(transactions: list[dict]) -> list[dict]:
    rows = []
    for trans in transactions:
        amount = trans.get("amount")
        if amount is None:
            raise BankSyncError("simplefin", "transaction without amount")
        posted = (trans.get("posted") or trans.get("transacted_at") or "")[:10]
        rows.append({
            "date": posted or None,
            "payee_name": trans.get("description") or None,
            "amount": str(amount),
            "notes": trans.get("memo") or None,
            "imported_id": trans.get("id"),
            "cleared": True,
            "raw": {"provider": "simplefin", "transaction": trans},
        })
    return rows


def _mapped_or_fallback(tx: dict, mapping: dict | None, side: str, key: str, fallbacks: tuple) -> str | None:
    """Actual's custom-sync-mapping: a per-account override of which
    payload field supplies date/payee/notes, tried before the default
    fallback chain."""
    if mapping:
        override = (mapping.get(side) or {}).get(key)
        if override and tx.get(override):
            return tx[override]
    for field in fallbacks:
        if tx.get(field):
            return tx[field]
    return None


def _berlin_row(tx: dict, *, booked: bool, mapping: dict | None = None) -> dict:
    inner = tx.get("transactionAmount") or {}
    side = "payment" if (inner.get("amount") or "0").startswith("-") else "deposit"
    payee = _mapped_or_fallback(
        tx, mapping, side, "payee",
        ("creditorName", "debtorName"),
    ) or (
        tx.get("remittanceInformationUnstructured")
        or tx.get("remittanceInformationStructured")
    )
    notes = _mapped_or_fallback(
        tx, mapping, side, "notes",
        ("remittanceInformationUnstructured", "remittanceInformationStructured"),
    )
    date_value = _mapped_or_fallback(
        tx, mapping, side, "date", ("bookingDate", "valueDate")
    )
    imported_id = tx.get("transactionId")
    if not imported_id and tx.get("internalTransactionId"):
        imported_id = f"{tx.get('_account_ref', '')}-{tx['internalTransactionId']}"
    amount = inner.get("amount")
    if amount is None:
        amount = tx.get("amount")
    return {
        "date": (str(date_value) if date_value else "")[:10] or None,
        "payee_name": payee,
        "amount": str(amount if amount is not None else ""),
        "notes": notes,
        "imported_id": imported_id,
        "cleared": booked,
        "raw": {"provider": "gocardless", "transaction": tx},
    }


def gocardless_transactions_to_rows(
    booked: list[dict],
    pending: list[dict],
    account_ref: str,
    mapping: dict | None = None,
) -> list[dict]:
    rows = []
    for tx in booked:
        tx = {**tx, "_account_ref": account_ref}
        rows.append(_berlin_row(tx, booked=True, mapping=mapping))
    for tx in pending:
        tx = {**tx, "_account_ref": account_ref}
        rows.append(_berlin_row(tx, booked=False, mapping=mapping))
    return rows


class _Client:
    def __init__(self, timeout: float = 30.0):
        self._http = httpx.AsyncClient(timeout=timeout)


class GoCardlessClient(_Client):
    def __init__(self, secret_id: str, secret_key: str):
        super().__init__()
        self._secret_id = secret_id
        self._secret_key = secret_key
        self._token: str | None = None

    async def aclose(self):
        await self._http.aclose()

    async def _token_value(self) -> str:
        if self._token is None:
            r = await self._http.post(
                f"{GOCARDLESS_BASE}/token/new/",
                json={"secret_id": self._secret_id, "secret_key": self._secret_key},
            )
            if r.status_code != 200:
                raise BankSyncError(
                    "gocardless", f"authentication failed ({r.status_code})"
                )
            self._token = r.json().get("access")
        return self._token

    async def _get(self, path: str, params: dict | None = None) -> Any:
        token = await self._token_value()
        r = await self._http.get(
            f"{GOCARDLESS_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 401:
            self._token = None
            token = await self._token_value()
            r = await self._http.get(
                f"{GOCARDLESS_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code != 200:
            raise BankSyncError(
                "gocardless", f"GET {path} failed ({r.status_code}): {r.text[:200]}"
            )
        return r.json()

    async def _post(self, path: str, body: dict) -> Any:
        token = await self._token_value()
        r = await self._http.post(
            f"{GOCARDLESS_BASE}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code not in (200, 201):
            raise BankSyncError(
                "gocardless", f"POST {path} failed ({r.status_code}): {r.text[:200]}"
            )
        return r.json()

    async def institutions(self, country: str) -> list[dict]:
        data = await self._get("/institutions/", {"country": country} if country else None)
        return data if isinstance(data, list) else []

    async def create_requisition(self, institution_id: str, redirect: str) -> dict:
        return await self._post(
            "/requisitions/",
            {"redirect": redirect, "institution_id": institution_id},
        )

    async def requisition(self, requisition_id: str) -> dict:
        return await self._get(f"/requisitions/{requisition_id}/")

    async def account_details(self, provider_account_id: str) -> dict:
        return await self._get(f"/accounts/{provider_account_id}/")

    async def transactions(self, provider_account_id: str, date_from: str) -> dict:
        return await self._get(
            f"/accounts/{provider_account_id}/transactions/",
            {"date_from": date_from},
        )

    async def fetch_rows(
        self, provider_account_id: str, date_from: str, mapping: dict | None = None
    ) -> list[dict]:
        data = await self.transactions(provider_account_id, date_from)
        return gocardless_transactions_to_rows(
            data.get("transactions", {}).get("booked", []),
            data.get("transactions", {}).get("pending", []),
            provider_account_id,
            mapping,
        )


async def simplefin_claim(email: str, password: str) -> str:
    """The two-step SimpleFIN handshake: bank password -> claim URL ->
    access URL (the persistent credential)."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        basic = base64.b64encode(f"{email}:{password}".encode()).decode()
        r = await http.post(
            SIMPLEFIN_AUTH, headers={"Authorization": f"Basic {basic}"}
        )
        if r.status_code != 200 or not r.text.strip().startswith("https://"):
            raise BankSyncError(
                "simplefin", f"claim failed ({r.status_code}): {r.text[:200]}"
            )
        claim_url = r.text.strip()
        r2 = await http.post(claim_url)
        if r2.status_code != 200 or not r2.text.strip().startswith("https://"):
            raise BankSyncError(
                "simplefin", f"access exchange failed ({r2.status_code})"
            )
        return r2.text.strip()


async def simplefin_fetch_accounts(access_url: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=60.0) as http:
        r = await http.get(f"{access_url.rstrip('/')}/accounts")
        if r.status_code != 200:
            raise BankSyncError(
                "simplefin", f"accounts fetch failed ({r.status_code})"
            )
        body = r.json()
    accounts = body.get("accounts") or []
    out = []
    for account in accounts:
        out.append({
            "provider_account_id": str(account.get("id")),
            "name": account.get("name") or account.get("org") or "Bank account",
            "balance": account.get("balance"),
            "transactions": account.get("transactions") or [],
        })
    return out


async def client_for(pool, user_id: UUID, provider: str):
    keys = await get_bank_keys(pool, user_id)
    creds = keys.get(provider)
    if creds is None:
        raise BankSyncError(provider, "credentials not configured")
    if provider == "gocardless":
        return GoCardlessClient(creds["secret_id"], creds["secret_key"])
    raise BankSyncError(provider, "no client needed; use simplefin functions")


async def _sync_start(pool, user_id: UUID, account_id: UUID) -> str:
    oldest = await pool.fetchval(
        """
        SELECT min(date) FROM finance_transaction
        WHERE account_id = $1 AND NOT deleted
        """,
        account_id,
    )
    window = date.today() - timedelta(days=SYNC_WINDOW_DAYS - 1)
    if oldest is not None and oldest < window:
        return oldest.isoformat()
    return window.isoformat()


async def sync_bank_account(
    pool,
    user_id: UUID,
    bank_account_id: UUID,
    *,
    client: GoCardlessClient | None = None,
    simplefin_rows: list[dict] | None = None,
) -> dict:
    row = await pool.fetchrow(
        """
        SELECT b.id, b.provider, b.provider_account_id, b.account_id,
               b.last_synced_at, b.field_mapping
        FROM finance_bank_account b
        WHERE b.id = $1 AND b.user_id = $2
        """,
        bank_account_id,
        user_id,
    )
    if row is None:
        raise BankSyncError("gocardless", "bank account link not found")
    provider = row["provider"]
    try:
        if provider == "gocardless":
            gc = client or await client_for(pool, user_id, "gocardless")
            try:
                mapping = row["field_mapping"]
                if isinstance(mapping, str):
                    import json as _json
                    mapping = _json.loads(mapping)
                raw_rows = await gc.fetch_rows(
                    row["provider_account_id"],
                    await _sync_start(pool, user_id, row["account_id"]),
                    mapping,
                )
            finally:
                if client is None:
                    await gc.aclose()
        else:
            if simplefin_rows is None:
                keys = await get_bank_keys(pool, user_id)
                access_url = (keys.get("simplefin") or {}).get("access_url")
                if access_url is None:
                    raise BankSyncError("simplefin", "credentials not configured")
                matches = [
                    a for a in await simplefin_fetch_accounts(access_url)
                    if a["provider_account_id"] == row["provider_account_id"]
                ]
                simplefin_rows = matches[0]["transactions"] if matches else []
            raw_rows = normalize_simplefin(simplefin_rows)

        raw_rows = [r for r in raw_rows if r.get("date")]
        result = await service.import_transactions(
            pool,
            user_id,
            account_id=row["account_id"],
            raw_rows=raw_rows,
        )
        await pool.execute(
            """
            UPDATE finance_bank_account
            SET last_synced_at = now(), sync_error = NULL WHERE id = $1
            """,
            bank_account_id,
        )
        await pool.execute(
            """
            INSERT INTO finance_import (user_id, account_id, format, added, updated)
            VALUES ($1, $2, $3, $4, $5)
            """,
            user_id,
            row["account_id"],
            provider,
            len(result["added"]),
            len(result["updated"]),
        )
        return result
    except BankSyncError as exc:
        await pool.execute(
            "UPDATE finance_bank_account SET sync_error = $2, last_synced_at = now() WHERE id = $1",
            bank_account_id,
            str(exc),
        )
        raise
    except Exception as exc:
        await pool.execute(
            "UPDATE finance_bank_account SET sync_error = $2, last_synced_at = now() WHERE id = $1",
            bank_account_id,
            str(exc)[:500],
        )
        raise BankSyncError(provider, str(exc)[:500]) from exc


async def complete_gocardless_link(
    pool, user_id: UUID, client: GoCardlessClient, link_id: UUID
) -> dict:
    """Poll the requisition; once the bank approved, create local accounts
    for every provider account that came back."""
    link = await pool.fetchrow(
        "SELECT * FROM finance_bank_link WHERE id = $1 AND user_id = $2",
        link_id,
        user_id,
    )
    if link is None:
        raise BankSyncError("gocardless", "link not found")
    requisition = await client.requisition(link["requisition_id"])
    status = requisition.get("status", "unknown")
    await pool.execute(
        "UPDATE finance_bank_link SET status = $2 WHERE id = $1", link_id, status
    )
    if status != "LN":
        return {"status": status, "accounts": []}

    created = []
    for provider_account_id in requisition.get("accounts", []):
        existing = await pool.fetchval(
            """
            SELECT id FROM finance_bank_account
            WHERE user_id = $1 AND provider = 'gocardless'
              AND provider_account_id = $2
            """,
            user_id,
            provider_account_id,
        )
        if existing:
            continue
        details = await client.account_details(provider_account_id)
        name = (
            (details.get("name") if isinstance(details, dict) else None)
            or link["institution_name"]
            or "Bank account"
        )
        iban = details.get("iban") if isinstance(details, dict) else None
        display = f"{name} {iban[-4:]}" if iban else name
        local = await service.create_account(
            pool, user_id, name=display, type="checking"
        )
        bank_account = await pool.fetchval(
            """
            INSERT INTO finance_bank_account
                (user_id, bank_link_id, account_id, provider, provider_account_id)
            VALUES ($1, $2, $3, 'gocardless', $4) RETURNING id
            """,
            user_id,
            link_id,
            UUID(local["id"]),
            provider_account_id,
        )
        created.append({
            "bank_account_id": str(bank_account),
            "account_id": local["id"],
            "name": display,
        })
    return {"status": status, "accounts": created}


async def link_simplefin_accounts(pool, user_id: UUID) -> list[dict]:
    """SimpleFIN has no redirect handshake: claim once (credential), then
    every account the bridge exposes gets a local counterpart."""
    keys = await get_bank_keys(pool, user_id)
    access_url = (keys.get("simplefin") or {}).get("access_url")
    if access_url is None:
        raise BankSyncError("simplefin", "credentials not configured")
    fetched = await simplefin_fetch_accounts(access_url)

    link_id = await pool.fetchval(
        """
        INSERT INTO finance_bank_link (user_id, provider, institution_name, status)
        VALUES ($1, 'simplefin', 'SimpleFIN', 'LN') RETURNING id
        """,
        user_id,
    )
    created = []
    for account in fetched:
        existing = await pool.fetchval(
            """
            SELECT id FROM finance_bank_account
            WHERE user_id = $1 AND provider = 'simplefin'
              AND provider_account_id = $2
            """,
            user_id,
            account["provider_account_id"],
        )
        if existing:
            continue
        local = await service.create_account(
            pool, user_id, name=account["name"], type="checking"
        )
        bank_account = await pool.fetchval(
            """
            INSERT INTO finance_bank_account
                (user_id, bank_link_id, account_id, provider, provider_account_id)
            VALUES ($1, $2, $3, 'simplefin', $4) RETURNING id
            """,
            user_id,
            link_id,
            UUID(local["id"]),
            account["provider_account_id"],
        )
        created.append({
            "bank_account_id": str(bank_account),
            "account_id": local["id"],
            "name": account["name"],
        })
    if not created:
        await pool.execute("DELETE FROM finance_bank_link WHERE id = $1", link_id)
    return created
