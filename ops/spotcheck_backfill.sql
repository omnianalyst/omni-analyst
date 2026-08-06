-- Post-deploy backfill honesty spot-check.
--
-- The cold-start backfill replays the trend producer at historical timestamps
-- so the conviction gate has resolved calibration on day one. Its honesty rests
-- on one property: at each replay timestamp ts, the producer saw only prices
-- knowable at ts -- no look-ahead. That property is enforced BY CONSTRUCTION in
-- conviction/trend.py (_price_window filters `knowledge_date <= as_of`), so this
-- script does not hunt a bug; it confirms the construction holds against the
-- live data and watches for the symptom of a leak.
--
-- A look-ahead leak inflates the backtest hit-rate to near-certainty (a backtest
-- that sees the future wins almost every time). An honest trend edge resolves in
-- the ~60-75% band. That calibration number is the load-bearing check; the other
-- two are sanity.
--
-- Run against the prod DB, e.g. from the host:
--   docker compose -f docker-compose.prod.yml exec -T postgres \
--     psql -U "$PGUSER" -d "$PGDATABASE" -f - < ops/spotcheck_backfill.sql
-- (Wait ~30 min after first boot for the background backfill to finish.)


\echo '1. Did the backfill run? Backfilled rows carry created_at in the past'
\echo '   (the replay timestamp), distinct from now(). Zero older_than_1d means'
\echo '   the backfill has not finished (or never ran).'
SELECT
    count(*)                                                   AS total_predictions,
    count(*) FILTER (WHERE created_at < now() - interval '1 day') AS backfilled_older_than_1d,
    min(created_at)                                            AS oldest_created_at
FROM prediction
WHERE method LIKE 'trend.sma%';


\echo ''
\echo '2. Calibration hit-rate -- THE look-ahead detector. ~60-75% is an honest'
\echo '   trend edge. >= ~90% on a 2-year backtest is the red flag: a backtest that'
\echo '   sees the future wins almost every time. Each method suffix (.w20/.w50/...)'
\echo '   is its own bucket; compare them.'
SELECT
    method,
    count(*) FILTER (WHERE outcome <> 'pending')                                            AS resolved,
    count(*) FILTER (WHERE outcome <> 'pending'
                      AND ((direction = 'up' AND outcome = 'upper')
                        OR (direction = 'down' AND outcome = 'lower')))                     AS hits,
    round(100.0 *
        count(*) FILTER (WHERE outcome <> 'pending'
                          AND ((direction = 'up' AND outcome = 'upper')
                            OR (direction = 'down' AND outcome = 'lower')))
        / NULLIF(count(*) FILTER (WHERE outcome <> 'pending'), 0), 1)                       AS hit_rate_pct
FROM prediction
WHERE method LIKE 'trend.sma%'
GROUP BY method
ORDER BY method;


\echo ''
\echo '3. Prices carry real historical knowledge_dates -- not bulk-stamped at one'
\echo '   ingest time. If distinct_kdays is small or oldest_knowledge is recent, the'
\echo '   producer filter would have abstained (good) or, if wrongly dated, cheated.'
\echo '   Expect hundreds of distinct days spanning ~2 years.'
SELECT
    count(*)                              AS prices,
    min(knowledge_date)                   AS oldest_knowledge,
    max(knowledge_date)                   AS newest_knowledge,
    count(DISTINCT knowledge_date::date)  AS distinct_kdays
FROM claim
WHERE claim_type = 'price_snapshot';
