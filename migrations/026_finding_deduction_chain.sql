-- Phase E: deduction_chain column on finding.
--
-- A surfaced autonomous finding carries the full reasoning that produced it:
-- the regime assessment (macro) that set the backdrop, the sector score that
-- identified the standout sector, and the stock prediction that the conviction
-- gate surfaced. Each link is a claim with provenance -- the chain is
-- auditable, not a black box (AUTONOMOUS_PLAN.md invariant #5).
--
-- The existing `supporting` JSONB list carries evidence strings; the chain is a
-- structured object with typed layers, so it gets its own column rather than
-- overloading supporting's semantics. The finding's own `prediction_id` is the
-- stock layer; the chain names the macro and sector layers above it.

ALTER TABLE finding
    ADD COLUMN deduction_chain JSONB NOT NULL DEFAULT '[]'::jsonb;

-- DOWN

ALTER TABLE finding DROP COLUMN IF EXISTS deduction_chain;
