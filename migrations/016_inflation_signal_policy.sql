-- The claim_type_policy row for 'inflation_signal', added by 015.
--
-- Postgres will not let a transaction that ADDs an enum value also INSERT a
-- row referencing it -- confirmed empirically against this instance in D10's
-- report ("unsafe use of new value ... New enum values must be committed
-- before they can be used"). Migrations 010/011 and 013/014 already
-- established the split (enum value alone, policy row in the next migration);
-- 015/016 is that split landing for the one value 015 added, following the
-- established convention rather than inventing a third one.
--
-- Staleness = 35 days: CPIAUCSL is a MONTHLY FRED series (verified against
-- FRED: "Consumer Price Index for All Urban Consumers: All Items", frequency
-- Monthly, Seasonally Adjusted), so the signal's headline fields (yoy,
-- mom_annualized, 3m_annualized) can change at most once per publication month.
-- This is the same crux D10/D14 hit: 010's daily treasury yields got
-- INTERVAL '1 day' because DGS2/DGS10 publish every session day; copying that
-- here would mark every inflation reading stale for ~29 of every 30 days and
-- drive pointless refills. 35 days is the macro_series_point default,
-- explicitly documented in 004 as "most FRED series are monthly" -- CPIAUCSL
-- is exactly that, so the derived signal matches its input's cadence (the
-- "derived; only as fresh as its inputs" convention applied to a monthly
-- input). 35 ~= one monthly cycle plus slack for the ~1-month publication lag
-- (a reference month's reading lands in the second week of the following
-- month).

INSERT INTO claim_type_policy (claim_type, default_staleness, note) VALUES
    ('inflation_signal', INTERVAL '35 days',
     'derived from monthly CPIAUCSL consumer price index; only as fresh as its inputs');

-- DOWN

DELETE FROM claim_type_policy WHERE claim_type = 'inflation_signal';
