-- Derivatives claim types: funding rate, open interest, liquidations, basis.
--
-- These are crypto's edge -- the strategies in AUTOTRADE_PLAN.md PHASE 2
-- (carry.funding, basis.crossvenue, oi.divergence) are blocked until they exist.
-- Added to the shared claim_type enum the same way 003 added the on-chain and
-- perception kinds, rather than per-adapter, because claim_type is shared and
-- concurrent work orders extending it would collide. No new tables: these are
-- claims, and the claim table already holds them.
--
-- `basis` is added now so the claim_type exists, but it is *computed* from a
-- spot price and a perp price/funding -- no adapter in P13 emits it. Its
-- producer (basis.crossvenue) reads funding_rate claims written by this adapter.
--
-- Funding and OI come from keyless public endpoints (Binance/Bybit/OKX), which
-- puts them in the `allowed` licence class: unlike CoinGecko's byo_only prices,
-- these claims accumulate as shared network coverage.

ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'funding_rate';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'open_interest';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'liquidation_event';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'basis';
