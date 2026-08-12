-- Private, read-only wallet accounts. Public addresses are still sensitive
-- portfolio metadata, so every row belongs to one authenticated user.

CREATE TABLE IF NOT EXISTS wallet_account (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    address_family  TEXT NOT NULL CHECK (address_family IN ('evm', 'solana', 'bitcoin')),
    address         TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('phantom', 'metamask', 'ledger', 'manual')),
    label           TEXT NOT NULL,
    discovered_by   TEXT NOT NULL DEFAULT 'manual',
    balance         JSONB,
    refreshed_at    TIMESTAMPTZ,
    refresh_error   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (length(btrim(address)) BETWEEN 26 AND 128),
    CHECK (length(btrim(label)) BETWEEN 1 AND 80)
);

CREATE UNIQUE INDEX IF NOT EXISTS wallet_account_owner_address_uq
    ON wallet_account (user_id, address_family, lower(address));
CREATE INDEX IF NOT EXISTS wallet_account_user_idx
    ON wallet_account (user_id, created_at);

-- DOWN

DROP TABLE IF EXISTS wallet_account;
