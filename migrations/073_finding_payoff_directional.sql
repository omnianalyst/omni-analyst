-- Directional payoff accounting for finding_payoff (audit B05).
--
-- The 068/063 definition had four defects: short risk measured entry-lower
-- (a short's risk is upper-entry); the down-branch payoff filter demanded
-- lower>entry, which the schema's straddle constraint makes impossible, so
-- every short was silently excluded instead of scored; every non-hit scored
-- -1R including profitable expiries (exit_price, recorded since 044, went
-- unread); and a neutral expiry received a directional payoff.
--
-- This replacement separates directional payoff from classification accuracy
-- (calibration_bucket keeps scoring neutral expiries as hits -- that is its
-- job) and measures what the call actually realized:
--
-- - risk/reward distances are keyed on direction: a long risks entry-lower
--   and targets upper-entry; a short risks upper-entry and targets
--   entry-lower;
-- - measured_exit is the barrier the outcome named, or exit_price when the
--   call expired -- expiry rows without a recorded exit_price have no
--   realized ratio and stay out of the realized average rather than being
--   priced by invention;
-- - neutral calls carry no directional risk leg and contribute nothing to
--   payoff geometry;
-- - geometry_n / realized_n are emitted separately because the scorecard
--   (B06) must weight audience-level averages by the observations behind
--   each metric, not average the per-audience means.

DROP MATERIALIZED VIEW IF EXISTS finding_payoff;

CREATE MATERIALIZED VIEW finding_payoff AS
WITH directional AS (
    SELECT f.method, f.audience_user_id, p.entry_price,
           CASE p.direction WHEN 'up'
                THEN p.entry_price - p.lower_barrier
                ELSE p.upper_barrier - p.entry_price END AS risk_distance,
           CASE p.direction WHEN 'up'
                THEN p.upper_barrier - p.entry_price
                ELSE p.entry_price - p.lower_barrier END AS reward_distance,
           CASE p.direction WHEN 'up' THEN 1 ELSE -1 END AS side_sign,
           CASE p.outcome
                WHEN 'upper' THEN p.upper_barrier
                WHEN 'lower' THEN p.lower_barrier
                WHEN 'expiry' THEN p.exit_price END AS measured_exit
    FROM finding f
    JOIN prediction p ON p.id = f.prediction_id
    WHERE f.status = 'surfaced' AND p.outcome <> 'pending'
      AND p.direction IN ('up', 'down')
      AND p.entry_price > 0 AND p.lower_barrier > 0
      AND p.lower_barrier < p.entry_price
      AND p.upper_barrier > p.entry_price
), per_call AS (
    SELECT *, 100.0 * risk_distance / entry_price AS risk_pct,
           100.0 * reward_distance / entry_price AS payoff_pct,
           CASE WHEN measured_exit IS NOT NULL
                THEN side_sign * (measured_exit - entry_price) / risk_distance
           END AS realized_ratio
    FROM directional
)
SELECT method, audience_user_id,
       count(*) AS resolved, count(*) AS geometry_n,
       count(realized_ratio) AS realized_n,
       avg(risk_pct) AS avg_risk_pct,
       avg(payoff_pct) AS avg_payoff_pct,
       avg(realized_ratio) AS avg_realized_ratio
FROM per_call
GROUP BY method, audience_user_id;

CREATE UNIQUE INDEX finding_payoff_key
    ON finding_payoff (method, audience_user_id) NULLS NOT DISTINCT;

-- DOWN
-- DROP MATERIALIZED VIEW IF EXISTS finding_payoff;
-- (the 068 definition is restored verbatim below)
-- CREATE MATERIALIZED VIEW finding_payoff AS
-- SELECT
--     f.method,
--     f.audience_user_id,
--     count(*) FILTER (WHERE p.outcome <> 'pending') AS resolved,
--     avg(
--         CASE WHEN p.direction = 'down'
--              THEN (p.entry_price - p.lower_barrier)
--              ELSE (p.entry_price - p.lower_barrier)
--         END / p.entry_price * 100.0
--     ) FILTER (
--         WHERE p.outcome <> 'pending'
--           AND p.entry_price > 0
--           AND p.lower_barrier > 0
--           AND p.lower_barrier < p.entry_price
--     ) AS avg_risk_pct,
--     avg(
--         CASE WHEN p.direction = 'down'
--              THEN (p.lower_barrier - p.entry_price)
--              ELSE (p.upper_barrier - p.entry_price)
--         END / p.entry_price * 100.0
--     ) FILTER (
--         WHERE p.outcome <> 'pending'
--           AND p.entry_price > 0
--           AND CASE WHEN p.direction = 'down'
--                    THEN p.lower_barrier > p.entry_price
--                    ELSE p.upper_barrier > p.entry_price
--               END
--     ) AS avg_payoff_pct,
--     avg(
--         CASE
--             WHEN (p.direction = 'up'   AND p.outcome = 'upper')
--               OR (p.direction = 'down' AND p.outcome = 'lower')
--               OR (p.direction = 'neutral' AND p.outcome = 'expiry')
--             THEN (
--                 CASE WHEN p.direction = 'down'
--                      THEN (p.lower_barrier - p.entry_price)
--                      ELSE (p.upper_barrier - p.entry_price)
--                 END
--                 / (p.entry_price - p.lower_barrier)
--             )
--             ELSE -1.0
--         END
--     ) FILTER (
--         WHERE p.outcome <> 'pending'
--           AND p.entry_price > 0
--           AND p.lower_barrier > 0
--           AND p.lower_barrier < p.entry_price
--           AND CASE WHEN p.direction = 'down'
--                    THEN p.lower_barrier > p.entry_price
--                    ELSE p.upper_barrier > p.entry_price
--               END
--     ) AS avg_realized_ratio
-- FROM finding f
-- JOIN prediction p ON p.id = f.prediction_id
-- WHERE f.status = 'surfaced'
-- GROUP BY f.method, f.audience_user_id;
-- CREATE UNIQUE INDEX finding_payoff_key
--     ON finding_payoff (method, audience_user_id) NULLS NOT DISTINCT;
