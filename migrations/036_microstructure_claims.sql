-- Microstructure claim types: order book snapshot and trade tape.
--
-- Two consumers are blocked until these exist:
--
-- * venue/costs.py charges a configured spread_bps on every taker fill. That
--   number is currently a caller-supplied constant; a measured spread per
--   symbol per venue -- the difference between the best bid and best ask -- is
--   what turns the cost model from a guess into a description of the market,
--   and the router accepts or rejects strategies on it.
-- * detect/manipulation.py runs on OHLCV percentiles. Wash trading does not
--   show up in daily bars; it shows up in the tape and the book. The detector
--   cannot see what it was built to see without this data.
--
-- Added to the shared claim_type enum the same way 035 added the derivatives
-- kinds, rather than per-adapter, because claim_type is shared and concurrent
-- work orders extending it would collide. No new tables: these are claims, and
-- the claim table already holds them.
--
-- Sampled, not streamed. The book snapshot and the trade tape are point-in-time
-- observations a gap-filler fetches on demand; a streaming path is a later
-- phase.

ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'orderbook_snapshot';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'trade_tape';
