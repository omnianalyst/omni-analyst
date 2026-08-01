-- The claim_type_policy row for 'sahm_rule_signal', added by 013.
--
-- Postgres will not let a transaction that ADDs an enum value also INSERT a
-- row referencing it -- confirmed empirically against this instance in D10's
-- report ("unsafe use of new value ... New enum values must be committed
-- before they can be used"). Migrations 010/011 already established the split
-- (enum value alone in 010, policy row in the next migration 011); 013/014 is
-- that split landing for the one value 013 added, following the established
-- convention rather than inventing a second one.
--
-- Staleness = 35 days: UNRATE is a MONTHLY FRED series (verified against FRED:
-- frequency "Monthly", units "Percent", Seasonally Adjusted), so the Sahm
-- signal's headline fields (value, triggered) can change at most once per
-- publication month. This is the crux where D10's template needed a daily-vs-
-- monthly adjustment: 010's daily treasury yields got INTERVAL '1 day' because
-- DGS2/DGS10 publish every session day; copying that here would mark every
-- Sahm reading stale for ~29 of every 30 days and drive pointless refills.
-- 35 days is the macro_series_point default, explicitly documented in 004 as
-- "most FRED series are monthly" -- UNRATE is exactly that, so the derived
-- signal matches its input's cadence (the "derived; only as fresh as its
-- inputs" convention applied to a monthly input). 35 ~= one monthly cycle plus
-- slack for the ~1-month publication lag (a reference month's reading lands in
-- the first week of the following month).

INSERT INTO claim_type_policy (claim_type, default_staleness, note) VALUES
    ('sahm_rule_signal', INTERVAL '35 days',
     'derived from monthly UNRATE unemployment rate; only as fresh as its inputs');

-- DOWN

DELETE FROM claim_type_policy WHERE claim_type = 'sahm_rule_signal';
