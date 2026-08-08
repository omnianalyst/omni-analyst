-- Address labels: turn anonymous on-chain flow into attributed flow.
--
-- Replaces the seven hardcoded addresses in onchain.py::KNOWN_EXCHANGES with a
-- queryable store. Binance alone runs hundreds of hot wallets, so any
-- "exchange inflow" signal computed from seven addresses is anecdote, not
-- measurement -- which is what flow.exchange_reserve and onchain.smart_money
-- are blocked on.
--
-- Addresses are stored LOWERCASE because EVM identity is case-insensitive;
-- onchain.py already lowercases its lookup keys once for the same reason.
-- Normalisation lives in labels.py::normalise_address, the single function both
-- write and read call, so they cannot disagree about casing.
--
-- UNIQUE is (chain, address, source), not (chain, address): two sources may
-- legitimately label one address differently and both rows persist. The hot
-- lookup is per-transaction, hence the (chain, address) index; lookup() picks
-- the highest-confidence row, ties broken by source ascending (see labels.py).
--
-- confidence CHECK is (0, 1]: 1.0 is reserved for an address sourced from the
-- operator itself or a published label set; anything inferred is below 1.0 and
-- says so in `source`. 0 is excluded -- a zero-confidence label is no label.

CREATE TABLE address_label (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chain        TEXT NOT NULL,
    address      TEXT NOT NULL,
    label        TEXT NOT NULL,
    category     TEXT NOT NULL,
    entity_name  TEXT,
    source       TEXT NOT NULL,
    confidence   DOUBLE PRECISION NOT NULL CHECK (confidence > 0 AND confidence <= 1),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chain, address, source)
);

CREATE INDEX address_label_chain_address_idx ON address_label (chain, address);
