-- Portfolio state: what is held, in what currency, and what it was last worth.
--
-- These rows are a MATERIALISATION of the fill history, not a second source of
-- truth. `portfolio/state.py` can rebuild every position row by replaying fills
-- and the reconciler compares the two; a divergence is a bug in the writer, not
-- a fact about the account. The schema is shaped to keep that replay honest.
--
-- Every money column is NUMERIC. DOUBLE PRECISION accumulates a binary error on
-- each fill applied to a position, and the error lands in the P&L of the
-- position held longest -- the one whose P&L matters most.
--
-- position.quantity is signed (negative is short), matching venue.protocol's
-- Position. A flat position is DELETED rather than stored as a zero row, and
-- the CHECK enforces it: a zero-quantity row still carries an average_entry,
-- which reads as an open exposure to anything scanning the table and would be
-- marked, exposed and risk-checked as one.

CREATE TABLE portfolio (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID REFERENCES users(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    base_currency  TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX portfolio_by_user ON portfolio (user_id) WHERE user_id IS NOT NULL;


CREATE TABLE position (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id   UUID NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,
    venue          TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    market_type    TEXT NOT NULL,
    quantity       NUMERIC NOT NULL,
    average_entry  NUMERIC NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The same symbol at the same venue as spot and as a perpetual is two
    -- exposures with different funding and different liquidation behaviour.
    -- Collapsing them would net a hedge into nothing.
    UNIQUE (portfolio_id, venue, symbol, market_type),

    CONSTRAINT position_market_type_is_known
        CHECK (market_type IN ('spot', 'margin', 'perpetual')),

    CONSTRAINT position_flat_row_is_deleted
        CHECK (quantity <> 0),

    CONSTRAINT position_open_has_a_real_entry
        CHECK (average_entry > 0)
);


-- free may be negative: a margin buy spends cash the account borrowed, and
-- refusing to record that would make the overdraw invisible rather than absent.
-- Refusing the trade is the risk engine's job, and it cannot do it from a row
-- that has been clamped. locked may not be negative -- it is a reservation
-- against resting orders, and a negative reservation is not a state.
CREATE TABLE cash_balance (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id  UUID NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,
    venue         TEXT NOT NULL,
    asset         TEXT NOT NULL,
    free          NUMERIC NOT NULL DEFAULT 0,
    locked        NUMERIC NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (portfolio_id, venue, asset),

    CONSTRAINT cash_locked_is_not_negative CHECK (locked >= 0)
);


-- A NAV reading is only meaningful with the marks it was taken against, so it
-- is a timestamped observation rather than a column on portfolio. cash is
-- stored alongside nav because nav - cash is the marked value of the book, and
-- deriving it later from today's positions would answer a different question.
CREATE TABLE nav_snapshot (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id    UUID NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,
    nav             NUMERIC NOT NULL,
    cash            NUMERIC NOT NULL,
    gross_exposure  NUMERIC NOT NULL,
    net_exposure    NUMERIC NOT NULL,
    taken_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT nav_gross_exposure_is_not_negative CHECK (gross_exposure >= 0)
);

CREATE INDEX nav_snapshot_by_portfolio
    ON nav_snapshot (portfolio_id, taken_at DESC);

-- DOWN

DROP TABLE IF EXISTS nav_snapshot;
DROP TABLE IF EXISTS cash_balance;
DROP TABLE IF EXISTS position;
DROP TABLE IF EXISTS portfolio;
