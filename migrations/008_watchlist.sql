-- Watchlists: a user-facing list whose entries raise demand.
--
-- A watchlist is not a standalone feature here; it is a second inlet to the
-- demand ledger. Adding an entity raises demand rows for a sensible default set
-- of claim types for that entity's kind; removing it withdraws them. Coverage
-- accumulates for a watched name whether or not anyone ever asks a question
-- about it, which is the signal a shared network exists to capture.
--
-- `watchlist_entry.demand_id` links an entry back to a demand row it raised, so
-- removal can withdraw what the entry added rather than guessing. The link is
-- nullable because an entry's demand set is one row per default claim type, and
-- a single column can name only one of them; withdrawal therefore matches the
-- rows add_entity wrote by their shape rather than by this id alone. See
-- omni.watchlist.lists for that reasoning.

CREATE TABLE watchlist (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX watchlist_by_user ON watchlist (user_id);

CREATE TABLE watchlist_entry (
    watchlist_id  UUID NOT NULL REFERENCES watchlist(id) ON DELETE CASCADE,
    entity_id     UUID NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    demand_id     UUID REFERENCES demand(id) ON DELETE SET NULL,
    PRIMARY KEY (watchlist_id, entity_id)
);

CREATE INDEX watchlist_entry_by_entity ON watchlist_entry (entity_id);

-- DOWN

DROP TABLE IF EXISTS watchlist_entry;
DROP TABLE IF EXISTS watchlist;
