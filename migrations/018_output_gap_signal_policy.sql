-- The claim_type_policy row for 'output_gap_signal', added by 017.
-- (same ALTER-TYPE-then-INSERT-in-next-migration rationale as 011/014/016)
--
-- Staleness = 120 days: GDPC1 and GDPPOT are QUARTERLY FRED series (verified:
-- "Real Gross Domestic Product" / "Real Potential Gross Domestic Product",
-- frequency Quarterly), so the signal's headline field (output_gap) can change
-- at most once per publication quarter. This is the next cadence step after
-- 013/014/015/016: 010's daily treasury yields got INTERVAL '1 day'; 013/015's
-- monthly unemployment/CPI got the macro_series_point default 35 days. A
-- quarterly series needs roughly one quarter (~91 days) plus the publication
-- lag (an advance GDP estimate lands ~1 month after the reference quarter,
-- and GDPPOT updates on CBO's projection cycle), so ~120 days. Copying the
-- monthly 35 days here would mark every reading stale for ~85 of every 90 days
-- and drive pointless refills; copying a daily value would be worse. The
-- derived signal matches its input's cadence ("only as fresh as its inputs").

INSERT INTO claim_type_policy (claim_type, default_staleness, note) VALUES
    ('output_gap_signal', INTERVAL '120 days',
     'derived from quarterly GDPC1/GDPPOT real GDP; only as fresh as its inputs');

-- DOWN

DELETE FROM claim_type_policy WHERE claim_type = 'output_gap_signal';
