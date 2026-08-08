-- Funding accrual: the cash a perpetual position pays or receives at a settlement.
--
-- A perpetual has no expiry, so it is tethered to spot by a payment exchanged
-- between longs and shorts every 8 hours. Until this table existed the portfolio
-- had no concept of it at all: a delta-neutral carry book -- long spot, short
-- perp -- showed a NAV that never moved, because the only thing it earns is the
-- funding neither leg records. Carry P&L could not exist, so the one strategy
-- this system has measured an edge in could not be graded.
--
-- **The primary key IS the idempotency guard, and it is derived entirely from
-- the settlement's own identity.** One settlement is one (portfolio, venue,
-- symbol, funding_time); re-running a cycle, replaying a day, or two schedulers
-- overlapping all present the same key and the second write is refused by the
-- database rather than by a caller remembering to check. Nothing in the key
-- comes from the clock of the process doing the applying -- a dedup key built
-- from `now()` is exactly how a backfill here once silently ran itself twice and
-- produced 3,622 rows where 1,811 were owed.
--
-- `market_type` is deliberately absent from the key. Funding exists only for
-- perpetuals, so a portfolio holds at most one fundable leg per (venue, symbol);
-- carrying the column would imply a spot funding row is a state this table can
-- hold, and it is not one.
--
-- **NULL means no position was held, and that is not the same fact as zero.** A
-- settlement that arrives while the book holds nothing accrues nothing. Storing
-- that as `amount = 0` would assert a perpetual leg existed and happened to earn
-- nothing, which is a claim about a position that did not exist -- and it would
-- be indistinguishable from a real leg settling at a zero rate, which does
-- happen. So `quantity`, `mark` and `amount` are NULL together, the row still
-- exists to record that the settlement was processed, and a reader can tell the
-- two apart.
--
-- Every money column is NUMERIC. A funding accrual is small relative to the
-- notional that produced it and is applied three times a day for the life of the
-- position; a binary error per settlement accumulates directly into the P&L of
-- the strategy this table exists to measure.

CREATE TABLE funding_accrual (
    portfolio_id  UUID NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,
    venue         TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    funding_time  TIMESTAMPTZ NOT NULL,

    -- The rate for this settlement, signed. Positive means longs pay shorts.
    funding_rate  NUMERIC NOT NULL,

    -- The signed perpetual quantity the settlement was applied to (negative is
    -- short) and the mark it was valued at. Stored rather than re-derivable:
    -- the position moves with the next fill and the mark is a reading taken at
    -- the settlement, so an audit that recomputed either from today's book would
    -- be checking a different settlement.
    quantity      NUMERIC,
    mark          NUMERIC,

    -- Cash flow to the portfolio: negative is paid away, positive is received.
    amount        NUMERIC,

    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (portfolio_id, venue, symbol, funding_time),

    -- The three columns describe one position and are absent together or present
    -- together. A row carrying an amount but no quantity is an accrual nothing
    -- can be checked against.
    CONSTRAINT funding_accrual_position_is_whole_or_absent
        CHECK ((quantity IS NULL) = (amount IS NULL)
               AND (quantity IS NULL) = (mark IS NULL)),

    -- A flat position is deleted rather than stored (see `position`), so a zero
    -- quantity here is a row claiming a settlement landed on a position that was
    -- not held. That case is the NULL group above.
    CONSTRAINT funding_accrual_settles_a_real_position
        CHECK (quantity IS NULL OR quantity <> 0),

    CONSTRAINT funding_accrual_mark_is_a_price
        CHECK (mark IS NULL OR mark > 0),

    -- The sign of the accrual, enforced where a bug cannot talk its way past it.
    -- A positive rate means longs pay shorts, so a long (quantity > 0) is debited
    -- and a short (quantity < 0) is credited: amount = -quantity * mark * rate,
    -- and mark > 0. Inverting that sign turns the strategy into its exact
    -- opposite while every total still looks like a plausible P&L, which is the
    -- one failure here that would not announce itself.
    --
    -- Only the sign is checked, not the product: Python's Decimal context rounds
    -- at 28 significant digits and NUMERIC does not, so an exact-equality CHECK
    -- would reject legitimate rows whose inputs carry enough decimal places
    -- between them. Rounding cannot change a sign.
    CONSTRAINT funding_accrual_sign_follows_the_convention
        CHECK (amount IS NULL
               OR sign(amount) = - sign(quantity) * sign(funding_rate)),

    -- NUMERIC accepts 'NaN' and sorts it above every number, so `mark > 0` above
    -- admits one and the sign check compares NaN against NaN and passes.
    CONSTRAINT funding_accrual_amounts_are_numbers
        CHECK (funding_rate <> 'NaN'::numeric
               AND (quantity IS NULL
                    OR (quantity <> 'NaN'::numeric
                        AND mark <> 'NaN'::numeric
                        AND amount <> 'NaN'::numeric))),

    CONSTRAINT funding_accrual_names_its_market
        CHECK (venue <> '' AND symbol <> '')
);

-- The read is "what did this book accrue over this window", and the carry the
-- strategy is graded on is that sum per symbol.
CREATE INDEX funding_accrual_by_portfolio_time
    ON funding_accrual (portfolio_id, funding_time DESC);

-- DOWN

DROP TABLE IF EXISTS funding_accrual;
