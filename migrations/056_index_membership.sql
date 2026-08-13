-- Dated index membership: what an index held on a date somebody actually looked.
--
-- Until now membership existed only as the static tuple list in
-- `_seed_data.py`, overwritten wholesale on every refresh. That is why the
-- ETF-versus-constituent experiment (docs/ETF_PORTFOLIO_EXPERIMENT.md) is
-- survivorship-biased: today's members are applied backward, so every company
-- that was dropped from the index -- which is disproportionately the ones that
-- did badly -- is invisible to the backtest. This table cannot repair that. It
-- can only start accumulating the record from the first observation forward,
-- which is why it is worth adding now rather than when it is needed.
--
-- `present` is what makes it a history rather than a list. A departure is
-- written as `present = false` on the observation that first missed it, so
-- "was EA in the S&P 500 on this date" has an answer, not a silence. A row's
-- absence means nobody looked, and that must stay distinguishable from a
-- member being gone.
--
-- `observed_on` is the date the constituent list was derived from its
-- canonical source (`SP500_ACCESSED_ON`), NOT the clock at seed time. The
-- seeder runs on every boot; stamping now() would have a boot in November
-- assert that the index held these names in November, when all that happened
-- was an August list being replayed. That is a fabricated observation in the
-- exact shape this store exists to refuse.
--
-- The primary key is therefore the whole identity of an observation, and makes
-- re-seeding idempotent for the same reason _neutron_migrations' does: a second
-- run must be a no-op, not a duplicate. Writes are INSERT ... ON CONFLICT DO
-- NOTHING -- an observation already recorded is never rewritten, because
-- editing history is how a survivorship-biased record is created rather than
-- corrected.

CREATE TABLE IF NOT EXISTS index_membership (
    index_symbol   TEXT        NOT NULL,
    member_symbol  TEXT        NOT NULL,
    observed_on    DATE        NOT NULL,
    present        BOOLEAN     NOT NULL,
    source         TEXT        NOT NULL,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (index_symbol, member_symbol, observed_on)
);

-- The read that matters is "the most recent observation of this index, and the
-- one before it" -- resolving a snapshot date, then its rows. Both are served
-- by leading on index_symbol with observed_on descending.
CREATE INDEX IF NOT EXISTS index_membership_observed_idx
    ON index_membership (index_symbol, observed_on DESC);

-- DOWN

DROP TABLE IF EXISTS index_membership;
