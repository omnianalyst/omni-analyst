ALTER TABLE loop_health
    ADD COLUMN last_status TEXT,
    ADD COLUMN last_result TEXT,
    ADD CONSTRAINT loop_health_status_is_known
        CHECK (last_status IS NULL OR last_status IN ('success', 'failure')),
    ADD CONSTRAINT loop_health_result_is_bounded
        CHECK (last_result IS NULL OR char_length(last_result) <= 2000),
    ADD CONSTRAINT loop_health_interval_is_positive
        CHECK (
            expected_interval_seconds IS NULL
            OR expected_interval_seconds > 0
        );

UPDATE loop_health
SET last_status = CASE
        WHEN consecutive_failures > 0 THEN 'failure'
        WHEN last_success_at IS NOT NULL THEN 'success'
        ELSE NULL
    END,
    last_error = left(last_error, 2000);

ALTER TABLE loop_health
    ADD CONSTRAINT loop_health_error_is_bounded
        CHECK (last_error IS NULL OR char_length(last_error) <= 2000);

INSERT INTO loop_health (loop_name, expected_interval_seconds)
VALUES
    ('sweep', 300),
    ('fill', 30),
    ('resolve', 60),
    ('predict', 300),
    ('surface', 300),
    ('alerts', 60),
    ('autonomous.macro', 86400),
    ('autonomous.sector', 43200),
    ('autonomous.demand', 3600),
    ('autonomous.synthesis', 300),
    ('autonomous.meta', 86400),
    ('venue_reconciliation', 360),
    ('carry', 86400),
    ('nav', 86400),
    ('shadow_decision', 86400),
    ('shadow_scoring', 86400),
    ('launch_sweep', 21600)
ON CONFLICT (loop_name) DO UPDATE SET
    expected_interval_seconds = EXCLUDED.expected_interval_seconds;

-- DOWN

ALTER TABLE loop_health
    DROP CONSTRAINT IF EXISTS loop_health_interval_is_positive,
    DROP CONSTRAINT IF EXISTS loop_health_error_is_bounded,
    DROP CONSTRAINT IF EXISTS loop_health_result_is_bounded,
    DROP CONSTRAINT IF EXISTS loop_health_status_is_known,
    DROP COLUMN IF EXISTS last_result,
    DROP COLUMN IF EXISTS last_status;
