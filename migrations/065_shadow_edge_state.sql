-- The edge decay monitor: one row per shadow book per night, append-only.
--
-- The shadow book records decisions and scores them; this table records the
-- judgement those scores support about whether a promoted edge still holds.
-- A judgement that can be revised until it agrees with the operator is not a
-- judgement, so the same refusal triggers as 058 apply: no UPDATE, no DELETE.
-- Re-running the same night is a no-op via the primary key, which is the
-- idempotency shape the rest of the book uses.
--
-- state vocabulary:
--   insufficient  not enough scored outcomes for the recent third to mean
--                 anything; mean and p are NULL and reason says why
--   holding       recent-third mean session excess is positive
--   unconfirmed   excess is non-positive but not significantly negative --
--                 absence of confirmation, not evidence of death
--   decayed       recent-third excess is significantly negative under the
--                 sign-flip null; the promoted claim has reversed
CREATE TABLE shadow_edge_state (
    book                TEXT        NOT NULL,
    as_of               DATE        NOT NULL,
    evaluated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    promoted            BOOLEAN     NOT NULL,
    state               TEXT        NOT NULL,

    scored_sessions     INTEGER     NOT NULL,
    recent_sessions     INTEGER     NOT NULL,
    mean_session_excess NUMERIC,
    decay_p             NUMERIC,
    window_start        DATE,
    window_end          DATE,
    reason              TEXT,

    CONSTRAINT shadow_edge_state_pk PRIMARY KEY (book, as_of),
    CONSTRAINT shadow_edge_state_names_its_book CHECK (book <> ''),
    CONSTRAINT shadow_edge_state_vocabulary
        CHECK (state IN ('insufficient', 'holding', 'unconfirmed', 'decayed')),
    CONSTRAINT shadow_edge_state_counts_are_real
        CHECK (scored_sessions >= 0 AND recent_sessions >= 0),
    CONSTRAINT shadow_edge_state_excess_is_a_number
        CHECK (mean_session_excess IS NULL OR mean_session_excess <> 'NaN'::numeric),
    CONSTRAINT shadow_edge_state_p_is_a_probability
        CHECK (decay_p IS NULL OR (decay_p <> 'NaN'::numeric
                                   AND decay_p > 0 AND decay_p <= 1)),
    CONSTRAINT shadow_edge_state_measured_iff_state_says_so
        CHECK ((state = 'insufficient') = (mean_session_excess IS NULL
                                           AND decay_p IS NULL)),
    CONSTRAINT shadow_edge_state_window_iff_measured
        CHECK ((state = 'insufficient') = (window_start IS NULL
                                           AND window_end IS NULL)),
    CONSTRAINT shadow_edge_state_window_runs_forward
        CHECK (window_start IS NULL OR window_end IS NULL OR window_end >= window_start),
    CONSTRAINT shadow_edge_state_insufficient_names_a_reason
        CHECK (state <> 'insufficient' OR reason IS NOT NULL)
);

CREATE FUNCTION refuse_shadow_edge_rewrite() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'shadow_edge_state is append-only: % on % is refused',
        TG_OP, NEW.book;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER shadow_edge_state_is_append_only
    BEFORE UPDATE OR DELETE ON shadow_edge_state
    FOR EACH ROW EXECUTE FUNCTION refuse_shadow_edge_rewrite();

-- DOWN
DROP TRIGGER IF EXISTS shadow_edge_state_is_append_only ON shadow_edge_state;
DROP FUNCTION IF EXISTS refuse_shadow_edge_rewrite();
DROP TABLE IF EXISTS shadow_edge_state;
