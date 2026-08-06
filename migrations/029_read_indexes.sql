-- Read-path indexes for the hot paths the audit flagged.
--
-- /system/status does MAX(created_at)/MAX(finished_at) over five tables every
-- poll (30s x users); without these each is a sequential scan. The partial /
-- entity-leading indexes serve /gaps/{id}, /autonomous/sectors + /regime, and
-- the predict_once pending dedup. All are pure cost reductions; no behaviour
-- change, no uniqueness (the structural prediction dedup was evaluated and
-- deferred -- it conflicts with the calibration test fixtures' multi-pending
-- model, and replicas:1 plus the read-then-write guard already hold it).

CREATE INDEX IF NOT EXISTS prediction_created_at
    ON prediction (created_at DESC);

CREATE INDEX IF NOT EXISTS finding_created_at
    ON finding (created_at DESC);

CREATE INDEX IF NOT EXISTS demand_created_at
    ON demand (created_at DESC);

CREATE INDEX IF NOT EXISTS fill_attempt_finished_at
    ON fill_attempt (finished_at DESC);

CREATE INDEX IF NOT EXISTS claim_observed_at
    ON claim (observed_at DESC);

-- /gaps/{id}: an entity-leading index over open gaps, so a one-entity view does
-- not walk the global ranked queue the fill workers lease from.
CREATE INDEX IF NOT EXISTS gap_open_by_entity
    ON gap (entity_id, score DESC, detected_at)
    WHERE resolved_at IS NULL;

-- /autonomous/sectors and /autonomous/regime filter by claim_type first; the
-- existing coverage index leads with entity_id and cannot serve them.
CREATE INDEX IF NOT EXISTS claim_by_type_fresh
    ON claim (claim_type, audience_user_id, knowledge_date DESC)
    WHERE superseded_by IS NULL;

-- predict_once's pending dedup check had no covering index.
CREATE INDEX IF NOT EXISTS prediction_pending_check
    ON prediction (entity_id, method, audience_user_id)
    WHERE outcome = 'pending';

-- DOWN

DROP INDEX IF EXISTS prediction_pending_check;
DROP INDEX IF EXISTS claim_by_type_fresh;
DROP INDEX IF EXISTS gap_open_by_entity;
DROP INDEX IF EXISTS claim_observed_at;
DROP INDEX IF EXISTS fill_attempt_finished_at;
DROP INDEX IF EXISTS demand_created_at;
DROP INDEX IF EXISTS finding_created_at;
DROP INDEX IF EXISTS prediction_created_at;
