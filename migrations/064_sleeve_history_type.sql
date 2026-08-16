-- Sleeve history series: long monthly price/level series (LBMA gold, 3-month
-- T-bill, 10-year yield) for descriptive disaster context. Distinct from
-- macro_series_point so a current-value series can never be mistaken for a
-- point-in-time vintage print by a backtest.

ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'sleeve_history_point';

-- DOWN
-- ALTER TYPE ... DROP VALUE has no Postgres support; removal requires a type
-- rebuild. The value is inert without rows, so the down path leaves it.
