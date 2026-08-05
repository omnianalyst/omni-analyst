-- The sector_score claim type: the autonomous sector scanner's output
-- (Phase C of AUTONOMOUS_PLAN.md). A derived STATE -- "how does this sector
-- ETF rank right now" (relative-strength percentile, trend direction, macro
-- alignment) -- computed from the ETF's own price history and the current
-- regime_assessment. It earns a claim type because the autonomous demand loop
-- reads it to decide which sectors' constituents to demand, and the synthesis
-- finding carries it as the middle link of the deduction chain.
--
-- The enum addition is isolated here, ALONE, following 022/010/013/015/017/020.
-- The policy row lands in 025.
--
-- Staleness: derived from daily ETF prices + the regime assessment (35-day).
-- The binding cadence is daily (prices update every session), so 1 day -- the
-- same cadence price_snapshot uses -- is correct.

ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'sector_score';
