-- Realized payoff accounting for surfaced calls, per method and audience.
--
-- The hit rate answers "how often were we right". It cannot answer the
-- question a trader actually lives: "when we were right, did it pay more
-- than being wrong cost?" A method can be right 40% of the time and compound
-- beautifully if its winners are 5x its losers -- the entire premise of
-- asymmetric risk-reward -- and the hit rate alone would call it a failure.
--
-- Every prediction fixes its barriers at write time, so both distances are
-- known before the outcome, for any direction:
--   risk_pct   = |entry - invalidation barrier| / entry  (the wrong-side cap)
--   payoff_pct = |target barrier - entry| / entry        (what being right pays)
-- realized_ratio = payoff_pct / risk_pct per resolved call; a miss realizes
-- -1.0 in ratio terms because the call is exited at its invalidation level
-- by construction -- losses are capped, the upside is not, which is the
-- entire subject of this view.
--
-- Like hit_rate, only resolved predictions count, and the audience column
-- keeps a byo-derived ratio scoped to the operator whose demand produced it.

CREATE VIEW finding_payoff AS
SELECT
    f.method,
    f.audience_user_id,
    count(*) FILTER (WHERE p.outcome <> 'pending') AS resolved,
    -- average distance risked, percent of entry
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
    -- average distance called for, percent of entry
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
    -- per-call realized ratio, averaged: payoff/risk when right, -1 when wrong
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

-- DOWN

DROP VIEW IF EXISTS finding_payoff;
