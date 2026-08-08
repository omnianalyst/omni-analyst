-- Record whether a counter-case was actually looked for.
--
-- Until the disconfirming search existed, the only producer of findings
-- hardcoded `searched_for_disconfirming=True` and set `supporting` to a
-- restatement of the call itself. No search ever ran, and `disconfirming` is
-- empty on every one of those rows.
--
-- The row shape cannot tell that apart from a finding where the search DID run
-- and legitimately found nothing -- both are `disconfirming = '[]'`. That
-- ambiguity is load-bearing in the UI, which renders an empty list as "the
-- checks ran and found none": true for a new finding, a lie for an old one.
--
-- This column resolves it without touching a single row's history. Existing
-- findings default to false, which is exactly correct for them -- no search was
-- performed. New findings carry what the gate was actually told. The UI reads
-- the flag rather than inferring from an empty list.
--
-- Deleting the legacy rows was considered and rejected. There are roughly fifty
-- thousand surfaced findings in production; deleting them would discard the
-- resolved outcomes behind the published hit rate and force a re-assessment of
-- every prediction behind them. The scorecard's value is that it is a long,
-- unedited record. A flag that marks the old rows honestly costs nothing and
-- keeps that record intact.

ALTER TABLE finding
    ADD COLUMN evidence_searched boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN finding.evidence_searched IS
    'Whether a disconfirming search actually ran for this finding. False on '
    'rows written before the search existed: for those, an empty disconfirming '
    'list means "never looked", not "looked and found nothing".';

-- A surfaced finding claiming a completed search must name what the search
-- turned up, on at least one side. Refusals are exempt for the same reason the
-- evidence CHECK is (031): a refusal's traceability is its reason.
ALTER TABLE finding
    ADD CONSTRAINT searched_findings_report_what_they_found
        CHECK (
            NOT evidence_searched
            OR status <> 'surfaced'::finding_status
            OR supporting <> '[]'::jsonb
            OR disconfirming <> '[]'::jsonb
        );

-- DOWN

ALTER TABLE finding
    DROP CONSTRAINT IF EXISTS searched_findings_report_what_they_found;

ALTER TABLE finding DROP COLUMN IF EXISTS evidence_searched;
