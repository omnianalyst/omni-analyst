-- Scope the evidence constraint to surfaced findings, like every other
-- status-dependent invariant on this table.
--
-- 012 required every finding to name its evidence (claim_id or a non-empty
-- supporting list). That held only because the sole producer filled `supporting`
-- unconditionally with a restatement of the call itself -- "up directional call
-- from trend.sma" -- which named no evidence at all. Removing that tautology
-- (the disconfirming search now builds `supporting` from real signals, and
-- reports nothing when it found nothing) makes the constraint fire on refusals,
-- which is the wrong row to demand evidence from.
--
-- A refusal's traceability is its reason, already enforced by
-- `refusal_names_a_reason`. A surfaced finding is the one that must justify
-- itself, and that requirement is unchanged and now genuinely load-bearing:
-- a surfaced finding whose evidence list is empty and which anchors to no claim
-- is exactly the advocacy the conviction gate exists to refuse.
--
-- Matches the shape of surfaced_findings_are_falsifiable and
-- surfaced_findings_record_their_threshold: predicate on status, not on all rows.

ALTER TABLE finding
    DROP CONSTRAINT IF EXISTS claim_or_supporting_names_the_evidence;

ALTER TABLE finding
    ADD CONSTRAINT surfaced_findings_name_their_evidence
        CHECK (
            status <> 'surfaced'::finding_status
            OR claim_id IS NOT NULL
            OR supporting <> '[]'::jsonb
        );

-- DOWN

ALTER TABLE finding
    DROP CONSTRAINT IF EXISTS surfaced_findings_name_their_evidence;

-- Re-adding the unscoped form fails if any refusal without supporting evidence
-- exists; the reverse is only valid before one is written.
ALTER TABLE finding
    ADD CONSTRAINT claim_or_supporting_names_the_evidence
        CHECK (claim_id IS NOT NULL OR supporting <> '[]'::jsonb);
