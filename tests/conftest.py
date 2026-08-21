import asyncio
import contextlib
import os

import asyncpg
import pytest
import pytest_asyncio

from omni.db import connect, migrate

# A database of its own. The fixtures TRUNCATE, so pointing this at the
# running instance's database means every test run silently destroys whatever
# coverage the background has accumulated -- which is exactly what happened
# once before this line existed.
#
# And one per SESSION, not one shared by every session. Sessions run
# concurrently here as a matter of course -- the agent fleet, a gate run
# alongside a manual one -- and a shared database means one session's TRUNCATE
# deletes rows another is mid-test on. It surfaces as unique and foreign-key
# violations in files neither run touched, which reads as a real defect and is
# not one.
#
# An explicit TEST_DATABASE_URL is honoured exactly, and nothing is created or
# dropped under it: CI and the operator's run script both name a database
# deliberately. Without one the session gets a database named for its pid,
# created on first use and dropped at the end.
_EXPLICIT_URL = os.environ.get("TEST_DATABASE_URL")
_SERVER = "postgresql://postgres:postgres@localhost:5434"
_SESSION_DATABASE = f"omni_v2_test_{os.getpid()}"
_MAINTENANCE_URL = f"{_SERVER}/postgres"

TEST_DATABASE_URL = _EXPLICIT_URL or f"{_SERVER}/{_SESSION_DATABASE}"

_session_database_ready = False


async def _ensure_session_database() -> None:
    global _session_database_ready

    if _EXPLICIT_URL is not None or _session_database_ready:
        return

    conn = await asyncpg.connect(_MAINTENANCE_URL)
    try:
        # A session that crashed before its teardown leaves its database
        # behind. The pid is unique among live processes, so anything already
        # under this name belongs to a dead session and is safe to replace.
        await conn.execute(f'DROP DATABASE IF EXISTS "{_SESSION_DATABASE}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{_SESSION_DATABASE}"')
    finally:
        await conn.close()

    client = await connect(TEST_DATABASE_URL)
    try:
        await migrate(client)
    finally:
        await client.close()

    _session_database_ready = True


@pytest_asyncio.fixture
async def database_url() -> str:
    await _ensure_session_database()
    return TEST_DATABASE_URL


@pytest_asyncio.fixture
async def db():
    await _ensure_session_database()
    client = await connect(TEST_DATABASE_URL)
    await migrate(client)
    try:
        yield client
    finally:
        await client.close()


async def _drop_session_database() -> None:
    conn = await asyncpg.connect(_MAINTENANCE_URL)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_SESSION_DATABASE}" WITH (FORCE)')
    finally:
        await conn.close()


def pytest_sessionfinish(session, exitstatus):
    if _EXPLICIT_URL is not None or not _session_database_ready:
        return
    # A failed drop costs one stale database, which the next session holding
    # this pid replaces. Failing the run over it would cost more.
    with contextlib.suppress(Exception):
        asyncio.run(_drop_session_database())


# The auth rate limiter is module-level in-memory state; reset it before each
# test so a suite that logs in many times does not flake on the per-IP ceiling.
@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from omni.auth.ratelimit import reset_for_test

    reset_for_test()
    yield
