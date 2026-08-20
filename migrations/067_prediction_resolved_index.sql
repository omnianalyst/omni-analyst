-- An index for the calibration hit-rate count.
--
-- `_entity_record` (conviction/disconfirm.py) counts resolved predictions per
-- (entity, method, audience, resolved_at) every time evidence is gathered --
-- the surface loop, the prediction pass, anything that grades a claim against
-- its track record. The only entity-keyed index was partial on
-- outcome = 'pending' (migration 052 era), which is the opposite rows: the
-- resolved set was seq-scanned, and on the 2.0M-row production prediction
-- table that meant a full scan per call -- 175,000 of them, pinning Postgres
-- at a constant 100% CPU with the apps themselves near-idle (measured
-- 2026-08-19). This partial index covers exactly the rows the query counts.
CREATE INDEX prediction_resolved_evidence
    ON prediction (entity_id, method, audience_user_id, resolved_at)
    WHERE outcome <> 'pending'::prediction_outcome;

-- DOWN
DROP INDEX IF EXISTS prediction_resolved_evidence;
