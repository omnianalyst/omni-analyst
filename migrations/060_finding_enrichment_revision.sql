-- Finding enrichment is a revision ledger, not a mutable finding field.
CREATE TABLE finding_enrichment_revision (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id       UUID NOT NULL REFERENCES finding(id) ON DELETE RESTRICT,
    evidence_as_of   TIMESTAMPTZ NOT NULL,
    deduction_chain  JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT finding_enrichment_chain_is_an_array
        CHECK (jsonb_typeof(deduction_chain) = 'array')
);

CREATE INDEX finding_enrichment_latest
    ON finding_enrichment_revision (finding_id, evidence_as_of DESC, created_at DESC);

CREATE FUNCTION refuse_finding_rewrite() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'the finding ledger is append-only: % on % is refused',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER finding_is_append_only
    BEFORE UPDATE OR DELETE ON finding
    FOR EACH ROW EXECUTE FUNCTION refuse_finding_rewrite();

CREATE TRIGGER finding_enrichment_is_append_only
    BEFORE UPDATE OR DELETE ON finding_enrichment_revision
    FOR EACH ROW EXECUTE FUNCTION refuse_finding_rewrite();

-- DOWN

DROP TRIGGER IF EXISTS finding_enrichment_is_append_only ON finding_enrichment_revision;
DROP TRIGGER IF EXISTS finding_is_append_only ON finding;
DROP FUNCTION IF EXISTS refuse_finding_rewrite();
DROP TABLE IF EXISTS finding_enrichment_revision;
