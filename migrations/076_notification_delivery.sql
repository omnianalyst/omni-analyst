-- Notification delivery queue (audit finding 9).
--
-- Delivery used to be fire-and-forget: one attempt per channel, failures
-- logged and dropped. A transient five-second outage permanently lost the
-- notification. For an alerting product the firing record is not enough --
-- the person asked to be told.
--
-- One row per (firing batch, channel), enqueued by dispatch at fire time and
-- worked by the scheduler's delivery loop. Attempts retry with bounded
-- exponential backoff until max attempts, then the row is marked failed -- it
-- stays visible rather than vanishing. Rows older than the delivery TTL are
-- expired to failed without further attempts: a retry dump hours after the
-- event is noise, not alerting.
--
-- The payload column carries everything a retry needs (full message content,
-- destination) so a later attempt does not depend on the alert rows still
-- being in the shape they were at fire time. The destination is member input
-- and can embed secrets (Discord tokens); it must never be returned to any
-- caller -- status endpoints expose status/attempts only.

CREATE TABLE notification_delivery (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID        NOT NULL,
    alert_id         UUID        NOT NULL,
    channel          TEXT        NOT NULL,
    payload          JSONB       NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'pending',
    attempts         INTEGER     NOT NULL DEFAULT 0,
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at     TIMESTAMPTZ
);

-- The worker's claim: due pending rows, oldest first.
CREATE INDEX notification_delivery_due
    ON notification_delivery (status, next_attempt_at)
    WHERE status = 'pending';

-- DOWN
-- DROP INDEX notification_delivery_due;
-- DROP TABLE notification_delivery;
