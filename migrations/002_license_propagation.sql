-- Licence propagation through the derivation graph.
--
-- 001 pins a claim's audience to its own declared redistribution class. That
-- is not sufficient: an agent can read a byo_only price series, compute a
-- signal from it, and write the result as 'allowed'. The raw data never moves,
-- but the restricted content reaches another user anyway. Enforcement has to
-- follow derivation, not just ingestion.

CREATE TYPE claim_derivation AS ENUM ('ingested', 'derived');

ALTER TABLE claim
    ADD COLUMN derivation claim_derivation NOT NULL DEFAULT 'ingested';


CREATE TABLE claim_input (
    claim_id  UUID NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    input_id  UUID NOT NULL REFERENCES claim(id) ON DELETE RESTRICT,
    PRIMARY KEY (claim_id, input_id),
    CONSTRAINT claim_input_is_not_self CHECK (claim_id <> input_id)
);

CREATE INDEX claim_input_by_input ON claim_input (input_id);


-- A derived claim is at most as redistributable as its least redistributable
-- input, and inherits that input's audience.
CREATE FUNCTION enforce_license_propagation() RETURNS TRIGGER AS $$
DECLARE
    derived_class     redistribution;
    derived_audience  UUID;
    input_class       redistribution;
    input_audience    UUID;
BEGIN
    SELECT redistributable, audience_user_id
      INTO derived_class, derived_audience
      FROM claim WHERE id = NEW.claim_id;

    SELECT redistributable, audience_user_id
      INTO input_class, input_audience
      FROM claim WHERE id = NEW.input_id;

    IF input_class = 'byo_only' AND derived_class <> 'byo_only' THEN
        RAISE EXCEPTION
            'claim % derives from byo_only claim % but is marked %',
            NEW.claim_id, NEW.input_id, derived_class
            USING ERRCODE = 'check_violation';
    END IF;

    IF input_audience IS NOT NULL
       AND derived_audience IS DISTINCT FROM input_audience THEN
        RAISE EXCEPTION
            'claim % derives from claim % private to %, but is scoped to %',
            NEW.claim_id, NEW.input_id, input_audience, derived_audience
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER claim_input_propagates_license
    BEFORE INSERT OR UPDATE ON claim_input
    FOR EACH ROW
    EXECUTE FUNCTION enforce_license_propagation();


-- The rule above only fires when provenance is declared, so an agent could
-- evade it by declaring none. A claim marked 'derived' must therefore name at
-- least one input. Deferred, because the claim necessarily exists before its
-- edges do -- the check runs at commit, not mid-transaction.
CREATE FUNCTION enforce_derived_claim_has_inputs() RETURNS TRIGGER AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM claim_input WHERE claim_id = NEW.id) THEN
        RAISE EXCEPTION
            'claim % is marked derived but declares no inputs', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER claim_derived_requires_inputs
    AFTER INSERT OR UPDATE ON claim
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.derivation = 'derived')
    EXECUTE FUNCTION enforce_derived_claim_has_inputs();


-- Shared coverage: what may be served to any user. Every read path that is
-- not explicitly scoped to one user should go through this, so "did we filter
-- on audience" is answered once rather than at every call site.
CREATE VIEW shared_coverage AS
SELECT * FROM claim
WHERE audience_user_id IS NULL
  AND redistributable = 'allowed'
  AND superseded_by IS NULL;

-- DOWN

DROP VIEW IF EXISTS shared_coverage;
DROP TRIGGER IF EXISTS claim_derived_requires_inputs ON claim;
DROP FUNCTION IF EXISTS enforce_derived_claim_has_inputs();
DROP TRIGGER IF EXISTS claim_input_propagates_license ON claim_input;
DROP FUNCTION IF EXISTS enforce_license_propagation();
DROP TABLE IF EXISTS claim_input;
ALTER TABLE claim DROP COLUMN IF EXISTS derivation;
DROP TYPE IF EXISTS claim_derivation;
