"""Private, non-custodial wallet-account storage and read-only refreshes."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

FAMILIES = frozenset({"evm", "solana", "bitcoin"})
SOURCES = frozenset({"phantom", "metamask", "ledger", "manual"})
DISCOVERY_METHODS = frozenset({"manual", "browser_extension"})
_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_BITCOIN = re.compile(
    r"^(bc1[ac-hj-np-z02-9]{11,87}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$",
    re.IGNORECASE,
)

ETHEREUM_RPC = "https://cloudflare-eth.com"
ETHEREUM_TOKEN_API = "https://eth.blockscout.com/api/v2/addresses/{address}/token-balances"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
BITCOIN_API = "https://mempool.space/api/address/{address}"
SOLANA_TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)


class DuplicateWallet(ValueError):
    pass


def normalize_address(family: str, address: str) -> str:
    family = family.strip().lower()
    address = address.strip()
    if family not in FAMILIES:
        raise ValueError(f"unsupported address family {family!r}")
    valid = {
        "evm": bool(_EVM.fullmatch(address)),
        "solana": bool(_SOLANA.fullmatch(address)),
        "bitcoin": bool(_BITCOIN.fullmatch(address)),
    }[family]
    if not valid:
        raise ValueError(f"address is not a valid {family} public address")
    return address.lower() if family in {"evm", "bitcoin"} else address


async def add_account(
    pool,
    *,
    user_id: UUID,
    address_family: str,
    address: str,
    source: str,
    label: str,
    discovered_by: str,
):
    family = address_family.strip().lower()
    source = source.strip().lower()
    discovered_by = discovered_by.strip().lower()
    label = label.strip()
    if source not in SOURCES:
        raise ValueError(f"unsupported wallet source {source!r}")
    if discovered_by not in DISCOVERY_METHODS:
        raise ValueError(f"unsupported discovery method {discovered_by!r}")
    if not label or len(label) > 80:
        raise ValueError("label must contain 1 to 80 characters")
    normalized = normalize_address(family, address)
    try:
        return await pool.fetchrow(
            """
            INSERT INTO wallet_account
                (user_id, address_family, address, source, label, discovered_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            user_id, family, normalized, source, label, discovered_by,
        )
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise DuplicateWallet("This address is already tracked") from exc
        raise


async def accounts_for_user(pool, *, user_id: UUID) -> list[Any]:
    return await pool.fetch(
        "SELECT * FROM wallet_account WHERE user_id = $1 ORDER BY created_at, id",
        user_id,
    )


async def remove_account(pool, *, user_id: UUID, account_id: UUID) -> bool:
    status = await pool.execute(
        "DELETE FROM wallet_account WHERE id = $1 AND user_id = $2",
        account_id, user_id,
    )
    return status == "DELETE 1"


async def rename_account(pool, *, user_id: UUID, account_id: UUID, label: str):
    label = label.strip()
    if not label or len(label) > 80:
        raise ValueError("label must contain 1 to 80 characters")
    return await pool.fetchrow(
        """
        UPDATE wallet_account SET label = $3, updated_at = now()
        WHERE id = $1 AND user_id = $2 RETURNING *
        """,
        account_id, user_id, label,
    )


async def _rpc(client: httpx.AsyncClient, url: str, method: str, params: list) -> Any:
    response = await client.post(url, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    })
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise ValueError(str(body["error"].get("message", "RPC refused the request")))
    return body.get("result")


async def _evm_balance(client: httpx.AsyncClient, address: str) -> dict:
    raw = await _rpc(client, ETHEREUM_RPC, "eth_getBalance", [address, "latest"])
    wei = int(raw, 16)
    assets = [{"symbol": "ETH", "amount": str(Decimal(wei) / Decimal(10**18))}]
    coverage = "Ethereum native balance"
    try:
        response = await client.get(ETHEREUM_TOKEN_API.format(address=address))
        response.raise_for_status()
        token_rows = response.json()
        if not isinstance(token_rows, list):
            raise TypeError("token balance response was not a list")
        for entry in token_rows[:100]:
            token = entry.get("token", {})
            if token.get("type") != "ERC-20":
                continue
            decimals = int(token.get("decimals") or 0)
            if decimals < 0 or decimals > 36:
                continue
            amount = Decimal(entry["value"]) / Decimal(10**decimals)
            if amount:
                assets.append({
                    "symbol": token.get("symbol") or token.get("address_hash", "token")[:10],
                    "amount": str(amount),
                    "kind": "erc20",
                })
        coverage = "Ethereum native and indexed ERC-20 balances"
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        coverage += "; ERC-20 index unavailable"
    return {
        "assets": assets,
        "coverage": coverage,
    }


async def _solana_balance(client: httpx.AsyncClient, address: str) -> dict:
    native = await _rpc(client, SOLANA_RPC, "getBalance", [address, {"commitment": "confirmed"}])
    token_rows = []
    for program_id in SOLANA_TOKEN_PROGRAMS:
        tokens = await _rpc(client, SOLANA_RPC, "getTokenAccountsByOwner", [
            address,
            {"programId": program_id},
            {"encoding": "jsonParsed", "commitment": "confirmed"},
        ])
        token_rows.extend(tokens.get("value", []))
    assets = [{
        "symbol": "SOL",
        "amount": str(Decimal(native["value"]) / Decimal(10**9)),
    }]
    for entry in token_rows:
        info = entry["account"]["data"]["parsed"]["info"]
        amount = info["tokenAmount"].get("uiAmountString")
        if amount and Decimal(amount) != 0:
            assets.append({"symbol": info["mint"], "amount": amount, "kind": "spl_token"})
    return {"assets": assets, "coverage": "SOL, SPL token, and Token-2022 balances"}


async def _bitcoin_balance(client: httpx.AsyncClient, address: str) -> dict:
    response = await client.get(BITCOIN_API.format(address=address))
    response.raise_for_status()
    body = response.json()
    chain = body["chain_stats"]
    mempool = body["mempool_stats"]
    sats = (
        chain["funded_txo_sum"] - chain["spent_txo_sum"]
        + mempool["funded_txo_sum"] - mempool["spent_txo_sum"]
    )
    return {
        "assets": [{"symbol": "BTC", "amount": str(Decimal(sats) / Decimal(10**8))}],
        "coverage": "This address only; rotating addresses require separate tracking",
    }


async def refresh_account(pool, *, user_id: UUID, account_id: UUID):
    row = await pool.fetchrow(
        "SELECT * FROM wallet_account WHERE id = $1 AND user_id = $2",
        account_id, user_id,
    )
    if row is None:
        return None
    error = None
    balance = None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            if row["address_family"] == "evm":
                balance = await _evm_balance(client, row["address"])
            elif row["address_family"] == "solana":
                balance = await _solana_balance(client, row["address"])
            else:
                balance = await _bitcoin_balance(client, row["address"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        error = str(exc)[:300]
    now = datetime.now(UTC)
    return await pool.fetchrow(
        """
        UPDATE wallet_account
        SET balance = COALESCE($3::jsonb, balance), refreshed_at = $4,
            refresh_error = $5, updated_at = now()
        WHERE id = $1 AND user_id = $2 RETURNING *
        """,
        account_id, user_id, json.dumps(balance) if balance is not None else None, now, error,
    )


__all__ = [
    "DuplicateWallet",
    "accounts_for_user",
    "add_account",
    "normalize_address",
    "refresh_account",
    "remove_account",
    "rename_account",
]
