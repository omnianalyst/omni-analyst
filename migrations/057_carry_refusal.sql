-- Carry refusals: the cycles that were declined, and what they were declined
-- against.
--
-- `carry_cycle` records what the book did. A refused cycle does nothing, so it
-- wrote nothing: `run_due_cycle` raises `CarryRunRefused` before
-- `run_carry_cycle` is reached, and the reason existed only in the log of the
-- process that ran. On a book that rebalances every six weeks that is roughly
-- 41 of every 42 days producing no row anywhere.
--
-- The consequence is the reason this table exists: **no absence of rows could
-- distinguish a correct refusal from a scheduler that never fired.** Both look
-- like silence between rebalances, and one of them is a book nobody is running.
-- That is a blind spot on a system holding real money, and it is closed by
-- writing the refusal rather than by watching the loop, because a refusal is
-- exactly the evidence that the loop ran.
--
-- It is a separate table rather than a nullable-outcome `carry_cycle` row on
-- purpose. Every column on that table describes a settlement -- the funding
-- window, what was collected, what was paid, the boundary the next cycle opens
-- at -- and a refusal has none of those, because nothing was read and nothing
-- was traded. A row of NULLs in the cycle log would have to be excluded by
-- every existing reader of the book's history, and the first reader that forgot
-- would report a refusal as a cycle that earned zero.
--
-- `guard` is the machine-readable identity of the check that fired; `reason` is
-- the sentence the runner already produced for the operator. Both are kept: the
-- sentence explains, the guard groups. The set of valid guards is pinned in
-- `omni.trading.carry_runner` and by test rather than by a CHECK constraint
-- here, so that adding a guard is one change instead of a migration plus a
-- deploy ordering problem.
--
-- The boundary columns are the state the decision was taken against, recorded
-- at write time for the same reason a prediction's barrier is: a refusal
-- re-derived later from a book that has since moved is a different refusal.
-- They are nullable because the window guard fires before the boundary is ever
-- read -- and `guard` says so, so a NULL there is "the runner never got that
-- far" rather than an unexplained absence.

CREATE TABLE carry_refusal (
    portfolio_id UUID NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,

    -- Part of the key rather than a label, as on carry_cycle: the cadence and
    -- the funding boundary are per venue, so a refusal pooled across venues
    -- would describe a decision no runner took.
    venue        TEXT        NOT NULL,

    -- The instant the runner was asked to act at -- the `now` it was given, not
    -- the clock at write time. That is the instant the guards were evaluated
    -- against, and stamping the write instead would make a cycle replayed with
    -- an explicit --as-of record a decision at a moment nothing was decided.
    attempted_at TIMESTAMPTZ NOT NULL,

    guard        TEXT        NOT NULL,
    reason       TEXT        NOT NULL,

    funding_window_opens_at TIMESTAMPTZ,
    last_cycle_at           TIMESTAMPTZ,
    last_completed_at       TIMESTAMPTZ,
    next_due_at             TIMESTAMPTZ,

    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One decision per book per venue per instant. Re-running a cycle at an
    -- instant already refused must be a no-op rather than a second row: the
    -- guards are deterministic in their inputs, so the second row would be a
    -- duplicate of the first, and a duplicated refusal inflates any count taken
    -- over this table -- including the one an operator would use to ask whether
    -- the scheduler is firing daily.
    PRIMARY KEY (portfolio_id, venue, attempted_at),

    CONSTRAINT carry_refusal_names_its_venue  CHECK (venue  <> ''),
    CONSTRAINT carry_refusal_names_its_guard  CHECK (guard  <> ''),
    CONSTRAINT carry_refusal_states_its_reason CHECK (reason <> ''),

    -- No constraint relates `last_cycle_at` to `attempted_at`, and the omission
    -- is deliberate: `instant_already_covered` fires precisely when the log has
    -- moved past the instant the runner was asked to act at, so a last cycle
    -- *later* than the attempt is the exact state that guard exists to record.
    -- A check requiring the boundary to precede the attempt reads as obviously
    -- true and rejects one of the five refusals outright.

    -- The hold is measured from the last completed cycle, which is one of the
    -- cycles the log holds -- so it can never be more recent than the last one.
    CONSTRAINT carry_refusal_completed_is_not_after_last_cycle
        CHECK (last_completed_at IS NULL
               OR (last_cycle_at IS NOT NULL AND last_completed_at <= last_cycle_at)),

    -- The next rebalance is derived from the last completed cycle. A due date
    -- without one would be a countdown to a date nothing measured.
    CONSTRAINT carry_refusal_due_needs_a_completed_cycle
        CHECK (next_due_at IS NULL OR last_completed_at IS NOT NULL)
);

-- The read this table exists for: "what did this book most recently refuse, and
-- when" -- taken per book per venue, newest first.
CREATE INDEX carry_refusal_by_book
    ON carry_refusal (portfolio_id, venue, attempted_at DESC);

-- DOWN

DROP TABLE IF EXISTS carry_refusal;
