-- The four coverage domains, the entity graph, and per-type freshness.
--
-- Adding these centrally rather than per-adapter: claim_type is a shared enum,
-- and concurrent work orders each extending it would collide.

ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'perception_news';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'perception_macro';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'perception_social';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'perception_positioning';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'perception_divergence';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'onchain_flow';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'onchain_tvl';
ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'onchain_supply';
