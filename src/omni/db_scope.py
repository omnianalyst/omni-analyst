"""Run code written against a pool against one pinned connection.

`portfolio.orders` and `portfolio.state` each open their own connection and
transaction from the pool they are handed. That is the right default for a
single write, and the wrong one when two writes must commit together: a fill
recorded in one transaction and applied to positions in a second is a ledger
that can say a trade happened to a book it never touched -- after a crash
between them, no path re-applies it, because the order already reads FILLED.

`BoundPool` hands those modules one already-open connection. Their own
`conn.transaction()` blocks become savepoints on the caller's transaction, so
each module keeps its atomicity guarantees and the caller's outer transaction
becomes the unit of commit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any


class BoundPool:
    """A pool-shaped object that always yields the same connection."""

    def __init__(self, connection: Any):
        self._connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self._connection

    async def execute(self, *args: Any, **kwargs: Any):
        return await self._connection.execute(*args, **kwargs)

    async def fetch(self, *args: Any, **kwargs: Any):
        return await self._connection.fetch(*args, **kwargs)

    async def fetchrow(self, *args: Any, **kwargs: Any):
        return await self._connection.fetchrow(*args, **kwargs)

    async def fetchval(self, *args: Any, **kwargs: Any):
        return await self._connection.fetchval(*args, **kwargs)
