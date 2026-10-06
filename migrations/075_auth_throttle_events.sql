-- Login throttling events (audit finding 1).
--
-- The in-memory per-IP limiter is per-process: with more than one API replica
-- the effective attempt limit multiplies, and a restart forgets an attack in
-- progress. This table is the shared, durable half of the throttle, keyed by
-- (email fingerprint, client IP) so a brute force against one account is
-- throttled as one stream regardless of which replica serves each attempt.
--
-- The email is stored as a sha256 fingerprint, not plaintext: a throttle log
-- is operational telemetry, and it should not become a plain list of the
-- deployment's email addresses. Only failed attempts are recorded; a
-- successful login deletes the key's rows, so an honest operator never
-- accumulates state.

CREATE TABLE auth_throttle_event (
    email_hash TEXT        NOT NULL,
    client_ip  TEXT        NOT NULL,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The decision reads failures since the key's last event within the lookback
-- window; this index serves it directly.
CREATE INDEX auth_throttle_event_key_at
    ON auth_throttle_event (email_hash, client_ip, at DESC);

-- DOWN
-- DROP INDEX auth_throttle_event_key_at;
-- DROP TABLE auth_throttle_event;
