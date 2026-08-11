-- The `holding` claim type: what an ETF or fund actually holds.
--
-- The exposure tool maps ETF -> holdings -> themes, answering "if I buy VTI,
-- what am I actually buying?" and "how much do my ETFs overlap?" This requires
-- holdings data as claims (not static edges), because fund compositions
-- rebalance quarterly and the weight drift matters: a holding claim is
-- superseded by the next filing, not overwritten.
--
-- A holding claim's `key` is the constituent's ticker (or CUSIP when no
-- ticker), and its `value` carries at minimum the portfolio weight. The
-- entity it attaches to is the ETF/fund itself.
--
-- Enum value only, following 041. Postgres will not let a transaction that
-- ADDs an enum value also INSERT a row referencing it (see 011's header), so
-- the claim_type_policy row must land in 051.

ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'holding';
