-- Let a finding exist without a backing claim.
--
-- A finding was anchored to exactly one claim (finding.claim_id NOT NULL). That
-- fits a claim-sourced finding, but not an analysis-sourced one: a risk score, a
-- Sharpe ratio, a correlation coefficient has no single claim it points at. The
-- minimal honest change is to let claim_id be NULL -- and to require that a
-- claim-less finding still names *something* traceable.
--
-- The evidence for a claim-less finding lives in `supporting` (JSONB, already
-- NOT NULL DEFAULT '[]', already described in 006 as the finding's evidence
-- list) rather than a new column. A second field duplicating "the evidence
-- behind this finding" would be two places for one fact: a claim-backed finding
-- already records its supporting reasons there, and a claim-less finding records
-- its input claim ids and computed result there. One evidence field, two kinds
-- of finding.
--
-- This order relaxes claim_id ONLY. The pre-existing falsifiability invariant
-- (surfaced_findings_are_falsifiable: a surfaced finding must carry a
-- prediction_id) is a separate CHECK on a separate column and is not touched
-- here. That matters: a risk score has no natural upper/lower price barrier, so
-- how a non-price analysis becomes a real, scorable prediction is an open
-- question this migration does not answer -- it only stops the schema from
-- forbidding the row shape.

ALTER TABLE finding ALTER COLUMN claim_id DROP NOT NULL;

ALTER TABLE finding
    ADD CONSTRAINT claim_or_supporting_names_the_evidence
        CHECK (claim_id IS NOT NULL OR supporting <> '[]'::jsonb);

-- DOWN

ALTER TABLE finding
    DROP CONSTRAINT IF EXISTS claim_or_supporting_names_the_evidence;

-- Re-adding NOT NULL fails if any claim-less finding exists; the reverse is
-- only valid before one is written.
ALTER TABLE finding ALTER COLUMN claim_id SET NOT NULL;
