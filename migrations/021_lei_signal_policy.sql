-- The claim_type_policy row for 'lei_signal', added by 020.
--
-- Postgres will not let a transaction that ADDs an enum value also INSERT a row
-- referencing it ("unsafe use of new value ... New enum values must be
-- committed before they can be used"). Migrations 010/011, 013/014, 015/016 and
-- 017/018 already established the split (enum value alone, policy row in the
-- next migration); 020/021 is that split landing for the one value 020 added,
-- following the established convention rather than inventing a sixth one.
--
-- Staleness = 35 days: USSLIND is a MONTHLY FRED series (verified against FRED
-- via the API: "Leading Index for the United States", frequency Monthly), so the
-- signal's fields (is_negative, change_6m) can change at most once per
-- publication month. This is the same crux 010/013/015/017 hit: 010's daily
-- treasury yields got INTERVAL '1 day' because DGS2/DGS10 publish every session
-- day; copying that here would mark every LEI reading stale for ~29 of every 30
-- days and drive pointless refills. 35 days is the macro_series_point default,
-- explicitly documented in 004 as "most FRED series are monthly" -- USSLIND is
-- exactly that, so the derived signal matches its input's cadence (the
-- "derived; only as fresh as its inputs" convention applied to a monthly input).
-- 35 ~= one monthly cycle plus slack for the publication lag.

INSERT INTO claim_type_policy (claim_type, default_staleness, note) VALUES
    ('lei_signal', INTERVAL '35 days',
     'derived from monthly USSLIND leading index; only as fresh as its inputs');

-- DOWN

DELETE FROM claim_type_policy WHERE claim_type = 'lei_signal';
