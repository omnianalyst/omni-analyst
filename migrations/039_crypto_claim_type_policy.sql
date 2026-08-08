-- claim_type_policy rows for the ten crypto claim types added by 035-038.
--
-- Migrations 035 (funding_rate, open_interest, liquidation_event, basis),
-- 036 (orderbook_snapshot, trade_tape), 037 (protocol_revenue, protocol_fees,
-- stablecoin_supply, chain_tvl) and 038 all added enum values without their
-- policy rows. Postgres will not let a transaction that ADDs an enum value also
-- INSERT a row referencing it -- migration 011's header records the empirical
-- confirmation -- so the rows have to land in a later migration regardless.
-- This is that migration for all ten at once.
--
-- Every claim type that existed before this wave carries a default_staleness.
-- Leaving ten without one makes the table silently non-exhaustive, so the first
-- consumer that reads it gets a NULL for exactly the newest, fastest-moving
-- coverage in the system.
--
-- The values are set by PUBLICATION CADENCE, not by preference. A staleness
-- shorter than the source's own publication interval marks coverage stale that
-- could not possibly have been refreshed, which burns fill budget re-fetching an
-- unchanged value. A staleness longer than it lets a genuinely old reading
-- present as current.
--
--   funding_rate       8h  -- Binance/Bybit/OKX settle on an 8-hour cycle; a
--                             rate cannot change between settlements
--   open_interest      1h  -- sampled, not settled; moves continuously
--   liquidation_event  1h  -- an event stream, sampled on the same cadence
--   basis              1h  -- derived from spot and perp marks, both continuous
--   orderbook_snapshot 5m  -- the shortest-lived claim in the system. A book is
--                             stale in seconds; 5m is the sampling floor, and
--                             anything older must not price a fill via
--                             venue/costs.py::entry_cost
--   trade_tape         5m  -- same sampling cadence as the book it accompanies
--   protocol_revenue   1d  -- DefiLlama publishes daily protocol snapshots
--   protocol_fees      1d  -- same publication
--   stablecoin_supply  1d  -- same publication
--   chain_tvl          1d  -- same publication

INSERT INTO claim_type_policy (claim_type, default_staleness, note) VALUES
    ('funding_rate', INTERVAL '8 hours',
     'perpetual funding settles on an 8-hour cycle; cannot change between settlements'),
    ('open_interest', INTERVAL '1 hour',
     'sampled rather than settled; moves continuously with position changes'),
    ('liquidation_event', INTERVAL '1 hour',
     'event stream sampled on the same cadence as open interest'),
    ('basis', INTERVAL '1 hour',
     'derived from spot and perpetual marks, both of which move continuously'),
    ('orderbook_snapshot', INTERVAL '5 minutes',
     'the shortest-lived claim in the system; an older book must not price a fill'),
    ('trade_tape', INTERVAL '5 minutes',
     'sampled on the same cadence as the book it accompanies'),
    ('protocol_revenue', INTERVAL '1 day',
     'DefiLlama publishes daily protocol snapshots'),
    ('protocol_fees', INTERVAL '1 day',
     'DefiLlama publishes daily protocol snapshots'),
    ('stablecoin_supply', INTERVAL '1 day',
     'DefiLlama publishes daily stablecoin snapshots'),
    ('chain_tvl', INTERVAL '1 day',
     'DefiLlama publishes daily chain snapshots');

-- DOWN

DELETE FROM claim_type_policy WHERE claim_type IN (
    'funding_rate', 'open_interest', 'liquidation_event', 'basis',
    'orderbook_snapshot', 'trade_tape', 'protocol_revenue', 'protocol_fees',
    'stablecoin_supply', 'chain_tvl'
);
