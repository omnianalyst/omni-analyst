-- Alerts as conditions over coverage.
--
-- An alert is not a price threshold. It is a condition evaluated against the
-- claims the owner may actually see (visible_claims), scoped to one
-- (entity, claim_type). When it fires it does two things: it records a firing
-- (the durable notification, surfaced by GET /alerts/{id}/firings) and it
-- raises the owner's demand for that coverage -- a user who cares enough to set
-- an alert on something is asking for it to stay covered.
--
-- The condition itself is a JSONB document drawn from a closed set
-- (see omni.alerts.rules): value_above, value_below, staleness_exceeds,
-- contradiction. The set is validated at creation, not at read, so evaluate
-- never has to defend against an arbitrary shape -- and the application never
-- accepts a user-supplied expression, because a rule engine that ran caller
-- code would be a hole, not a feature.
--
-- alert_firing exists so an alert cannot fire twice on the same claim. Without
-- it, every re-evaluation would re-notify for a condition that is still true,
-- which is how an alerting system trains people to ignore it. The primary key
-- on (alert_id, claim_id) is the dedup: a claim already recorded as a firing is
-- skipped by evaluate, so a persistent condition produces one notification, not
-- one per poll.

CREATE TABLE alert (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    entity_id     UUID NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    claim_type    claim_type NOT NULL,
    condition     JSONB NOT NULL,
    active        BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_fired_at TIMESTAMPTZ
);

CREATE INDEX alert_by_user ON alert (user_id, active);
CREATE INDEX alert_by_scope ON alert (entity_id, claim_type, active);

CREATE TABLE alert_firing (
    alert_id  UUID NOT NULL REFERENCES alert(id) ON DELETE CASCADE,
    claim_id  UUID NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    fired_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (alert_id, claim_id)
);

CREATE INDEX alert_firing_by_claim ON alert_firing (claim_id);

-- DOWN

DROP TABLE IF EXISTS alert_firing;
DROP TABLE IF EXISTS alert;
