CREATE TYPE claim_type AS ENUM (
    'price_snapshot',
    'fundamental_metric',
    'filing_event',
    'macro_series_point',
    'news_event',
    'manipulation_signal'
);

CREATE TYPE redistribution AS ENUM ('allowed', 'byo_only', 'prohibited');

CREATE TYPE gap_class AS ENUM (
    'missing',
    'stale',
    'low_confidence',
    'unverified',
    'contradictory'
);

CREATE TYPE fill_outcome AS ENUM ('filled', 'unfillable', 'error');

CREATE TYPE prediction_direction AS ENUM ('up', 'down', 'neutral');

CREATE TYPE prediction_outcome AS ENUM ('pending', 'upper', 'lower', 'expiry');


CREATE TABLE entity (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind         TEXT NOT NULL,
    symbol       TEXT,
    name         TEXT NOT NULL,
    identifiers  JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, symbol)
);


-- The coverage store.
--
-- event_date is when the fact happened; knowledge_date is when it became
-- knowable. Both are required: a single as_of cannot answer "what did we know,
-- and when", which is the question a point-in-time backtest asks.
--
-- audience_user_id is access control, not metadata. NULL means the claim is
-- part of the shared network. Non-NULL means it was fetched with that user's
-- own credential under terms that forbid redistribution, so it is visible only
-- to them. The CHECK below makes the two states impossible to confuse.
CREATE TABLE claim (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id         UUID NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    claim_type        claim_type NOT NULL,
    key               TEXT NOT NULL,
    value             JSONB NOT NULL,
    unit              TEXT,
    evidence          JSONB,

    source            TEXT NOT NULL,
    event_date        TIMESTAMPTZ NOT NULL,
    knowledge_date    TIMESTAMPTZ NOT NULL,
    confidence        DOUBLE PRECISION NOT NULL
                          CHECK (confidence >= 0 AND confidence <= 1),

    credential_owner  TEXT,
    redistributable   redistribution NOT NULL,
    audience_user_id  UUID,

    observed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_by     UUID REFERENCES claim(id) ON DELETE SET NULL,

    CONSTRAINT claim_never_stores_prohibited_sources
        CHECK (redistributable <> 'prohibited'),

    CONSTRAINT claim_audience_matches_redistribution
        CHECK (
            (redistributable = 'allowed'  AND audience_user_id IS NULL)
         OR (redistributable = 'byo_only' AND audience_user_id IS NOT NULL)
        ),

    CONSTRAINT claim_knowable_at_or_after_event
        CHECK (knowledge_date >= event_date)
);

-- Ingestion idempotency. Two partial indexes because NULL audience_user_id
-- (shared coverage) would otherwise never collide with itself.
CREATE UNIQUE INDEX claim_identity_shared
    ON claim (entity_id, claim_type, key, source, event_date, knowledge_date)
    WHERE audience_user_id IS NULL;

CREATE UNIQUE INDEX claim_identity_private
    ON claim (entity_id, claim_type, key, source, event_date, knowledge_date,
              audience_user_id)
    WHERE audience_user_id IS NOT NULL;

CREATE INDEX claim_coverage_lookup
    ON claim (entity_id, claim_type, audience_user_id, knowledge_date DESC);


-- What has been asked for, by whom, how urgently.
CREATE TABLE demand (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    claim_type      claim_type NOT NULL,
    key             TEXT,
    channel         TEXT NOT NULL,
    requested_by    UUID,
    weight          DOUBLE PRECISION NOT NULL DEFAULT 1.0 CHECK (weight > 0),
    max_staleness   INTERVAL,
    min_confidence  DOUBLE PRECISION
                        CHECK (min_confidence IS NULL
                               OR (min_confidence >= 0 AND min_confidence <= 1)),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    active          BOOLEAN NOT NULL DEFAULT true
);

CREATE INDEX demand_active_lookup
    ON demand (entity_id, claim_type, active) WHERE active;


-- Demand minus coverage. Scoped to an audience for the same reason claims are.
CREATE TABLE gap (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id         UUID NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    claim_type        claim_type NOT NULL,
    key               TEXT,
    gap_class         gap_class NOT NULL,
    audience_user_id  UUID,
    score             DOUBLE PRECISION NOT NULL,
    detail            JSONB NOT NULL DEFAULT '{}',
    detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ,

    lease_owner       TEXT,
    lease_expires_at  TIMESTAMPTZ,

    CONSTRAINT gap_lease_is_all_or_nothing
        CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

-- One open gap per (entity, type, key, class, audience).
CREATE UNIQUE INDEX gap_open_identity
    ON gap (entity_id, claim_type, COALESCE(key, ''), gap_class,
            COALESCE(audience_user_id, '00000000-0000-0000-0000-000000000000'::uuid))
    WHERE resolved_at IS NULL;

CREATE INDEX gap_ranked_queue
    ON gap (score DESC, detected_at)
    WHERE resolved_at IS NULL;


-- Every attempt to close a gap, including the ones that could not be closed.
-- An agent that cannot fill a gap must say why; the CHECK enforces it, because
-- gap-filling that always produces something is how fabricated coverage enters
-- the store.
CREATE TABLE fill_attempt (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gap_id       UUID NOT NULL REFERENCES gap(id) ON DELETE CASCADE,
    capability   TEXT NOT NULL,
    outcome      fill_outcome NOT NULL,
    claim_id     UUID REFERENCES claim(id) ON DELETE SET NULL,
    reason       TEXT,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,

    CONSTRAINT filled_attempt_produces_a_claim
        CHECK (outcome <> 'filled' OR claim_id IS NOT NULL),

    CONSTRAINT unfillable_attempt_states_why
        CHECK (outcome <> 'unfillable' OR reason IS NOT NULL),

    CONSTRAINT errored_attempt_states_why
        CHECK (outcome <> 'error' OR reason IS NOT NULL)
);

CREATE INDEX fill_attempt_by_gap ON fill_attempt (gap_id, started_at DESC);


-- The falsifiable subset of the store.
--
-- Barriers and entry price are NOT NULL because falsifiability requires the
-- threshold to exist before the outcome. The writer never sets outcome fields;
-- scoring is a separate pass.
CREATE TABLE prediction (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id        UUID NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    claim_id         UUID REFERENCES claim(id) ON DELETE SET NULL,
    method           TEXT NOT NULL,
    direction        prediction_direction NOT NULL,
    confidence       DOUBLE PRECISION NOT NULL
                         CHECK (confidence >= 0 AND confidence <= 1),

    entry_price      NUMERIC NOT NULL,
    upper_barrier    NUMERIC NOT NULL,
    lower_barrier    NUMERIC NOT NULL,
    horizon_ends_at  TIMESTAMPTZ NOT NULL,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome          prediction_outcome NOT NULL DEFAULT 'pending',
    resolved_at      TIMESTAMPTZ,
    provenance       JSONB NOT NULL,

    CONSTRAINT prediction_barriers_straddle_entry
        CHECK (upper_barrier > entry_price AND lower_barrier < entry_price),

    CONSTRAINT prediction_resolution_is_timestamped
        CHECK ((outcome = 'pending') = (resolved_at IS NULL))
);

CREATE INDEX prediction_due
    ON prediction (horizon_ends_at) WHERE outcome = 'pending';

CREATE INDEX prediction_by_method ON prediction (method, outcome);


-- Calibration is derived, never stored, so it cannot drift from the ledger.
-- Small-sample suppression is deliberately NOT applied here: the threshold
-- belongs in exactly one place, and that place is the Python caller.
CREATE VIEW calibration_bucket AS
SELECT
    method,
    width_bucket(confidence, 0, 1, 10) AS bucket,
    (width_bucket(confidence, 0, 1, 10) - 1) / 10.0 AS bucket_low,
    width_bucket(confidence, 0, 1, 10) / 10.0 AS bucket_high,
    count(*) AS n,
    count(*) FILTER (
        WHERE (direction = 'up'      AND outcome = 'upper')
           OR (direction = 'down'    AND outcome = 'lower')
           OR (direction = 'neutral' AND outcome = 'expiry')
    ) AS hits,
    avg(confidence) AS mean_confidence
FROM prediction
WHERE outcome <> 'pending'
GROUP BY method, width_bucket(confidence, 0, 1, 10);

-- DOWN

DROP VIEW IF EXISTS calibration_bucket;
DROP TABLE IF EXISTS prediction;
DROP TABLE IF EXISTS fill_attempt;
DROP TABLE IF EXISTS gap;
DROP TABLE IF EXISTS demand;
DROP TABLE IF EXISTS claim;
DROP TABLE IF EXISTS entity;
DROP TYPE IF EXISTS prediction_outcome;
DROP TYPE IF EXISTS prediction_direction;
DROP TYPE IF EXISTS fill_outcome;
DROP TYPE IF EXISTS gap_class;
DROP TYPE IF EXISTS redistribution;
DROP TYPE IF EXISTS claim_type;
