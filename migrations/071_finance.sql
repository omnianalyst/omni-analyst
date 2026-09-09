-- Personal finance domain: envelope budgeting with import reconciliation.
--
-- Every row belongs to one authenticated user. Bank-sourced data is BYO by
-- construction: user_id is the access-control key, the shared claim network
-- never sees these tables. Amounts are integer cents everywhere -- floats
-- never enter a ledger.

CREATE TABLE IF NOT EXISTS finance_account (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL CHECK (type IN ('checking','savings','credit','loan','cash','investment','other')),
    currency     CHAR(3) NOT NULL DEFAULT 'USD',
    offbudget    BOOLEAN NOT NULL DEFAULT false,
    closed       BOOLEAN NOT NULL DEFAULT false,
    balance_current BIGINT,
    balance_as_of   DATE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS finance_category (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    is_income  BOOLEAN NOT NULL DEFAULT false,
    tombstone  BOOLEAN NOT NULL DEFAULT false,
    sort_order BIGINT NOT NULL DEFAULT 0,
    goal_type  TEXT CHECK (goal_type IN ('monthly','save_by_date')),
    goal_amount BIGINT,
    goal_date  DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS finance_category_owner_name_uq
    ON finance_category (user_id, lower(name)) WHERE NOT tombstone;

CREATE TABLE IF NOT EXISTS finance_payee (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    transfer_acct UUID REFERENCES finance_account(id) ON DELETE SET NULL,
    tombstone     BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS finance_payee_owner_name_uq
    ON finance_payee (user_id, lower(name)) WHERE NOT tombstone;

CREATE TABLE IF NOT EXISTS finance_schedule (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    payee_id   UUID REFERENCES finance_payee(id) ON DELETE SET NULL,
    account_id UUID REFERENCES finance_account(id) ON DELETE SET NULL,
    amount     BIGINT,
    config     JSONB NOT NULL,
    active     BOOLEAN NOT NULL DEFAULT true,
    notes      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS finance_transaction (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id     UUID NOT NULL REFERENCES finance_account(id) ON DELETE CASCADE,
    payee_id       UUID REFERENCES finance_payee(id) ON DELETE SET NULL,
    category_id    UUID REFERENCES finance_category(id) ON DELETE SET NULL,
    date           DATE NOT NULL,
    amount         BIGINT NOT NULL CHECK (amount <> 0),
    notes          TEXT,
    cleared        BOOLEAN NOT NULL DEFAULT true,
    reconciled     BOOLEAN NOT NULL DEFAULT false,
    imported_id    TEXT,
    imported_payee TEXT,
    transfer_id    UUID REFERENCES finance_transaction(id) ON DELETE SET NULL,
    parent_id      UUID REFERENCES finance_transaction(id) ON DELETE CASCADE,
    is_parent      BOOLEAN NOT NULL DEFAULT false,
    schedule_id    UUID REFERENCES finance_schedule(id) ON DELETE SET NULL,
    sort_order     BIGINT NOT NULL DEFAULT 0,
    raw_import     JSONB,
    deleted        BOOLEAN NOT NULL DEFAULT false,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- An imported_id is unique per account among live rows. Dead rows keep
-- theirs so re-import-after-delete is detectable rather than silently
-- duplicating.
CREATE UNIQUE INDEX IF NOT EXISTS finance_transaction_imported_uq
    ON finance_transaction (account_id, imported_id)
    WHERE imported_id IS NOT NULL AND NOT deleted;

CREATE INDEX IF NOT EXISTS finance_transaction_owner_date_idx
    ON finance_transaction (user_id, date DESC);
CREATE INDEX IF NOT EXISTS finance_transaction_account_date_idx
    ON finance_transaction (account_id, date);
CREATE INDEX IF NOT EXISTS finance_transaction_category_idx
    ON finance_transaction (user_id, category_id, date);

CREATE TABLE IF NOT EXISTS finance_rule (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rank       BIGINT NOT NULL,
    conditions JSONB NOT NULL,
    actions    JSONB NOT NULL,
    enabled    BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS finance_rule_owner_rank_idx ON finance_rule (user_id, rank);

-- month is always the first day of its month; amount is cents budgeted.
-- rollover_mode follows Actual's budget semantics: 'rollover' carries
-- unspent forward, 'reset' zeroes it, 'hold' freezes the carry at its
-- entry value (new leftover neither adds nor is discarded).
CREATE TABLE IF NOT EXISTS finance_budget (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month          DATE NOT NULL,
    category_id    UUID NOT NULL REFERENCES finance_category(id) ON DELETE CASCADE,
    amount         BIGINT NOT NULL DEFAULT 0,
    rollover_mode  TEXT NOT NULL DEFAULT 'rollover'
                   CHECK (rollover_mode IN ('rollover','reset','hold')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT finance_budget_month_first_day CHECK (month = date_trunc('month', month)::date),
    UNIQUE (user_id, month, category_id)
);

CREATE TABLE IF NOT EXISTS finance_import (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id  UUID NOT NULL REFERENCES finance_account(id) ON DELETE CASCADE,
    format      TEXT NOT NULL,
    filename    TEXT,
    added       INTEGER NOT NULL,
    updated     INTEGER NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Bank links. The requisition row is the goCardless handshake (link URL ->
-- bank approval -> provider account ids); finance_bank_account maps one
-- provider account to one local account. Credentials live encrypted in
-- user_settings under finance_bank_keys, never here.

CREATE TABLE IF NOT EXISTS finance_bank_link (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL CHECK (provider IN ('gocardless','simplefin')),
    institution_id  TEXT,
    institution_name TEXT,
    requisition_id  TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, requisition_id)
);

CREATE TABLE IF NOT EXISTS finance_bank_account (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bank_link_id        UUID NOT NULL REFERENCES finance_bank_link(id) ON DELETE CASCADE,
    account_id          UUID NOT NULL REFERENCES finance_account(id) ON DELETE CASCADE,
    provider            TEXT NOT NULL CHECK (provider IN ('gocardless','simplefin')),
    provider_account_id TEXT NOT NULL,
    field_mapping       JSONB,
    last_synced_at      TIMESTAMPTZ,
    sync_error          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, provider, provider_account_id)
);

-- DOWN

DROP TABLE IF EXISTS finance_bank_account;
DROP TABLE IF EXISTS finance_bank_link;
DROP TABLE IF EXISTS finance_schedule;
DROP TABLE IF EXISTS finance_import;
DROP TABLE IF EXISTS finance_budget;
DROP TABLE IF EXISTS finance_rule;
DROP TABLE IF EXISTS finance_transaction;
DROP TABLE IF EXISTS finance_payee;
DROP TABLE IF EXISTS finance_category;
DROP TABLE IF EXISTS finance_account;
