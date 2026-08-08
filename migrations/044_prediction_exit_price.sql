-- The price a resolved prediction's position actually closed at.
--
-- GATE A measured 35.1% of resolved predictions expiring without touching a
-- barrier. For the two barrier outcomes the realised move is recoverable -- the
-- position closed at the barrier, and the barrier is already on the row. For
-- `expiry` nothing recorded the price at the horizon, so every expectancy
-- figure scored over a third of the sample as zero. That is not a conservative
-- assumption and not an optimistic one; it is an unknown quantity entered as a
-- number, and it drags every measurement toward zero without saying so.
--
-- Both columns are NULLable and are deliberately NOT back-filled. A prediction
-- resolved before this migration has no observed exit price. Substituting the
-- entry, the barrier, or a price fetched today would manufacture precisely the
-- figure these columns exist to stop assuming, and it would be indistinguishable
-- afterwards from a measured one. NULL means "not measured" -- a fact a reader
-- can act on. A guess is not.
--
-- The CHECK ties the pair: a price with no time is unattributable (a price
-- observed when?) and a time with no price measures nothing, so a
-- half-populated row must not exist. Written as an equality of NULL-ness rather
-- than two ORed conjunctions, matching 001's prediction_resolution_is_timestamped,
-- which ties outcome to resolved_at the same way.

ALTER TABLE prediction
    ADD COLUMN exit_price NUMERIC,
    ADD COLUMN exit_at    TIMESTAMPTZ;

ALTER TABLE prediction
    ADD CONSTRAINT prediction_exit_price_is_timestamped
        CHECK ((exit_price IS NULL) = (exit_at IS NULL));

-- DOWN

ALTER TABLE prediction
    DROP CONSTRAINT IF EXISTS prediction_exit_price_is_timestamped;

ALTER TABLE prediction DROP COLUMN IF EXISTS exit_at;
ALTER TABLE prediction DROP COLUMN IF EXISTS exit_price;
