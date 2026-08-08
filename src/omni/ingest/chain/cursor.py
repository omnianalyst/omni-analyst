"""Per-chain block cursor: resumable, monotonic block-range traversal.

Replaces the traversal strategy behind ``onchain.py::_fetch_latest_block``,
which grabs the single most recent block once per fill. That samples a sliver
and silently misses everything between fills -- so at ~12s per Ethereum block
"exchange inflows" computed from it is anecdote, and the
``flow.exchange_reserve`` producer would calibrate on whatever happened to be
in the last block when the loop fired. A cursor makes traversal resumable and,
critically, makes coverage *knowable*: the gap engine can see which block range
has been read, which is the difference between a gap it can detect and a hole
nobody can see.

``next_range`` is the whole design. With no cursor row it returns a bounded
range ENDING at ``head`` -- it does not claim genesis-to-head, because that
would assert coverage that was never fetched (a large chain has millions of
unfetched blocks below head). With a cursor at block N it returns
``N+1 .. min(H, N+max_span)``: no gap (starts at N+1, not N) and no overlap
(never re-reads N). When ``N >= H`` the chain is caught up and ``next_range``
returns ``None`` -- caught-up is a normal outcome, not an error and not an
empty range.

Monotonicity is the load-bearing guarantee. ``advance`` may never move the
cursor backwards: a rewound cursor re-emits blocks already read, and duplicate
transfers are indistinguishable from real repeated ones. The guard is a
``WHERE`` clause on the UPSERT, not a Python check, so it survives two workers
racing on one chain -- whichever order they commit in, the higher block wins.
``to_block <= stored`` is a no-op (not an error, not an overwrite): equal is a
re-acknowledgement, less is a stale worker, and neither bumps ``updated_at``
and lies about when the cursor last moved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg

_GET_CURSOR = "SELECT last_block FROM chain_cursor WHERE chain = $1"

# Monotonic UPSERT. The INSERT path runs when no row exists (first advance for
# this chain) and always inserts. On conflict the DO UPDATE is gated by
# ``$2 > chain_cursor.last_block``: a stale or equal to_block is a no-op, so the
# cursor can never move backwards regardless of commit ordering. The guard is
# in SQL, not Python, so it holds under concurrent advance() calls -- the last
# writer cannot clobber a higher value an earlier writer already committed.
_ADVANCE = """
INSERT INTO chain_cursor (chain, last_block, last_block_at)
VALUES ($1, $2, $3)
ON CONFLICT (chain) DO UPDATE SET
    last_block    = EXCLUDED.last_block,
    last_block_at = EXCLUDED.last_block_at,
    updated_at    = now()
WHERE $2 > chain_cursor.last_block
"""


@dataclass(frozen=True)
class BlockRange:
    chain: str
    start_block: int
    end_block: int  # inclusive

    def __post_init__(self) -> None:
        if self.start_block < 0 or self.end_block < 0:
            raise ValueError("block numbers cannot be negative")
        if self.end_block < self.start_block:
            raise ValueError("end_block precedes start_block")


async def get_cursor(pool: asyncpg.Pool, chain: str) -> int | None:
    return await pool.fetchval(_GET_CURSOR, chain)


async def advance(
    pool: asyncpg.Pool,
    chain: str,
    *,
    to_block: int,
    block_time: datetime,
) -> None:
    await pool.execute(_ADVANCE, chain, to_block, block_time)


async def next_range(
    pool: asyncpg.Pool,
    chain: str,
    *,
    head: int,
    max_span: int,
) -> BlockRange | None:
    if head < 0:
        raise ValueError("head must be non-negative")
    if max_span < 1:
        raise ValueError("max_span must be at least 1")

    last = await get_cursor(pool, chain)

    if last is None:
        # First read: a bounded range ENDING at head. We do not claim
        # genesis-to-head -- on a large chain that asserts coverage of millions
        # of unfetched blocks. start is head - max_span + 1 so the range spans
        # exactly max_span blocks; it is clamped to 0 only when fewer than
        # max_span blocks exist, which is honest (there is nothing below 0 to
        # claim we skipped).
        start = max(0, head - max_span + 1)
        return BlockRange(chain, start, head)

    if last >= head:
        # Caught up: every block up to head is already read. This is a normal
        # outcome -- returning an empty range or raising would make "nothing
        # to do" look like a failure.
        return None

    # No gap (start = last + 1, so the block at `last` is never re-read) and no
    # overlap. end is capped at head and at last + max_span, so one call cannot
    # request a million blocks.
    start = last + 1
    end = min(head, last + max_span)
    return BlockRange(chain, start, end)
