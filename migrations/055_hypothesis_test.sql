-- Every hypothesis this project has ever tested, mirrored from the research
-- registry so the running system can show its own search.
--
-- The JSONL registry at docs/research/hypothesis_registry.jsonl remains the
-- single WRITER. It is what `harness.evaluate()` appends to, and the
-- significance bar is computed from it during a research run. This table is a
-- one-way published mirror: the image ships only src/ and migrations/, so a
-- deployed API cannot read that file at all.
--
-- Keeping one writer is the point. The bar is sqrt(2 ln N) over every test ever
-- run, and two independently-written histories would give two different N --
-- which is precisely the arithmetic dishonesty the registry exists to prevent.
--
-- The primary key makes the mirror idempotent for the same reason
-- _neutron_migrations uses one: re-running the sync must be a no-op, not a
-- duplicate. (name, recorded_at) is the natural identity -- the registry stamps
-- recorded_at with an fsync'd UTC timestamp at append time, and the same
-- hypothesis retested later is a genuinely new row.

CREATE TABLE IF NOT EXISTS hypothesis_test (
    name         TEXT        NOT NULL,
    source       TEXT        NOT NULL,
    cells        INTEGER     NOT NULL CHECK (cells >= 1),
    verdict      TEXT        NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL,
    detail       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    mirrored_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (name, recorded_at)
);

-- A test that produced no statistics is not a test; recording zero cells would
-- understate the search and deflate the bar. Enforced here as well as in
-- Registry.record so a bad mirror cannot corrupt what the API reports.

CREATE INDEX IF NOT EXISTS hypothesis_test_recorded_idx
    ON hypothesis_test (recorded_at DESC);

-- DOWN

DROP TABLE IF EXISTS hypothesis_test;
