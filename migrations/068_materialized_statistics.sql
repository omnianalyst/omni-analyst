-- Materialize the two statistics views every scheduler loop reads.
--
-- Measured 2026-08-19 on production: calibration_bucket and finding_payoff
-- are aggregate views over the 2.0M-row prediction table, and the scheduler
-- reads them constantly (the conviction gate per surfaced candidate, the
-- trend producer's continuation ratio per predict pass). Every read was a
-- full-table aggregate; Postgres sat pinned at a constant 100% CPU with the
-- applications themselves under 0.5%. The prediction count beside them got
-- its index in 067; these views cannot be indexed -- they are recomputed
-- per read -- so they become materialized views, refreshed once per resolve
-- pass: the ONLY writer of outcome fields. Statistics between refreshes are
-- one resolve-interval stale, which is the honest granularity of a
-- calibration bucket anyway.
--
-- Unique indexes exist because REFRESH ... CONCURRENTLY requires them (the
-- refresh must diff old against new without blocking readers), and
-- NULLS NOT DISTINCT because audience_user_id is NULL for the shared
-- network's rows and plain UNIQUE treats NULLs as never-equal.
DROP VIEW IF EXISTS calibration_bucket;

CREATE MATERIALIZED VIEW calibration_bucket AS
SELECT
    audience_user_id,
    method,
    width_bucket(confidence, 0, 1, 10) AS bucket,
    (width_bucket(confidence, 0, 1, 10) - 1) / 10.0 AS bucket_low,
    (width_bucket(confidence, 0, 1, 10)) / 10.0 AS bucket_high,
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

CREATE UNIQUE INDEX calibration_bucket_key
    ON calibration_bucket (audience_user_id, method, bucket)
    NULLS NOT DISTINCT;

DROP VIEW IF EXISTS finding_payoff;

CREATE MATERIALIZED VIEW finding_payoff AS
SELECT
    f.method,
    f.audience_user_id,
    count(*) FILTER (WHERE p.outcome <> 'pending') AS resolved,
    avg(
        CASE WHEN p.direction = 'down'
             THEN (p.entry_price - p.lower_barrier)
             ELSE (p.entry_price - p.lower_barrier)
        END / p.entry_price * 100.0
    ) FILTER (
        WHERE p.outcome <> 'pending'
          AND p.entry_price > 0
          AND p.lower_barrier > 0
          AND p.lower_barrier < p.entry_price
    ) AS avg_risk_pct,
    avg(
        CASE WHEN p.direction = 'down'
             THEN (p.lower_barrier - p.entry_price)
             ELSE (p.upper_barrier - p.entry_price)
        END / p.entry_price * 100.0
    ) FILTER (
        WHERE p.outcome <> 'pending'
          AND p.entry_price > 0
          AND CASE WHEN p.direction = 'down'
                   THEN p.lower_barrier > p.entry_price
                   ELSE p.upper_barrier > p.entry_price
              END
    ) AS avg_payoff_pct,
    avg(
        CASE
            WHEN (p.direction = 'up'   AND p.outcome = 'upper')
              OR (p.direction = 'down' AND p.outcome = 'lower')
              OR (p.direction = 'neutral' AND p.outcome = 'expiry')
            THEN (
                CASE WHEN p.direction = 'down'
                     THEN (p.lower_barrier - p.entry_price)
                     ELSE (p.upper_barrier - p.entry_price)
                END
                / (p.entry_price - p.lower_barrier)
            )
            ELSE -1.0
        END
    ) FILTER (
        WHERE p.outcome <> 'pending'
          AND p.entry_price > 0
          AND p.lower_barrier > 0
          AND p.lower_barrier < p.entry_price
          AND CASE WHEN p.direction = 'down'
                   THEN p.lower_barrier > p.entry_price
                   ELSE p.upper_barrier > p.entry_price
              END
    ) AS avg_realized_ratio
FROM finding f
JOIN prediction p ON p.id = f.prediction_id
WHERE f.status = 'surfaced'
GROUP BY f.method, f.audience_user_id;

CREATE UNIQUE INDEX finding_payoff_key
    ON finding_payoff (method, audience_user_id)
    NULLS NOT DISTINCT;

-- DOWN
DROP MATERIALIZED VIEW IF EXISTS finding_payoff;
DROP MATERIALIZED VIEW IF EXISTS calibration_bucket;

CREATE VIEW calibration_bucket AS
SELECT
    audience_user_id,
    method,
    width_bucket(confidence, 0, 1, 10) AS bucket,
    (width_bucket(confidence, 0, 1, 10) - 1) / 10.0 AS bucket_low,
    (width_bucket(confidence, 0, 1, 10)) / 10.0 AS bucket_high,
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
