-- Findings: what the system chose to say unprompted, and whether it was right.
--
-- Storing the refusals alongside the surfaced ones is the point. A table of
-- only what was published cannot answer "how often does it stay quiet, and
-- why", and that is the question that makes the hit rate trustworthy.

CREATE TYPE finding_status AS ENUM ('surfaced', 'refused');

CREATE TABLE finding (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id      UUID NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    entity_id     UUID NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    audience_user_id UUID,

    status        finding_status NOT NULL,
    -- Populated only when refused; names which gate stopped it.
    refusal       TEXT,

    method        TEXT NOT NULL,
    confidence    DOUBLE PRECISION NOT NULL
                      CHECK (confidence >= 0 AND confidence <= 1),

    -- What the gate knew at the moment it decided. Recorded rather than
    -- recomputed, because calibration moves and a decision must remain
    -- explicable with the numbers that actually produced it.
    threshold           DOUBLE PRECISION,
    calibrated_hit_rate DOUBLE PRECISION,

    supporting    JSONB NOT NULL DEFAULT '[]',
    disconfirming JSONB NOT NULL DEFAULT '[]',

    -- Every surfaced finding writes a falsifiable prediction. That link is
    -- what lets the product publish its accuracy on the things it chose to
    -- surface, rather than on everything it ever computed.
    prediction_id UUID REFERENCES prediction(id) ON DELETE SET NULL,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT refusal_names_a_reason
        CHECK (status <> 'refused' OR refusal IS NOT NULL),

    CONSTRAINT surfaced_findings_are_falsifiable
        CHECK (status <> 'surfaced' OR prediction_id IS NOT NULL),

    CONSTRAINT surfaced_findings_record_their_threshold
        CHECK (status <> 'surfaced' OR threshold IS NOT NULL)
);

CREATE INDEX finding_feed
    ON finding (audience_user_id, created_at DESC) WHERE status = 'surfaced';

CREATE INDEX finding_by_method ON finding (method, status);

-- The product's own scorecard: accuracy on what it chose to say, not on
-- everything it computed. Only resolved predictions count.
CREATE VIEW finding_hit_rate AS
SELECT
    f.method,
    count(*) AS surfaced,
    count(p.id) FILTER (WHERE p.outcome <> 'pending') AS resolved,
    count(*) FILTER (
        WHERE (p.direction = 'up'      AND p.outcome = 'upper')
           OR (p.direction = 'down'    AND p.outcome = 'lower')
           OR (p.direction = 'neutral' AND p.outcome = 'expiry')
    ) AS hits
FROM finding f
JOIN prediction p ON p.id = f.prediction_id
WHERE f.status = 'surfaced'
GROUP BY f.method;

-- DOWN

DROP VIEW IF EXISTS finding_hit_rate;
DROP TABLE IF EXISTS finding;
DROP TYPE IF EXISTS finding_status;
