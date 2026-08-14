-- The forward shadow book: allocation decisions recorded before they apply.
--
-- Every allocation result this project has produced is a backtest, and
-- `docs/ETF_PORTFOLIO_EXPERIMENT.md` says plainly why its own numbers are not
-- decision-grade: today's membership applied backward, eighteen post-warmup
-- months, a mean carried by one sector. The fix is not a better backtest. The
-- fix is a record of decisions made *before* their outcome existed, which can
-- only be started and never backfilled -- the same property that made dated
-- index membership (056) worth adding on a day nobody needed it.
--
-- **The immutability is the point.** A shadow book that can be edited is a
-- backtest wearing a costume: every revision improves the record, each one
-- looks locally justified, and the result is indistinguishable from a strategy
-- that was always right. So it is enforced by a trigger rather than by
-- convention. A rule that lives in a docstring is a rule until the first agent
-- in a hurry, and this project has that failure written into five separate
-- modules for the float-equality rule alone.
--
-- **`effective_from` must be strictly after the day the decision was written.**
-- That is the point-in-time rule from AGENTS.md -- a score at t executes no
-- earlier than the following session -- expressed where it cannot be forgotten.
-- Without it, a row written after the close it claims to precede is a perfect
-- forecast, and nothing downstream could tell it apart from a real one.
--
-- Outcomes live in a separate table for the reason the prediction ledger keeps
-- scoring as a separate pass: the writer must never be able to set them. If the
-- decision row carried its own realised return, the process that computes the
-- return would need UPDATE on the decision, and the immutability above would
-- have to be relaxed to allow it.

CREATE TABLE shadow_decision (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Which book. One rule's decisions over time; comparing two rules means
    -- comparing two books, never re-labelling rows inside one.
    book           TEXT        NOT NULL,

    -- The rule that produced the weights, versioned. A rule that changes gets a
    -- new version rather than a rewritten history: the old decisions were still
    -- taken, and deleting them is how a strategy acquires a flawless record.
    rule_version   TEXT        NOT NULL,

    decided_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The first session the weights apply to. A DATE rather than a timestamp
    -- because the decision is "hold this from this session onward", and a
    -- clock time would imply an intraday execution this book does not model.
    effective_from DATE        NOT NULL,

    -- What the rule chose from, so a later reader can tell a name that scored
    -- badly from a name that was never a candidate. Those are different facts
    -- and only one of them is evidence about the rule.
    universe       TEXT[]      NOT NULL,

    -- The measurements the rule actually saw. Without these, a later reader
    -- cannot distinguish a rule that changed from a market that changed.
    inputs         JSONB       NOT NULL,

    -- symbol -> target weight. Cash is the residual and is not a key: an
    -- explicit cash weight that disagreed with 1 - sum(weights) would give two
    -- answers to what the book holds.
    weights        JSONB       NOT NULL,

    -- The cost assumption charged against turnover into these weights, stated
    -- at decision time. Recorded rather than applied later, because a cost
    -- chosen after the outcome is known is the cheapest way to make a losing
    -- rule pass.
    cost_bps       NUMERIC     NOT NULL,

    -- What this book is measured against. A shadow book with no declared
    -- baseline is a return with nothing to beat, and every return looks good
    -- against nothing.
    benchmark      TEXT        NOT NULL,

    note           TEXT,

    -- One decision per book per effective session. A second would mean two
    -- different sets of weights claiming the same session, and any reader
    -- picking between them -- newest, highest, first -- would be choosing the
    -- record after the fact.
    CONSTRAINT shadow_decision_one_per_session UNIQUE (book, effective_from),

    CONSTRAINT shadow_decision_names_its_book      CHECK (book <> ''),
    CONSTRAINT shadow_decision_names_its_rule      CHECK (rule_version <> ''),
    CONSTRAINT shadow_decision_names_its_benchmark CHECK (benchmark <> ''),

    -- The whole point, stated where it cannot be forgotten. Cast in UTC
    -- explicitly: `decided_at::date` would depend on the session TimeZone, so
    -- the same row could satisfy this check on one connection and violate it on
    -- another.
    CONSTRAINT shadow_decision_precedes_the_session_it_applies_to
        CHECK (effective_from > (decided_at AT TIME ZONE 'UTC')::date),

    CONSTRAINT shadow_decision_universe_is_not_empty
        CHECK (cardinality(universe) > 0),

    CONSTRAINT shadow_decision_weights_are_an_object
        CHECK (jsonb_typeof(weights) = 'object' AND weights <> '{}'::jsonb),

    CONSTRAINT shadow_decision_inputs_are_an_object
        CHECK (jsonb_typeof(inputs) = 'object'),

    -- NUMERIC accepts 'NaN' and sorts it above every number, so a NaN cost
    -- would pass any range check written against this column and then poison
    -- every net figure taken over the book.
    CONSTRAINT shadow_decision_cost_is_a_number
        CHECK (cost_bps >= 0 AND cost_bps <> 'NaN'::numeric)
);

CREATE INDEX shadow_decision_by_book
    ON shadow_decision (book, effective_from DESC);


-- Scored separately, and only from the decision's own weights. `realised` is
-- what the recorded weights returned over the period; `benchmark_return` is
-- what the declared baseline returned over the same sessions. Both net of the
-- cost recorded on the decision, because a gross comparison flatters whichever
-- side trades more -- which is always the active one.
CREATE TABLE shadow_outcome (
    decision_id      UUID PRIMARY KEY
                         REFERENCES shadow_decision(id) ON DELETE CASCADE,
    scored_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The window actually measured. A decision scored over a shorter window
    -- than it was held is a partial result, and it must be visible as one.
    period_start     DATE        NOT NULL,
    period_end       DATE        NOT NULL,
    sessions         INTEGER     NOT NULL,

    realised_return  NUMERIC     NOT NULL,
    benchmark_return NUMERIC     NOT NULL,
    cost_charged     NUMERIC     NOT NULL,
    turnover         NUMERIC     NOT NULL,

    -- Named rather than derived so an incomplete price panel is a stated
    -- limitation on the row instead of a silently shorter window.
    limits           JSONB       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT shadow_outcome_period_runs_forward
        CHECK (period_end >= period_start),
    CONSTRAINT shadow_outcome_sessions_are_counted
        CHECK (sessions > 0),
    CONSTRAINT shadow_outcome_amounts_are_numbers
        CHECK (realised_return  <> 'NaN'::numeric
           AND benchmark_return <> 'NaN'::numeric
           AND cost_charged     <> 'NaN'::numeric
           AND turnover         <> 'NaN'::numeric),
    CONSTRAINT shadow_outcome_turnover_is_not_negative
        CHECK (turnover >= 0 AND cost_charged >= 0)
);


-- Immutability, enforced rather than documented.
--
-- Both tables are append-only. A decision cannot be revised because revising it
-- is the failure mode; an outcome cannot be revised because a score that can be
-- recomputed until it is favourable is not a score. Re-scoring an existing
-- decision means deleting nothing and inserting nothing -- it means the earlier
-- score was already the answer.
--
-- DELETE is refused alongside UPDATE. Delete-then-insert is an update with an
-- extra step, and it is the shape an agent reaches for first when INSERT
-- conflicts on the unique key.
--
-- TRUNCATE is deliberately NOT blocked, and the omission is a decision rather
-- than an oversight. Row triggers do not see it, so blocking it needs a
-- statement trigger -- and that would leave the test suite unable to reset
-- these tables between cases, which is a real cost paid against no real
-- protection. The failure mode this guards is *selective* revision: one
-- decision improved after its outcome arrived, invisible among the honest ones.
-- Emptying the whole book is not that; it destroys the record rather than
-- flattering it, no application path performs it, and it cannot be mistaken for
-- an intact history.
CREATE FUNCTION refuse_shadow_book_rewrite() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'the shadow book is append-only: % on % is refused. A decision or a '
        'score that can be revised after its outcome is known is a backtest, '
        'not a forward record. Insert a new decision instead.',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER shadow_decision_is_append_only
    BEFORE UPDATE OR DELETE ON shadow_decision
    FOR EACH ROW EXECUTE FUNCTION refuse_shadow_book_rewrite();

CREATE TRIGGER shadow_outcome_is_append_only
    BEFORE UPDATE OR DELETE ON shadow_outcome
    FOR EACH ROW EXECUTE FUNCTION refuse_shadow_book_rewrite();

-- DOWN

DROP TRIGGER IF EXISTS shadow_outcome_is_append_only  ON shadow_outcome;
DROP TRIGGER IF EXISTS shadow_decision_is_append_only ON shadow_decision;
DROP FUNCTION IF EXISTS refuse_shadow_book_rewrite();
DROP TABLE IF EXISTS shadow_outcome;
DROP TABLE IF EXISTS shadow_decision;
