CREATE TABLE watchlist_entry_demand (
    watchlist_id  UUID NOT NULL,
    entity_id     UUID NOT NULL,
    demand_id     UUID NOT NULL UNIQUE REFERENCES demand(id) ON DELETE CASCADE,

    PRIMARY KEY (watchlist_id, entity_id, demand_id),
    FOREIGN KEY (watchlist_id, entity_id)
        REFERENCES watchlist_entry(watchlist_id, entity_id) ON DELETE CASCADE
);

-- The legacy representative link is exact. No other historical ownership can
-- be recovered honestly because watchlist demand shared the direct channel.
INSERT INTO watchlist_entry_demand (watchlist_id, entity_id, demand_id)
SELECT watchlist_id, entity_id, demand_id
FROM watchlist_entry
WHERE demand_id IS NOT NULL;

-- DOWN

DROP TABLE IF EXISTS watchlist_entry_demand;
