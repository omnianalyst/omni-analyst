-- Alert delivery: one-shot alerts and firing acknowledgement.

-- one_shot: the alert deactivates itself after its next firing. Retail
-- standard ("alert me once when X") and the quiet default for conditions
-- expected to fire rarely. Deactivation is done by evaluate at fire time, so
-- a one-shot that never fires stays armed like any other.

-- acknowledged_at on alert_firing: the unread state. A firing is an event the
-- user has not necessarily seen; without an ack column the UI can only show
-- "last fired", and there is no way to express "I have read this". NULL means
-- unread. The partial index serves the inbox query (unread across alerts).

ALTER TABLE alert
    ADD COLUMN one_shot BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE alert_firing
    ADD COLUMN acknowledged_at TIMESTAMPTZ;

CREATE INDEX alert_firing_unread
    ON alert_firing (fired_at DESC)
    WHERE acknowledged_at IS NULL;

-- DOWN
-- DROP INDEX alert_firing_unread;
-- ALTER TABLE alert_firing DROP COLUMN acknowledged_at;
-- ALTER TABLE alert DROP COLUMN one_shot;
