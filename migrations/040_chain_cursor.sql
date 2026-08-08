-- Per-chain block cursor: makes on-chain traversal resumable and coverage
-- knowable.
--
-- onchain.py::_fetch_latest_block grabs the single most recent block once per
-- fill. There is no backfill, no resume, and no way to know what was already
-- seen -- so "exchange inflows" computed from it is anecdote, and the gap
-- engine cannot tell a covered range from a hole. One row per chain records
-- the highest block whose contents have been read into the store, plus the
-- timestamp of that block (the claim event_date for the blocks behind it).
--
-- last_block is monotonic: advance() never moves it backwards. The guard lives
-- in the UPDATE's WHERE clause, not in Python, so two racing workers cannot
-- rewind the cursor and re-emit blocks already claimed -- which would produce
-- duplicate flow claims indistinguishable from real repeated transfers.
-- The CHECK is >= 0, not > 0: genesis is block 0 and is a valid cursor position.

CREATE TABLE chain_cursor (
    chain         TEXT PRIMARY KEY,
    last_block    BIGINT NOT NULL,
    last_block_at TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chain_cursor_block_is_not_negative CHECK (last_block >= 0)
);
