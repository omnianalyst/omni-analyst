-- Retry backoff for gaps that fail transiently.
--
-- A lease stops two workers taking the same gap at the same time. It does not
-- stop one worker taking the same gap again immediately after failing, which
-- is a different problem and a much more expensive one: a gap whose source is
-- down gets re-attempted as fast as the loop can turn.
--
-- Observed before this existed: 3 gaps produced 14,900 fill attempts in 20
-- seconds. With real credentials configured that is a live API bill rather
-- than 14,900 rows saying "no API key configured".

ALTER TABLE gap
    ADD COLUMN attempts        INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN next_attempt_at TIMESTAMPTZ;

-- Claimable means: open, not leased, and not inside a backoff window.
DROP INDEX IF EXISTS gap_ranked_queue;
CREATE INDEX gap_ranked_queue
    ON gap (score DESC, detected_at)
    WHERE resolved_at IS NULL;

CREATE INDEX gap_backoff ON gap (next_attempt_at)
    WHERE resolved_at IS NULL AND next_attempt_at IS NOT NULL;

-- DOWN

DROP INDEX IF EXISTS gap_backoff;
ALTER TABLE gap DROP COLUMN IF EXISTS next_attempt_at;
ALTER TABLE gap DROP COLUMN IF EXISTS attempts;
