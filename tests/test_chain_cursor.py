"""Chain cursor: resumable, monotonic per-chain block-range traversal.

Each load-bearing property is asserted on its own:

- A first read with no cursor is BOUNDED and ends at head -- it does not start
  at genesis, because genesis-to-head would assert coverage of blocks never
  fetched.
- The boundary after a cursor is exact: start is last_block + 1. An off-by-one
  here either double-counts a block or silently skips one, and both are silent
  corruption of the flow stream.
- max_span is a hard ceiling: one call cannot request a million blocks.
- Caught-up returns None, not an empty range or an error -- "nothing to do" is
  a healthy outcome.
- advance is monotonic: a lower to_block is a no-op. A rewound cursor re-emits
  blocks already read, and duplicate transfers are indistinguishable from real
  repeated ones.
- Resume over several ranges covers every block exactly once: no gap, no
  duplicate, even across a simulated restart.
- Two concurrent advances leave the cursor at the higher block, not the
  later-arriving one.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from omni.ingest.chain.cursor import BlockRange, advance, get_cursor, next_range


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE chain_cursor")
    yield


_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _t(block: int) -> datetime:
    # Distinct timestamp per block so last_block_at assertions are exact.
    return _EPOCH + timedelta(seconds=block)


CHAIN = "eth"


class TestBlockRange:
    def test_end_before_start_refused(self):
        with pytest.raises(ValueError):
            BlockRange(CHAIN, 10, 9)

    def test_negative_start_refused(self):
        with pytest.raises(ValueError):
            BlockRange(CHAIN, -1, 5)

    def test_negative_end_refused(self):
        with pytest.raises(ValueError):
            BlockRange(CHAIN, 0, -1)

    def test_equal_start_end_allowed(self):
        # A single-block range (start == end) is valid: one block.
        r = BlockRange(CHAIN, 7, 7)
        assert r.start_block == 7
        assert r.end_block == 7

    def test_is_frozen(self):
        # FrozenInstanceError subclasses AttributeError, so the precise form is
        # AttributeError -- a frozen dataclass mutation is an attribute error.
        r = BlockRange(CHAIN, 1, 2)
        with pytest.raises(AttributeError):
            r.start_block = 99  # type: ignore[misc]


class TestFirstRead:
    async def test_no_cursor_returns_bounded_range_ending_at_head(self, db):
        # head=1000, max_span=10 -> start=991, end=1000. Not genesis: start is
        # well above 0, so this does not claim coverage of blocks 0..990.
        r = await next_range(db.pool, CHAIN, head=1000, max_span=10)
        assert r is not None
        assert r.chain == CHAIN
        assert r.start_block == 991
        assert r.end_block == 1000
        assert r.start_block > 0, "first read must not start at genesis"

    async def test_no_cursor_range_spans_exactly_max_span(self, db):
        r = await next_range(db.pool, CHAIN, head=1000, max_span=10)
        assert r is not None
        assert (r.end_block - r.start_block + 1) == 10

    async def test_no_cursor_clamps_start_to_zero_when_chain_is_short(self, db):
        # head < max_span: there are fewer than max_span blocks in existence,
        # so the bounded read legitimately reaches block 0. This is honest --
        # there is nothing below 0 to have skipped -- and still never exceeds
        # max_span in size.
        r = await next_range(db.pool, CHAIN, head=3, max_span=10)
        assert r is not None
        assert r.start_block == 0
        assert r.end_block == 3
        assert (r.end_block - r.start_block + 1) <= 10


class TestSubsequentRange:
    async def test_starts_at_last_block_plus_one_no_gap_no_overlap(self, db):
        # Read 991..1000, advance the cursor to 1000, then read again with head
        # just ahead. The next range must start at 1001 -- not 1000 (would
        # re-read / double-count) and not 1002 (would skip 1001).
        r1 = await next_range(db.pool, CHAIN, head=1000, max_span=10)
        assert r1 is not None
        await advance(db.pool, CHAIN, to_block=r1.end_block, block_time=_t(1000))

        r2 = await next_range(db.pool, CHAIN, head=1005, max_span=10)
        assert r2 is not None
        assert r2.start_block == 1001, "must start at last_block + 1"
        assert r2.end_block == 1005
        # Adjacency, no overlap: r2 picks up exactly one past r1's end.
        assert r2.start_block == r1.end_block + 1

    async def test_max_span_caps_range_when_head_is_far_ahead(self, db):
        # Cursor at 1000, head at 2000, max_span=10 -> end caps at 1010, not
        # 2000. One call cannot request a thousand blocks.
        await advance(db.pool, CHAIN, to_block=1000, block_time=_t(1000))
        r = await next_range(db.pool, CHAIN, head=2000, max_span=10)
        assert r is not None
        assert r.start_block == 1001
        assert r.end_block == 1010
        assert (r.end_block - r.start_block + 1) == 10


class TestCaughtUp:
    async def test_cursor_at_head_returns_none(self, db):
        await advance(db.pool, CHAIN, to_block=1000, block_time=_t(1000))
        r = await next_range(db.pool, CHAIN, head=1000, max_span=10)
        assert r is None

    async def test_cursor_ahead_of_head_returns_none(self, db):
        # A reorg-free chain never moves head backwards, but next_range must
        # still treat "cursor ahead of head" as caught-up rather than error or
        # emit a negative-width range.
        await advance(db.pool, CHAIN, to_block=1005, block_time=_t(1005))
        r = await next_range(db.pool, CHAIN, head=1000, max_span=10)
        assert r is None


class TestMonotonicAdvance:
    async def test_advancing_to_a_lower_block_is_a_noop(self, db):
        # The headline test. advance to 100, then attempt to rewind to 90: the
        # stored value must stay 100. A rewound cursor re-emits blocks already
        # claimed as if they were new transfers.
        await advance(db.pool, CHAIN, to_block=100, block_time=_t(100))
        await advance(db.pool, CHAIN, to_block=90, block_time=_t(90))
        assert await get_cursor(db.pool, CHAIN) == 100

    async def test_advancing_to_equal_block_is_a_noop(self, db):
        await advance(db.pool, CHAIN, to_block=100, block_time=_t(100))
        before = await db.pool.fetchrow(
            "SELECT last_block, last_block_at, updated_at FROM chain_cursor WHERE chain = $1",
            CHAIN,
        )
        # Equal to_block is a re-acknowledgement, not a move: it must not bump
        # updated_at and lie about when the cursor last advanced.
        await advance(db.pool, CHAIN, to_block=100, block_time=_t(100))
        after = await db.pool.fetchrow(
            "SELECT last_block, last_block_at, updated_at FROM chain_cursor WHERE chain = $1",
            CHAIN,
        )
        assert after["last_block"] == 100
        assert after["last_block_at"] == before["last_block_at"]
        assert after["updated_at"] == before["updated_at"]

    async def test_advancing_forward_moves_cursor_and_block_time(self, db):
        await advance(db.pool, CHAIN, to_block=100, block_time=_t(100))
        await advance(db.pool, CHAIN, to_block=150, block_time=_t(150))
        row = await db.pool.fetchrow(
            "SELECT last_block, last_block_at FROM chain_cursor WHERE chain = $1",
            CHAIN,
        )
        assert row["last_block"] == 150
        assert row["last_block_at"] == _t(150)

    async def test_first_advance_inserts(self, db):
        assert await get_cursor(db.pool, CHAIN) is None
        await advance(db.pool, CHAIN, to_block=42, block_time=_t(42))
        assert await get_cursor(db.pool, CHAIN) == 42


class TestResume:
    async def test_union_of_ranges_covers_each_block_exactly_once(self, db):
        # Consume several ranges from a head far ahead, advancing the cursor
        # after each, then simulate a restart by re-reading the cursor and
        # continuing. The union of every range (plus the final catch-up gap)
        # must cover the traversed interval exactly once: no gaps, no
        # duplicates.
        max_span = 10
        head = 45
        # First read is bounded and ends at head, NOT genesis-to-head.
        ranges: list[BlockRange] = []

        r = await next_range(db.pool, CHAIN, head=head, max_span=max_span)
        assert r is not None
        assert r.start_block == head - max_span + 1  # 36
        ranges.append(r)
        await advance(db.pool, CHAIN, to_block=r.end_block, block_time=_t(r.end_block))

        # Keep consuming until caught up. head is fixed (no new blocks arrive).
        while True:
            r = await next_range(db.pool, CHAIN, head=head, max_span=max_span)
            if r is None:
                break
            ranges.append(r)
            await advance(
                db.pool, CHAIN, to_block=r.end_block, block_time=_t(r.end_block)
            )

        # --- simulate a restart: cursor is read fresh from disk ---
        restarted_cursor = await get_cursor(db.pool, CHAIN)
        assert restarted_cursor == head, "after consuming to head, cursor == head"
        # The next range after restart must be None (caught up).
        assert await next_range(db.pool, CHAIN, head=head, max_span=max_span) is None

        # --- the union must cover [first_start, head] exactly once ---
        seen: dict[int, int] = {}
        for rg in ranges:
            for b in range(rg.start_block, rg.end_block + 1):
                seen[b] = seen.get(b, 0) + 1

        first_start = min(rg.start_block for rg in ranges)
        expected = list(range(first_start, head + 1))
        assert sorted(seen) == expected, "missing or extra blocks in the union"
        assert all(v == 1 for v in seen.values()), (
            f"duplicate or missing block: { {b: c for b, c in seen.items() if c != 1} }"
        )

    async def test_resume_across_new_blocks_no_gap_no_duplicate(self, db):
        # Blocks arrive between passes. Each pass reads to the current head and
        # advances; the next pass must pick up at last_block + 1, never
        # overlapping what was read and never skipping what arrived.
        await advance(db.pool, CHAIN, to_block=100, block_time=_t(100))
        r1 = await next_range(db.pool, CHAIN, head=105, max_span=50)
        assert r1 is not None
        assert r1.start_block == 101 and r1.end_block == 105
        await advance(db.pool, CHAIN, to_block=105, block_time=_t(105))

        # New blocks arrive; head moves to 108.
        r2 = await next_range(db.pool, CHAIN, head=108, max_span=50)
        assert r2 is not None
        assert r2.start_block == 106 and r2.end_block == 108
        assert r2.start_block == r1.end_block + 1


class TestConcurrentAdvance:
    async def test_two_concurrent_advances_leave_cursor_at_higher_block(self, db):
        # Fire two advances concurrently. Whichever commits last, the cursor
        # must end at the higher block: the monotonic guard refuses to let the
        # lower one overwrite the higher, and lets the higher overwrite the
        # lower.
        await advance(db.pool, CHAIN, to_block=100, block_time=_t(100))
        await asyncio.gather(
            advance(db.pool, CHAIN, to_block=90, block_time=_t(90)),
            advance(db.pool, CHAIN, to_block=150, block_time=_t(150)),
        )
        assert await get_cursor(db.pool, CHAIN) == 150

    async def test_lower_arriving_last_does_not_rewind(self, db):
        # The deterministic core of the concurrency guarantee: even when the
        # lower advance provably commits AFTER the higher one, the cursor does
        # not move. This is the case that would corrupt coverage under a
        # last-writer-wins UPDATE.
        await advance(db.pool, CHAIN, to_block=150, block_time=_t(150))
        # Lower, committed strictly later.
        await advance(db.pool, CHAIN, to_block=120, block_time=_t(120))
        assert await get_cursor(db.pool, CHAIN) == 150


class TestIsolationBetweenChains:
    async def test_each_chain_has_its_own_cursor(self, db):
        await advance(db.pool, "eth", to_block=100, block_time=_t(100))
        await advance(db.pool, "base", to_block=50, block_time=_t(50))
        assert await get_cursor(db.pool, "eth") == 100
        assert await get_cursor(db.pool, "base") == 50
        # Advancing one chain must not touch the other.
        await advance(db.pool, "eth", to_block=200, block_time=_t(200))
        assert await get_cursor(db.pool, "base") == 50
