-- Manual holdings: the user's personal position tracker.
--
-- Deliberately its own table and NOT a position row on a trading book. A
-- `position` row asserts "the ledger replayed from recorded fills produces
-- exactly this" -- that is the invariant realised_pnl, reconciliation and
-- the carry guards all stand on, and a hand-typed entry forced through it
-- would be a fabrication wearing the ledger's authority. A manual holding is
-- a different statement: "I own this, track it for me." Its price comes from
-- the audience-scoped claim store, never from the user, and a symbol the
-- store has no entity for is refused rather than tracked against air.
--
-- Unlike the evidence tables (fills, predictions, findings) this is the
-- user's own notebook: rows may be edited and deleted. The append-only
-- discipline applies to records that are evidence; a personal list the
-- system could not correct would be a liability, not an audit trail.
--
-- cost_basis is OPTIONAL and is the total amount paid for the whole
-- quantity, not a unit price -- what a person actually knows offhand. NULL
-- means "track value only": an absent basis must never render as zero P&L,
-- the same way an absent price must never render as a zero value.
CREATE TABLE manual_holding (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES users(id),
    symbol      TEXT        NOT NULL,
    quantity    NUMERIC     NOT NULL,
    cost_basis  NUMERIC,
    currency    TEXT        NOT NULL DEFAULT 'USD',
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT manual_holding_names_its_symbol  CHECK (symbol <> ''),
    CONSTRAINT manual_holding_quantity_is_real  CHECK (quantity <> 'NaN'::numeric
                                                       AND quantity > 0),
    CONSTRAINT manual_holding_basis_is_sane     CHECK (
        cost_basis IS NULL
        OR (cost_basis <> 'NaN'::numeric AND cost_basis >= 0)
    ),
    CONSTRAINT manual_holding_one_row_per_symbol
        UNIQUE (user_id, symbol, currency)
);

-- DOWN
DROP TABLE IF EXISTS manual_holding;
