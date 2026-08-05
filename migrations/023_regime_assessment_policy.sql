-- The claim_type_policy row for 'regime_assessment', added by 022.
--
-- 35 days: the binding input cadence is monthly (Sahm/CPI/LEI/output gap are
-- all monthly FRED series). The yield curve is daily, but the regime
-- assessment's headline fields (recession_probability, cycle_phase) cannot
-- change faster than the slowest monthly input, so 35 days -- the same
-- cadence macro_series_point uses for monthly FRED -- is correct. Copying the
-- yield curve's 1-day staleness would mark every assessment stale for ~29 of
-- every 30 days and drive pointless recomputation.

INSERT INTO claim_type_policy (claim_type, default_staleness, note) VALUES
    ('regime_assessment', INTERVAL '35 days',
     'autonomous macro regime from monthly FRED signals; only as fresh as its slowest input');

-- DOWN

DELETE FROM claim_type_policy WHERE claim_type = 'regime_assessment';
