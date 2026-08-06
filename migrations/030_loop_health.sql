-- Loop health: the one signal the output-derived freshness in /system/status
-- cannot give. A scheduler loop that is *alive but failing every cycle* still
-- iterates (so the process looks up), but stops writing output rows, and the
-- only visible symptom is those rows aging -- pull-only, no verdict, no reason.
-- A loop that is *alive but idle* (sweep finds no gaps) writes nothing either,
-- so output-aging cannot tell idle from dead.
--
-- This table is the process view that disambiguates both. The scheduler writes
-- one state row per loop on each iteration: a success resets consecutive
-- failures and stamps last_success_at; a failure increments consecutive
-- failures, stamps last_failure_at and captures the error. A dead process
-- writes nothing here either -- and that is exactly what makes a stale row
-- meaningful, the same honesty property the output-derived view relies on.
--
-- This is deliberately NOT a heartbeat table. A heartbeat a dead loop also
-- stops writing is no more informative than reading the tables it writes. The
-- value here is the failure dimension (consecutive_failures + last_error) and
-- the alive-idle dimension (last_success_at moves even when output does not),
-- neither of which reading output tables can provide. expected_interval_seconds
-- is stored at record time so /system/status can grade staleness against each
-- loop's own cadence without needing the scheduler's config.

CREATE TABLE loop_health (
    loop_name                 TEXT PRIMARY KEY,
    last_success_at           TIMESTAMPTZ,
    last_failure_at           TIMESTAMPTZ,
    consecutive_failures      INTEGER NOT NULL DEFAULT 0,
    last_error                TEXT,
    expected_interval_seconds DOUBLE PRECISION,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- DOWN

DROP TABLE IF EXISTS loop_health;
