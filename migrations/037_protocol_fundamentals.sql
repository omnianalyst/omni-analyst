-- Extends claim_type for the DefiLlama protocol-fundamentals adapter (P15).
-- DefiLlama is the crypto counterpart to EDGAR: free, keyless, permissively
-- redistributable, so every claim these types carry accumulates as shared
-- network coverage -- the only redistributable crypto fundamentals source v2
-- has. Four values, one per adapter key kind:
--   protocol_fees     -- what users paid to use the protocol (gross)
--   protocol_revenue  -- the protocol's own share (fees minus LP/holder payouts)
--   stablecoin_supply -- a stablecoin's circulating supply, USD-denominated
--   chain_tvl         -- total value locked on a single chain
--
-- fees and revenue are distinct values because conflating them yields a P/F
-- ratio wrong by whatever the protocol distributes (often an order of
-- magnitude); the adapter emits them from separate dataType responses and the
-- tests fail if one is substituted for the other.
--
-- Enum-only, following 010/013/015/017/020/022/024. No new tables.

ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'protocol_revenue';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'protocol_fees';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'stablecoin_supply';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'chain_tvl';
