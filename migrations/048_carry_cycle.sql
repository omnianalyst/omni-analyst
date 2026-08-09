-- Carry cycle log: what each rebalance settled, and the boundary the next one starts from.
--
-- `run_carry_cycle` takes `funding_since` from its caller and refuses to invent
-- one, for a reason the module states plainly: every default is wrong in the
-- direction that loses money quietly. One settlement period skips every
-- settlement in a longer gap; the portfolio's inception re-walks history already
-- collected. Until this table existed there was no caller other than a test, so
-- the boundary lived in whatever the test passed. A process that must be
-- restarted, or an operator running a cycle every six weeks from a shell, has
-- nowhere to read it from -- and `apply_funding` is idempotent on
-- `(portfolio, venue, symbol, funding_time)`, so a re-walk is refused by the
-- database while a SKIP is silent and simply understates the only thing this
-- book earns.
--
-- **`funding_settled_through` is the whole point of the table, and it is
-- nullable on purpose.** A cycle halts before it settles anything when the book
-- holds an unpaired leg or the venue disagrees with the book; a cycle that halts
-- after settling has still applied the window. Recording `as_of` as the new
-- boundary in the first case would advance past a window whose settlements were
-- never applied -- the silent gap above, created by the bookkeeping meant to
-- prevent it. NULL says "this cycle did not settle", the boundary does not move,
-- and the next cycle re-walks a window whose duplicate writes the database
-- refuses anyway.
--
-- The next cycle's window therefore opens at
-- `COALESCE(max(funding_settled_through), min(funding_since))`: the furthest
-- point actually settled, or -- if no cycle has ever settled -- the origin the
-- first cycle was given. The origin is read back rather than re-supplied so that
-- an operator cannot quietly restate it on a later run.
--
-- Every money column is NUMERIC for the reason `funding_accrual` gives: these
-- are small numbers accumulated over the life of the book, and a binary error
-- per cycle accumulates into the P&L of the strategy the table exists to record.

CREATE TABLE carry_cycle (
    portfolio_id            UUID NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,

    -- The venue is part of the key, not a label. `run_carry_cycle` treats the
    -- portfolio as a dedicated book at one venue, and a boundary read that
    -- pooled two venues would settle one venue's window with another's clock.
    venue                   TEXT NOT NULL,

    -- The rebalance instant. Also the upper bound, inclusive, of the funding
    -- window this cycle settled.
    as_of                   TIMESTAMPTZ NOT NULL,

    -- The lower bound, exclusive. Stated by the caller, recorded so the chain
    -- of windows can be audited for gaps after the fact.
    funding_since           TIMESTAMPTZ NOT NULL,

    -- `as_of` once funding was applied, NULL when the cycle halted before
    -- reaching it. See above: this column is why the table exists.
    funding_settled_through TIMESTAMPTZ,

    halted                  BOOLEAN NOT NULL,
    halt_reason             TEXT,

    -- The selector declining to rank. Not a halt and not a failure: the book
    -- held still, which Finding 9 says is the correct response to thin data.
    abstention              TEXT,

    funding_collected       NUMERIC NOT NULL,
    fees_paid               NUMERIC NOT NULL,
    modelled_turnover_cost  NUMERIC NOT NULL,

    pairs_opened            INTEGER NOT NULL,
    pairs_closed            INTEGER NOT NULL,
    pairs_held              INTEGER NOT NULL,

    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (portfolio_id, venue, as_of),

    -- Mirrors `CarryCycleResult.__post_init__`. A halted cycle that lost its
    -- reason on the way to the database is an operator reading "something went
    -- wrong" six weeks later with no way to find out what.
    CONSTRAINT carry_cycle_halt_names_its_reason
        CHECK (halted = (halt_reason IS NOT NULL)),

    -- The window a cycle settles runs forward. Reversed bounds would settle
    -- nothing while looking like a completed cycle, and would then advance the
    -- boundary past everything in between.
    CONSTRAINT carry_cycle_settles_forward
        CHECK (funding_since <= as_of),

    -- The boundary a cycle reaches is its own `as_of` or nothing. A value
    -- between the two would claim a partial settlement, which no path produces:
    -- the funding loop either runs over the whole window or is never reached.
    CONSTRAINT carry_cycle_settled_through_is_the_upper_bound
        CHECK (funding_settled_through IS NULL OR funding_settled_through = as_of),

    -- An abstention is the book holding still, so it cannot have traded.
    CONSTRAINT carry_cycle_abstention_traded_nothing
        CHECK (abstention IS NULL OR (pairs_opened = 0 AND pairs_closed = 0)),

    CONSTRAINT carry_cycle_counts_are_counts
        CHECK (pairs_opened >= 0 AND pairs_closed >= 0 AND pairs_held >= 0),

    -- NUMERIC accepts 'NaN' and sorts it above every number, so a NaN cost would
    -- pass any range check written against these columns and then poison every
    -- sum taken over the book's history.
    CONSTRAINT carry_cycle_amounts_are_numbers
        CHECK (funding_collected <> 'NaN'::numeric
               AND fees_paid <> 'NaN'::numeric
               AND modelled_turnover_cost <> 'NaN'::numeric),

    CONSTRAINT carry_cycle_names_its_venue
        CHECK (venue <> '')
);

-- The two reads this table exists for, both taken per book per venue: the
-- boundary the next cycle opens at, and the last cycle that ran to completion
-- (which is what the rebalance cadence is measured from -- a halted cycle did
-- not complete and must be re-runnable at once).
CREATE INDEX carry_cycle_by_book
    ON carry_cycle (portfolio_id, venue, as_of DESC);

-- DOWN

DROP TABLE IF EXISTS carry_cycle;
