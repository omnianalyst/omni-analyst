-- Token launches, observed forward, so a base rate can exist.
--
-- The question this table is built to answer is **what fraction of new tokens
-- go to zero**, and that number is the denominator of every claim anyone makes
-- about launch trading. It cannot be reconstructed after the fact: the pools
-- that died are delisted from every aggregator index, so a survey taken later
-- surveys survivors and reports a base rate that is wrong in the direction that
-- makes the strategy look good. Finding 42 is the same failure in miniature --
-- an endpoint that answers `retCode: 0` while omitting everything delisted.
--
-- **So the cohort is frozen BEFORE outcomes are known, and nothing is filtered
-- on the way in.** Obvious junk is recorded with the same care as a promising
-- launch: a pool with $0 liquidity and four dollars of volume is not noise here,
-- it IS the measurement. Filtering at collection time would destroy exactly the
-- denominator the table exists to establish, and it would do so invisibly.
--
-- `launch_sweep` exists so that ABSENCE is interpretable. Without it, a pool
-- with no observation on a given day is ambiguous between "the collector looked
-- and it was gone" and "the collector did not run", and those two facts point in
-- opposite directions -- the first is a death, the second is a gap. One row per
-- sweep makes the difference legible, and a sweep that fails records nothing,
-- which then shows up as a missing sweep rather than as a cohort of survivors.
--
-- Money columns are NUMERIC for the reason every other table here gives: these
-- are small numbers whose ratios decide the result, and binary error in a
-- liquidity figure moves a pool across a threshold.

CREATE TABLE launch_sweep (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    network     TEXT NOT NULL,

    -- 'discover' finds pools not seen before; 'reobserve' re-reads a known
    -- cohort. Recorded because the two have different coverage guarantees and
    -- a reader must not average across them.
    kind        TEXT NOT NULL,

    swept_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    pools_seen  INTEGER NOT NULL,

    CONSTRAINT launch_sweep_names_its_kind
        CHECK (kind IN ('discover', 'reobserve')),
    CONSTRAINT launch_sweep_names_its_network
        CHECK (network <> ''),
    CONSTRAINT launch_sweep_counts_are_counts
        CHECK (pools_seen >= 0)
);

CREATE INDEX launch_sweep_by_time ON launch_sweep (network, kind, swept_at DESC);


CREATE TABLE launch_observation (
    network         TEXT NOT NULL,
    pool_address    TEXT NOT NULL,
    observed_at     TIMESTAMPTZ NOT NULL,
    sweep_id        UUID NOT NULL REFERENCES launch_sweep(id) ON DELETE CASCADE,

    -- FALSE means the sweep looked for this pool and the venue no longer served
    -- it. That is a measurement -- very likely the death this table exists to
    -- count -- and it is not the same as no row at all.
    present         BOOLEAN NOT NULL,

    pool_created_at TIMESTAMPTZ,
    name            TEXT,
    base_token      TEXT,
    quote_token     TEXT,

    price_usd       NUMERIC,
    liquidity_usd   NUMERIC,
    volume_24h_usd  NUMERIC,
    fdv_usd         NUMERIC,
    market_cap_usd  NUMERIC,

    -- Buys against sells, and unique buyers against unique sellers. The pair is
    -- the cheapest honeypot tell there is: a contract that accepts buys and
    -- reverts sells shows a healthy price series and almost no unique sellers,
    -- and no OHLCV series can see that.
    buys_24h        INTEGER,
    sells_24h       INTEGER,
    buyers_24h      INTEGER,
    sellers_24h     INTEGER,

    PRIMARY KEY (network, pool_address, observed_at),

    -- An absent pool has nothing to measure. Storing zeros instead would assert
    -- a pool that exists and happens to be empty, which is a different and
    -- commonplace state -- and the two must stay distinguishable.
    CONSTRAINT launch_observation_absent_measures_nothing
        CHECK (present OR (price_usd IS NULL AND liquidity_usd IS NULL
                           AND volume_24h_usd IS NULL AND buys_24h IS NULL)),

    CONSTRAINT launch_observation_amounts_are_numbers
        CHECK ((price_usd IS NULL OR price_usd <> 'NaN'::numeric)
               AND (liquidity_usd IS NULL OR liquidity_usd <> 'NaN'::numeric)
               AND (volume_24h_usd IS NULL OR volume_24h_usd <> 'NaN'::numeric)
               AND (fdv_usd IS NULL OR fdv_usd <> 'NaN'::numeric)
               AND (market_cap_usd IS NULL OR market_cap_usd <> 'NaN'::numeric)),

    CONSTRAINT launch_observation_amounts_are_not_negative
        CHECK ((liquidity_usd IS NULL OR liquidity_usd >= 0)
               AND (volume_24h_usd IS NULL OR volume_24h_usd >= 0)
               AND (price_usd IS NULL OR price_usd >= 0)),

    CONSTRAINT launch_observation_counts_are_counts
        CHECK ((buys_24h IS NULL OR buys_24h >= 0)
               AND (sells_24h IS NULL OR sells_24h >= 0)
               AND (buyers_24h IS NULL OR buyers_24h >= 0)
               AND (sellers_24h IS NULL OR sellers_24h >= 0)),

    CONSTRAINT launch_observation_names_its_pool
        CHECK (network <> '' AND pool_address <> '')
);

-- The two reads: one cohort's whole history, and every pool first seen in a
-- window (which is how a cohort is defined in the first place).
CREATE INDEX launch_observation_by_pool
    ON launch_observation (network, pool_address, observed_at);
CREATE INDEX launch_observation_by_time
    ON launch_observation (observed_at DESC);
CREATE INDEX launch_observation_by_creation
    ON launch_observation (network, pool_created_at);

-- DOWN

DROP TABLE IF EXISTS launch_observation;
DROP TABLE IF EXISTS launch_sweep;
