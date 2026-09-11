"""Ops scripts must operate the CONFIGURED book, not `LIMIT 1`.

A multi-portfolio install with `SELECT id FROM portfolio LIMIT 1` marks or
operates an arbitrary row under a hard-coded owner's audience (U04). The
scope helper requires both UUIDs from the environment and validates the
ownership join before anything else runs.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omni.trading.ops_scope import configured_book


async def _user(db, *, active=True) -> uuid4:
    uid = uuid4()
    await db.pool.execute(
        "INSERT INTO users (id, email, password_hash, active) "
        "VALUES ($1, $2, 'x', $3)",
        uid,
        f"{uid}@example.com",
        active,
    )
    return uid


async def _portfolio(db, owner) -> uuid4:
    return await db.pool.fetchval(
        "INSERT INTO portfolio (user_id, name, base_currency) "
        "VALUES ($1, 'book', 'USD') RETURNING id",
        owner,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


async def test_unset_scope_refuses(db, monkeypatch):
    monkeypatch.delenv("OMNI_TRADING_PORTFOLIO_ID", raising=False)
    monkeypatch.delenv("OMNI_TRADING_OWNER_ID", raising=False)
    with pytest.raises(ValueError, match="refusing to pick one arbitrarily"):
        await configured_book(db.pool)


async def test_malformed_scope_refuses(db, monkeypatch):
    monkeypatch.setenv("OMNI_TRADING_PORTFOLIO_ID", "not-a-uuid")
    monkeypatch.setenv("OMNI_TRADING_OWNER_ID", str(uuid4()))
    with pytest.raises(ValueError):
        await configured_book(db.pool)


async def test_the_configured_book_is_returned_not_the_first_row(db, monkeypatch):
    owner = await _user(db)
    first = await _portfolio(db, owner)
    second = await _portfolio(db, owner)
    assert first != second

    monkeypatch.setenv("OMNI_TRADING_PORTFOLIO_ID", str(second))
    monkeypatch.setenv("OMNI_TRADING_OWNER_ID", str(owner))

    pid, inception, resolved_owner = await configured_book(db.pool)
    assert pid == second
    assert resolved_owner == owner
    assert inception is not None


async def test_another_owners_book_is_refused(db, monkeypatch):
    a = await _user(db)
    b = await _user(db)
    b_book = await _portfolio(db, b)

    monkeypatch.setenv("OMNI_TRADING_PORTFOLIO_ID", str(b_book))
    monkeypatch.setenv("OMNI_TRADING_OWNER_ID", str(a))

    with pytest.raises(ValueError, match="does not belong"):
        await configured_book(db.pool)


async def test_an_inactive_owner_is_refused(db, monkeypatch):
    owner = await _user(db, active=False)
    book = await _portfolio(db, owner)

    monkeypatch.setenv("OMNI_TRADING_PORTFOLIO_ID", str(book))
    monkeypatch.setenv("OMNI_TRADING_OWNER_ID", str(owner))

    with pytest.raises(ValueError, match="does not belong"):
        await configured_book(db.pool)
