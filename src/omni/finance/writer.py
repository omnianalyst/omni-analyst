"""One-connection, one-transaction boundary for finance writes.

Every write path here historically ran as separate autocommit statements, so
a failure midway left a split parent with half its children committed, and
two concurrent imports read the same pre-state and both matched the same bank
row (there is no imported-id uniqueness). The decorator below pins a finance
write to one connection and one transaction, and serialises per user on the
users row so read-then-write matching cannot race a second importer.

Provider data (bank HTTP) must be fetched BEFORE entering: the transaction
holds a row lock for its duration, and a slow upstream inside it would park
that lock for the length of an HTTP timeout.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any
from uuid import UUID

from omni.db_scope import BoundPool

_LOCK_USER = "SELECT id FROM users WHERE id = $1 FOR UPDATE"


def finance_write(func: Callable) -> Callable:
    """Run a `write(pool, user_id, ...)` finance function on one transaction.

    The wrapped function receives a `BoundPool` pinned to a single connection;
    its own statements and those of the helpers it calls all commit together
    or not at all, and the per-user lock orders concurrent writers.
    """

    @functools.wraps(func)
    async def wrapper(pool: Any, user_id: UUID, *args: Any, **kwargs: Any):
        if isinstance(pool, BoundPool):
            return await func(pool, user_id, *args, **kwargs)
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(_LOCK_USER, user_id)
            return await func(BoundPool(conn), user_id, *args, **kwargs)

    return wrapper
