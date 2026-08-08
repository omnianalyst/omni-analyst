-- Risk alerts: conditions over portfolio state, where silence has to mean checked.
--
-- The coverage alert in 009 fires on a claim. This one fires on capital, and the
-- two cannot share a table for the same reason `omni.alerts` and
-- `omni.portfolio` cannot share a module: a coverage alert is evaluated on the
-- analysis side against claims the owner may see, and dragging a NAV into that
-- path would invert the one-way rule `tests/test_trading_isolation.py` enforces.
--
-- The condition set is closed and lives in the CHECK, not in the application
-- alone. A kind the code cannot evaluate must not be storable, because a row the
-- evaluator silently skips is a safety check that was configured, looks
-- configured, and is not running -- which is worse than no alert at all, since
-- the operator believes they have one.
--
-- threshold is NUMERIC, never DOUBLE PRECISION. It is compared against a share
-- derived from money, and a threshold that arrives back from the database a
-- fraction under what was stored moves the point at which a limit binds.
--
-- The bounds are the safety check on the safety check. `threshold > 0` refuses a
-- fat-fingered zero, which every comparison written the obvious way reads as
-- "alert on everything" or "alert on nothing" depending on the operator; and
-- `threshold <= 1` refuses a `25` typed for 25%, which would produce an alert
-- that can never fire and can never be noticed not firing. Both mirror
-- `RiskLimits.__post_init__`, which refuses the same two shapes for the same
-- reason.
--
-- A reconciliation alert must name its venue. Deriving the venue set from the
-- positions on the book instead would mean a portfolio holding nothing has no
-- venue to check and therefore reports no problem -- an empty book reading as a
-- clean one, which is precisely the fail-open this table exists to make
-- impossible.

CREATE TABLE risk_alert (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id   UUID NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    threshold      NUMERIC NOT NULL,
    venue          TEXT,
    stale_after    INTERVAL,
    active         BOOLEAN NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_fired_at  TIMESTAMPTZ,

    CONSTRAINT risk_alert_kind_is_known
        CHECK (kind IN ('drawdown_from_peak', 'gross_exposure',
                        'position_concentration', 'reconciliation')),

    -- NUMERIC 'NaN' sorts above every number, so `> 0` admits it; `<= 1` is the
    -- half of the band that turns it away.
    CONSTRAINT risk_alert_threshold_is_a_live_share
        CHECK (threshold > 0 AND threshold <= 1),

    CONSTRAINT risk_alert_reconciliation_names_its_venue
        CHECK ((kind = 'reconciliation') = (venue IS NOT NULL)),

    CONSTRAINT risk_alert_reconciliation_bounds_its_staleness
        CHECK ((kind = 'reconciliation') = (stale_after IS NOT NULL)),

    CONSTRAINT risk_alert_staleness_is_positive
        CHECK (stale_after IS NULL OR stale_after > interval '0')
);

CREATE INDEX risk_alert_by_portfolio ON risk_alert (portfolio_id, active);


-- A firing is an EPISODE, not an event.
--
-- 009's (alert_id, claim_id) key works because a claim is a new object each
-- time; a risk condition is a state that persists, so the same key shape would
-- either re-notify on every poll or fire once in the portfolio's lifetime and
-- never again. Neither is an alert. So a row is opened when the condition starts
-- holding and closed when it stops, and the partial unique index is the dedup:
-- at most one open episode per (alert, subject), enforced by the database rather
-- than by the evaluator remembering to look.
--
-- `reason` is on the row and in the key because the answer can change without
-- the condition clearing -- a venue reconciliation going from never-run to
-- diverged is a different fact about the same venue, and an episode that kept
-- the first label would tell the operator the check has still never run.
--
-- subject is what the condition is about: the portfolio for a book-wide
-- threshold, `venue|symbol|market_type` for a concentration, the venue name for
-- a reconciliation. It is what makes the dedup per-position rather than
-- per-alert, so a second position breaching is a second notification.
--
-- observed and threshold are stored with the firing rather than read back from
-- risk_alert, because the threshold may be edited afterwards and a firing whose
-- numbers move with it is not evidence of anything.
CREATE TABLE risk_alert_firing (
    alert_id    UUID NOT NULL REFERENCES risk_alert(id) ON DELETE CASCADE,
    subject     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    opened_at   TIMESTAMPTZ NOT NULL,
    observed    NUMERIC,
    threshold   NUMERIC,
    detail      TEXT NOT NULL,
    cleared_at  TIMESTAMPTZ,

    PRIMARY KEY (alert_id, subject, reason, opened_at),

    CONSTRAINT risk_alert_firing_reason_is_known
        CHECK (reason IN ('max_drawdown_hit', 'gross_exposure_exceeded',
                          'position_too_large', 'reconciliation_divergence',
                          'reconciliation_unknown', 'stale_data',
                          'peak_nav_unknown', 'no_state_available')),

    -- A breach recorded without the number that caused it cannot be audited
    -- afterwards. The unknown reasons carry no number by definition -- that is
    -- what makes them unknown -- so they are exempt rather than filled in.
    CONSTRAINT risk_alert_firing_breach_carries_its_numbers
        CHECK (reason NOT IN ('max_drawdown_hit', 'gross_exposure_exceeded',
                              'position_too_large')
               OR (observed IS NOT NULL AND threshold IS NOT NULL)),

    CONSTRAINT risk_alert_firing_clears_after_it_opened
        CHECK (cleared_at IS NULL OR cleared_at >= opened_at),

    CONSTRAINT risk_alert_firing_subject_is_named
        CHECK (subject <> '')
);

CREATE UNIQUE INDEX risk_alert_firing_one_open_episode
    ON risk_alert_firing (alert_id, subject)
    WHERE cleared_at IS NULL;

-- DOWN

DROP TABLE IF EXISTS risk_alert_firing;
DROP TABLE IF EXISTS risk_alert;
