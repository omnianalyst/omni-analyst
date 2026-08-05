-- The regime_assessment claim type: the autonomous macro-regime layer's
-- headline output (Phase B of AUTONOMOUS_PLAN.md). A derived STATE -- "what is
-- the current macro regime" (recession probability, cycle phase, risk regime,
-- inflation regime, policy stance) -- composed deterministically from the five
-- earned macro signal claims (yield_curve_signal, sahm_rule_signal,
-- inflation_signal, output_gap_signal, lei_signal). It earns a claim type
-- because the sector scanner consumes it (Layer 2's macro_alignment reads it)
-- and the synthesis finding traces it as the root of the deduction chain.
--
-- The enum addition is isolated here, ALONE, following 010/013/015/017/020
-- exactly: Postgres forbids using a new enum value in the same transaction that
-- adds it, and the Neutron migrator wraps each migration's up in one
-- transaction. The policy row lands in 023.
--
-- Staleness: derived from mixed daily (yield curve) and monthly (Sahm, CPI,
-- LEI, output gap) inputs. The binding cadence is monthly (the slowest input),
-- so 35 days matches the macro_series_point default -- "only as fresh as its
-- slowest input."

ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'regime_assessment';
