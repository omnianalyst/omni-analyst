-- Phase F: meta-calibration -- the system learns whether its DEDUCTION is
-- sound, not just its predictions.
--
-- A regime_assessment that says "risk_on" is correct if the market rose over
-- the following window. A sector_score that ranks XLK highest is correct if XLK
-- actually led. This table records those meta-resolutions: whether each
-- autonomous assessment (regime or sector) was directionally right against the
-- market data that subsequently arrived. The meta-hit-rate feeds back into the
-- sector scanner's weighting -- sectors the system has historically been right
-- about earn higher autonomous demand (AUTONOMOUS_PLAN.md Phase F).
--
-- One row per assessed claim. PK on claim_id makes it idempotent: re-running
-- the meta-calibration on an already-resolved claim is a no-op.

CREATE TABLE meta_resolution (
    claim_id        UUID PRIMARY KEY REFERENCES claim(id) ON DELETE CASCADE,
    claim_type      TEXT NOT NULL,
    correct         BOOLEAN NOT NULL,
    actual_outcome  TEXT,
    resolved_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    evidence        JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX meta_resolution_by_type ON meta_resolution (claim_type, correct);

-- DOWN

DROP TABLE IF EXISTS meta_resolution;
