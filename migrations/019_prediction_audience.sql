-- Phase 1.1: close the calibration licence leak.
--
-- calibration_bucket aggregated by (method, confidence bucket) with NO audience
-- dimension (001). publish.load_calibration read that global aggregate; gate.assess
-- used it to set the conviction threshold that decides what EVERY audience sees.
-- An outcome decided by a byo_only price series is a deterministic function of
-- audience-private data, so feeding it into a shared aggregate would redistribute
-- private signal through the calibration channel. Dormant only because the
-- resolver resolved against the shared network alone (audience=None).
--
-- Fix: carry the audience through the prediction itself, and partition
-- calibration by it. This mirrors claim.audience_user_id exactly -- NULL means
-- the prediction resolves on the shared (allowed) network and feeds the shared
-- calibration every audience may read; non-NULL means it resolves on that
-- audience's visible prices (which may include their byo series) and feeds that
-- audience's calibration alone. The licence class is implied by NULL-ness,
-- precisely as 001's claim_audience_matches_redistribution CHECK makes it
-- isomorphic with redistributable on claims; a separate licence_class column
-- would be redundant and re-introduce the drift that CHECK exists to prevent.
--
-- A bare UUID (no FK), matching claim.audience_user_id. Resolution is
-- self-scoped: _resolve_one reads this column back and scopes the price path to
-- it, so a prediction's outcome is always decided by the same audience it then
-- calibrates.

ALTER TABLE prediction ADD COLUMN audience_user_id UUID;

-- Replaces prediction_by_method (method, outcome): the partitioned view GROUPs
-- BY (audience_user_id, method, bucket), so this index serves it. The old index
-- only served the un-partitioned view and no other query touches method alone.
DROP INDEX IF EXISTS prediction_by_method;

CREATE INDEX prediction_by_method_audience
    ON prediction (method, audience_user_id, outcome);

DROP VIEW calibration_bucket;
CREATE VIEW calibration_bucket AS
SELECT
    audience_user_id,
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
GROUP BY audience_user_id, method, width_bucket(confidence, 0, 1, 10);

-- DOWN

DROP VIEW IF EXISTS calibration_bucket;
DROP INDEX IF EXISTS prediction_by_method_audience;
ALTER TABLE prediction DROP COLUMN IF EXISTS audience_user_id;

-- Restore the un-partitioned view and the original method/outcome index exactly
-- as 001 defined them, so a clean rollback re-establishes the prior shape.
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

CREATE INDEX prediction_by_method ON prediction (method, outcome);
