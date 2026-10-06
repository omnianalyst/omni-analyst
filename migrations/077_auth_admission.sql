-- Atomic admission budgets for credential endpoints (audit finding A01).
--
-- 075 made throttle state durable, but admission stayed check-then-record:
-- concurrent logins could all observe the same admitted state before any of
-- them recorded its outcome, and the durable key (email fingerprint, IP)
-- reset when either side rotated. This table is a fixed-window request
-- budget reserved atomically in a single upsert, independent of the
-- attempt's outcome: a successful login does not refund the budget, so a
-- distributed guesser cannot launder one address's allowance through many
-- account names, and rotating IPs cannot reset an account's allowance.
--
-- Rows are per (key, window) counters with an expiry; admission maintenance
-- (not the request path) deletes expired ones in bounded batches.

CREATE TABLE auth_budget (
    key        TEXT        NOT NULL PRIMARY KEY,
    used       INTEGER     NOT NULL CHECK (used > 0),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX auth_budget_expiry ON auth_budget (expires_at);

-- Age-leading index so the maintenance job's bounded deletes (and the
-- lookback scan it replaces on the login path) never seq-scan the table.
CREATE INDEX auth_throttle_event_at ON auth_throttle_event (at);

-- DOWN
-- DROP INDEX auth_throttle_event_at;
-- DROP INDEX auth_budget_expiry;
-- DROP TABLE auth_budget;
