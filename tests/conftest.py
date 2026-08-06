import os

import pytest
import pytest_asyncio

from omni.db import connect, migrate

# A database of its own. The fixtures TRUNCATE, so pointing this at the
# running instance's database means every test run silently destroys whatever
# coverage the background has accumulated -- which is exactly what happened
# once before this line existed.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5434/omni_v2_test",
)


@pytest.fixture
def database_url() -> str:
    return TEST_DATABASE_URL


@pytest_asyncio.fixture
async def db():
    client = await connect(TEST_DATABASE_URL)
    await migrate(client)
    try:
        yield client
    finally:
        await client.close()


# The auth rate limiter is module-level in-memory state; reset it before each
# test so a suite that logs in many times does not flake on the per-IP ceiling.
@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from omni.auth.ratelimit import reset_for_test

    reset_for_test()
    yield
