-- The claim_type_policy row for 'yield_curve_signal', added by 010.
--
-- Postgres will not let a transaction that ADDs an enum value also INSERT a
-- row referencing it -- confirmed empirically against this instance in D10's
-- report ("unsafe use of new value ... New enum values must be committed
-- before they can be used"). Migration 003 already established the split
-- (enum values alone, policy rows in the next migration); this is that split
-- landing for the one value 010 added.
--
-- Staleness = 1 day: DGS2/DGS10 are daily treasury constant-maturity yields
-- (the macro_series_point default of 35 days is explicitly for monthly FRED
-- series), so the signal's headline fields (current_spread, is_inverted) can
-- change every publication day. This matches perception_divergence's
-- convention for a derived signal: "only as fresh as its inputs." A longer
-- value would let a stale inversion reading present as fresh coverage past
-- the next session.

INSERT INTO claim_type_policy (claim_type, default_staleness, note) VALUES
    ('yield_curve_signal', INTERVAL '1 day',
     'derived from daily DGS2/DGS10 treasury yields; only as fresh as its inputs');

-- DOWN

DELETE FROM claim_type_policy WHERE claim_type = 'yield_curve_signal';
