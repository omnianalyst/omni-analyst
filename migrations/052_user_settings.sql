-- User settings: encrypted credential storage per user.
-- One row per user, JSONB data column for flexibility.

CREATE TABLE IF NOT EXISTS user_settings (
    user_id    UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- DOWN

DROP TABLE IF EXISTS user_settings;
