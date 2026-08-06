-- Scope finding_hit_rate by audience so the scorecard honors redistribution.
--
-- Previously the view grouped every operator's surfaced findings together, so
-- /briefing/scorecard published a hit rate that was a deterministic function
-- of every signed-in operator's byo-derived demand -- the same shape of leak
-- migration 019 closed for calibration_bucket. The view now carries
-- audience_user_id through the GROUP BY; the scorecard query re-aggregates the
-- shared (NULL) row plus the caller's own row per method, matching the rule a
-- user sees everywhere else: the shared network plus their own private claims.

DROP VIEW IF EXISTS finding_hit_rate;

CREATE VIEW finding_hit_rate AS
SELECT
    f.method,
    f.audience_user_id,
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
GROUP BY f.method, f.audience_user_id;

-- DOWN

DROP VIEW IF EXISTS finding_hit_rate;

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
