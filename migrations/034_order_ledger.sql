-- The order ledger: the audit trail capital movement is derived from.
--
-- Portfolio state is a projection of this table, not an independent record. So
-- a row missing here is a position that exists in the world and not in the
-- system, which is the failure mode that makes a reconciliation loop unable to
-- tell a venue outage from a lost order.
--
-- idempotency_key is the whole safety property, and the UNIQUE constraint is
-- named so the writer can target it explicitly rather than relying on the
-- index Postgres happens to infer. A retried submission -- the trading loop
-- restarting mid-flight, a duplicated scheduler tick, an operator re-running a
-- command -- must resolve to the SAME order, not a second one. Enforcing that
-- in Python with a SELECT-then-INSERT races; enforcing it here does not.
--
-- order_event is the transition log: every status change with the raw request
-- or response that caused it. trade_order carries only the current state, so
-- without this table a rejection reason or an exchange's acknowledgement
-- payload is overwritten by the next transition and the trail stops at
-- "cancelled, at some point, for some reason".
--
-- venue / symbol / side / market_type / order_kind are TEXT rather than
-- enums: they mirror Python-side value objects in omni.venue.protocol that a
-- new venue adapter extends, and a migration per new order kind would make the
-- schema the thing blocking an adapter. status is an enum because its member
-- set is the state machine itself -- adding one is a deliberate change to the
-- lifecycle, and orders.py asserts the Python and SQL member lists match.

CREATE TYPE order_status AS ENUM (
    'intent',
    'submitted',
    'acknowledged',
    'partially_filled',
    'filled',
    'rejected',
    'cancelled'
);


CREATE TABLE trade_order (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id       UUID NOT NULL,
    idempotency_key    TEXT NOT NULL,

    venue              TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    side               TEXT NOT NULL,
    market_type        TEXT NOT NULL,
    order_kind         TEXT NOT NULL,

    quantity           NUMERIC NOT NULL,
    reference_price    NUMERIC NOT NULL,
    limit_price        NUMERIC,
    stop_price         NUMERIC,
    take_profit_price  NUMERIC,
    expires_at         TIMESTAMPTZ,

    status             order_status NOT NULL DEFAULT 'intent',
    external_id        TEXT,
    filled_quantity    NUMERIC NOT NULL DEFAULT 0,
    average_fill_price NUMERIC,
    fee_paid           NUMERIC NOT NULL DEFAULT 0,

    provenance         JSONB NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT trade_order_idempotency_key_unique UNIQUE (idempotency_key),

    -- Mirrors TradeIntent.__post_init__: direction is carried by side, never
    -- by the sign of the size.
    CONSTRAINT trade_order_quantity_positive CHECK (quantity > 0),
    CONSTRAINT trade_order_reference_price_positive CHECK (reference_price > 0),
    CONSTRAINT trade_order_filled_quantity_not_negative CHECK (filled_quantity >= 0),
    CONSTRAINT trade_order_fee_not_negative CHECK (fee_paid >= 0),

    -- A quantity filled at no recorded price is a fabricated P&L waiting to be
    -- computed. NUMERIC is exact, so the = 0 comparison here means what it says.
    CONSTRAINT trade_order_filled_quantity_has_a_price
        CHECK (filled_quantity = 0 OR average_fill_price IS NOT NULL)
);

-- The reconciliation loop's query: what is still live for this portfolio.
CREATE INDEX trade_order_open_by_portfolio
    ON trade_order (portfolio_id, created_at)
    WHERE status IN ('intent', 'submitted', 'acknowledged', 'partially_filled');

CREATE INDEX trade_order_by_external_id
    ON trade_order (venue, external_id)
    WHERE external_id IS NOT NULL;


CREATE TABLE order_event (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id     UUID NOT NULL REFERENCES trade_order(id) ON DELETE CASCADE,
    status       order_status NOT NULL,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    external_id  TEXT,
    payload      JSONB
);

CREATE INDEX order_event_by_order ON order_event (order_id, at);

-- DOWN

DROP TABLE IF EXISTS order_event;
DROP TABLE IF EXISTS trade_order;
DROP TYPE IF EXISTS order_status;
