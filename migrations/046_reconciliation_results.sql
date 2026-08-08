-- Reconciliation results: the evidence behind a verdict about capital.
--
-- Until this table existed, `portfolio/reconcile.py` built a
-- `ReconciliationResult`, handed it to one caller, and the caller dropped it.
-- Nothing outlived the cycle, so `/trading/reconciliation` could only ever
-- report `never_run` and `risk_alert`'s reconciliation kind had nothing to age.
-- A check whose answer is not kept is a check nobody can be shown to have run.
--
-- What is stored is the RESULT, never the verdict recomputed later. The row is
-- a reading taken at `checked_at` against a tolerance the operator chose at that
-- moment; re-deriving `reconciled` from the discrepancies at read time would let
-- a later change of mind about what counts as a divergence rewrite history.
--
-- The absence of a row is the load-bearing state. A venue with no row here has
-- not been checked, which is not the same fact as a venue that was checked and
-- agreed, and every reader of this table has to keep those apart -- that is the
-- whole reason `never_run` is in the read contract. Nothing in this schema
-- defaults, backfills or synthesises a row, because the moment one appears
-- without a check behind it the distinction is gone.
--
-- Money and quantities are NUMERIC. These columns hold the two sides of a
-- disagreement an operator will reconcile by hand, and a difference that arrives
-- back a fraction off is a difference they cannot close.

CREATE TABLE reconciliation_result (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id  UUID NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,
    venue         TEXT NOT NULL,
    reconciled    BOOLEAN NOT NULL,

    -- When the books were compared, supplied by the caller's clock, versus when
    -- the row was written. They are different facts: a result recorded after a
    -- retry is evidence about the moment it was taken, and ageing it against the
    -- write would call a stale reading fresh.
    checked_at    TIMESTAMPTZ NOT NULL,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A result scoped to no venue is evidence about nothing. Scope is one venue
    -- in `reconcile()` for the same reason: a pass at one venue says nothing
    -- about another.
    CONSTRAINT reconciliation_result_names_its_venue CHECK (venue <> '')
);

-- The read is "the most recent result per venue for this portfolio", which is
-- exactly this key.
CREATE INDEX reconciliation_result_latest_per_venue
    ON reconciliation_result (portfolio_id, venue, checked_at DESC);


-- One disagreement per row, both sides of it kept.
--
-- `local` and `remote` are NULLABLE and their NULL is a statement: that side of
-- the book had no row at all, which is not the same as a row reading zero. A
-- `position_missing_at_venue` stored with `remote = 0` would assert the venue
-- reported a flat position, which it did not -- it reported nothing. Defaulting
-- either column to 0 would erase the distinction the reconciler exists to draw.
--
-- The kind set is closed in the CHECK and not only in the application, because a
-- kind the reader cannot render is a divergence that reaches an operator as a
-- blank. It mirrors `reconcile.Divergence` exactly.
--
-- `seq` preserves the order the reconciler produced -- unknown identifiers, then
-- positions, then balances, each sorted. An operator reads the list in that
-- order and a set-valued table would reshuffle it per query plan.
CREATE TABLE reconciliation_discrepancy (
    result_id  UUID NOT NULL REFERENCES reconciliation_result(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    venue      TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    local      NUMERIC,
    remote     NUMERIC,
    detail     TEXT NOT NULL,

    PRIMARY KEY (result_id, seq),

    CONSTRAINT reconciliation_discrepancy_kind_is_known
        CHECK (kind IN ('position_quantity', 'position_missing_locally',
                        'position_missing_at_venue', 'cash_balance',
                        'cash_locked', 'unknown_symbol', 'venue_unavailable')),

    -- A divergence recorded without saying what it was cannot be acted on, and
    -- an empty string renders as a blank line rather than as a missing one.
    CONSTRAINT reconciliation_discrepancy_says_what_happened
        CHECK (detail <> '')
);

-- There is deliberately no constraint tying `reconciled` to the presence of
-- child rows: a CHECK cannot count them, and a denormalised counter is a second
-- copy of the truth that can drift from the rows it claims to describe. The
-- pairing is enforced where it can be enforced completely -- the writer inserts
-- the result and every discrepancy in ONE transaction, so a half-written result
-- is not a state the table can hold, and the reader rebuilds
-- `ReconciliationResult`, whose constructor refuses both incoherent shapes: a
-- reconciled result carrying divergences, and a divergent one naming none.

-- DOWN

DROP TABLE IF EXISTS reconciliation_discrepancy;
DROP TABLE IF EXISTS reconciliation_result;
