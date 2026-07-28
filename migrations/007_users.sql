-- Users: identity for the audience model.
--
-- This is identity, not a profile. The redistribution guarantee rests on
-- knowing who is asking: a claim fetched under a byo_only credential is visible
-- only to its owner, so the owner must be a real, authenticated principal and
-- not a value read from an unauthenticated header.
--
-- Email uniqueness is case-insensitive, enforced at the DB with an expression
-- index on lower(email) rather than a plain UNIQUE constraint. The application
-- canonicalises email to lower case on write and lookup; the index is the
-- guarantee that case-folding cannot be forgotten by a future writer.

CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    active        BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE UNIQUE INDEX users_email_lower_unique ON users (lower(email));

-- DOWN

DROP TABLE IF EXISTS users;
