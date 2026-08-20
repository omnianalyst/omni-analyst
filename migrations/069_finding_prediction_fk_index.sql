-- FK index for finding.prediction_id.
--
-- Created directly on production during the 2026-08-20 prediction dedup after
-- the first delete batch ran 15 minutes: every prediction row DELETE checks
-- this FK, and without an index each check seq-scanned the 836K-row finding
-- table. The same probe happens on every finding INSERT/DELETE touching
-- prediction_id, so the index earns its keep beyond the one-time cleanup.
--
-- IF NOT EXISTS because production already has it (CREATE INDEX CONCURRENTLY,
-- which cannot run inside the migrator's transaction).

CREATE INDEX IF NOT EXISTS idx_finding_prediction_id ON finding (prediction_id);
