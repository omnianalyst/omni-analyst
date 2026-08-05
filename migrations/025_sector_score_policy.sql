-- The claim_type_policy row for 'sector_score', added by 024.
--
-- 1 day: sector ETF prices update every session day. The scanner's
-- rs_percentile and trend fields cannot be meaningful at sub-daily resolution
-- (a daily bar is superseded by the next session), so 1 day -- the same
-- cadence price_snapshot uses -- is correct. The macro_alignment field changes
-- at the regime_assessment cadence (35 days), but that is the slower of the two
-- inputs, and the scanner recomputes the whole score when either input moves.

INSERT INTO claim_type_policy (claim_type, default_staleness, note) VALUES
    ('sector_score', INTERVAL '1 day',
     'autonomous sector scan from daily ETF prices + regime assessment');

-- DOWN

DELETE FROM claim_type_policy WHERE claim_type = 'sector_score';
