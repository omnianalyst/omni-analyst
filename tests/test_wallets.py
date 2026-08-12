"""Read-only wallet accounts remain valid, private, and non-custodial."""

import asyncio
import json

import pytest
from neutron.test import TestClient

from omni import wallets
from omni.main import create_app


class _Lifespan:
    def __init__(self, app):
        self._app = app
        self._receive = asyncio.Queue()
        self._send = asyncio.Queue()

    async def __aenter__(self):
        self._task = asyncio.create_task(
            self._app({"type": "lifespan"}, self._receive.get, self._send.put)
        )
        await self._receive.put({"type": "lifespan.startup"})
        message = await self._send.get()
        assert message["type"] == "lifespan.startup.complete", message
        return self._app

    async def __aexit__(self, *exc):
        await self._receive.put({"type": "lifespan.shutdown"})
        await self._send.get()
        await self._task


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


async def _user(db, email: str):
    return await db.pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id",
        email,
    )


def test_public_address_validation():
    assert wallets.normalize_address("evm", "0x" + "AB" * 20) == "0x" + "ab" * 20
    assert wallets.normalize_address("solana", "11111111111111111111111111111111")
    assert wallets.normalize_address(
        "bitcoin", "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"
    )
    with pytest.raises(ValueError, match="public address"):
        wallets.normalize_address("evm", "correct horse battery staple")


async def test_accounts_are_private_and_deduplicated(db):
    owner = await _user(db, "owner@example.com")
    other = await _user(db, "other@example.com")
    address = "0x" + "12" * 20
    created = await wallets.add_account(
        db.pool,
        user_id=owner,
        address_family="evm",
        address=address,
        source="metamask",
        label="Main",
        discovered_by="browser_extension",
    )

    assert [row["id"] for row in await wallets.accounts_for_user(db.pool, user_id=owner)] == [
        created["id"]
    ]
    assert await wallets.accounts_for_user(db.pool, user_id=other) == []
    assert not await wallets.remove_account(
        db.pool, user_id=other, account_id=created["id"]
    )
    with pytest.raises(wallets.DuplicateWallet):
        await wallets.add_account(
            db.pool,
            user_id=owner,
            address_family="evm",
            address=address.upper().replace("0X", "0x"),
            source="manual",
            label="Duplicate",
            discovered_by="manual",
        )


async def test_refresh_records_balance_without_credentials(db, monkeypatch):
    owner = await _user(db, "owner@example.com")
    created = await wallets.add_account(
        db.pool,
        user_id=owner,
        address_family="evm",
        address="0x" + "34" * 20,
        source="ledger",
        label="Nano X",
        discovered_by="manual",
    )

    async def fake_balance(client, address):
        assert address == "0x" + "34" * 20
        return {"assets": [{"symbol": "ETH", "amount": "1.5"}], "coverage": "test"}

    monkeypatch.setattr(wallets, "_evm_balance", fake_balance)
    refreshed = await wallets.refresh_account(
        db.pool, user_id=owner, account_id=created["id"]
    )
    balance = refreshed["balance"]
    if isinstance(balance, str):
        balance = json.loads(balance)
    assert balance["assets"][0] == {"symbol": "ETH", "amount": "1.5"}
    assert refreshed["refresh_error"] is None
    assert refreshed["refreshed_at"] is not None


async def test_wallet_api_requires_authentication(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        listed = await client.get("/wallets")
        added = await client.post(
            "/wallets",
            json={
                "address_family": "evm",
                "address": "0x" + "56" * 20,
                "source": "manual",
                "label": "No owner",
            },
        )
    assert listed.status_code == 401
    assert added.status_code == 401


async def test_wallet_api_adds_and_bulk_refreshes(database_url, monkeypatch):
    async def fake_balance(client, address):
        return {"assets": [{"symbol": "ETH", "amount": "2"}], "coverage": "test"}

    monkeypatch.setattr(wallets, "_evm_balance", fake_balance)
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        setup = await client.post(
            "/auth/setup",
            json={"email": "operator@example.com", "password": "a" * 16},
        )
        assert setup.status_code == 200, setup.text
        headers = {"authorization": f"Bearer {setup.json()['token']}"}
        added = await client.post(
            "/wallets",
            headers=headers,
            json={
                "address_family": "evm",
                "address": "0x" + "78" * 20,
                "source": "metamask",
                "label": "Browser account",
                "discovered_by": "browser_extension",
            },
        )
        refreshed = await client.post("/wallets/refresh", headers=headers)

    assert added.status_code == 201, added.text
    assert refreshed.status_code == 201, refreshed.text
    assert refreshed.json()["accounts"][0]["balance"]["assets"][0] == {
        "symbol": "ETH",
        "amount": "2",
    }
