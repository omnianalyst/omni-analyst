-- Cross-domain relationships, and how fast each kind of claim goes stale.
--
-- Separate from 003 because Postgres forbids using an enum value in the same
-- transaction that adds it.

-- BTC relates to COIN; oil relates to XLE; the Fed funds rate relates to
-- everything. Cross-domain analysis is impossible without this, and it is the
-- reason a coverage network beats four separate dashboards.
CREATE TABLE entity_edge (
    from_entity  UUID NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    to_entity    UUID NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    relation     TEXT NOT NULL,
    weight       DOUBLE PRECISION NOT NULL DEFAULT 1.0 CHECK (weight > 0),
    source       TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_entity, to_entity, relation),
    CONSTRAINT entity_edge_is_not_a_self_loop CHECK (from_entity <> to_entity)
);

CREATE INDEX entity_edge_reverse ON entity_edge (to_entity, relation);


-- Default staleness per claim type.
--
-- A sentiment reading is stale in hours; a 10-K is fresh for a quarter. Without
-- this the gap engine would need every demand row to specify max_staleness, and
-- an unspecified one would never go stale at all.
CREATE TABLE claim_type_policy (
    claim_type        claim_type PRIMARY KEY,
    default_staleness INTERVAL NOT NULL,
    note              TEXT
);

INSERT INTO claim_type_policy (claim_type, default_staleness, note) VALUES
    ('price_snapshot',         INTERVAL '1 day',    'a daily bar is superseded by the next session'),
    ('fundamental_metric',     INTERVAL '100 days', 'quarterly reporting plus filing lag'),
    ('filing_event',           INTERVAL '30 days',  'filings arrive irregularly; absence is informative'),
    ('macro_series_point',     INTERVAL '35 days',  'most FRED series are monthly'),
    ('news_event',             INTERVAL '2 days',   NULL),
    ('manipulation_signal',    INTERVAL '1 day',    'derived from a rolling window of recent bars'),
    ('perception_news',        INTERVAL '12 hours', 'news tone decays fast'),
    ('perception_macro',       INTERVAL '7 days',   'survey and volatility indices are weekly or slower'),
    ('perception_social',      INTERVAL '4 hours',  'the fastest-decaying claim in the system'),
    ('perception_positioning', INTERVAL '7 days',   'short interest and ETF flows report on a lag'),
    ('perception_divergence',  INTERVAL '1 day',    'derived; only as fresh as its inputs'),
    ('onchain_flow',           INTERVAL '6 hours',  NULL),
    ('onchain_tvl',            INTERVAL '1 day',    NULL),
    ('onchain_supply',         INTERVAL '1 day',    NULL);

-- DOWN

DROP TABLE IF EXISTS claim_type_policy;
DROP TABLE IF EXISTS entity_edge;
