-- Private header bulletin: short notes and safe http(s) links per user.

CREATE TABLE IF NOT EXISTS bulletin_item (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('note', 'link')),
    title       TEXT NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 100),
    body        TEXT CHECK (body IS NULL OR length(body) <= 1000),
    url         TEXT CHECK (url IS NULL OR length(url) <= 2000),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((kind = 'link' AND url IS NOT NULL) OR (kind = 'note' AND url IS NULL))
);

CREATE INDEX IF NOT EXISTS bulletin_item_user_idx
    ON bulletin_item (user_id, updated_at DESC);

-- DOWN

DROP TABLE IF EXISTS bulletin_item;
