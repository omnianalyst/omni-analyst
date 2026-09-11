-- Cross-user reference containment for the finance domain (audit B10).
--
-- The single-column FKs (finance_schedule.account_id -> finance_account(id),
-- finance_transaction.category_id -> finance_category(id)) prove existence,
-- not ownership: a caller holding a foreign UUID could link to another user's
-- account or category. The Python layer now checks ownership on every write,
-- and these composite FKs make the database enforce the same boundary for
-- any path that misses a check.
--
-- The FKs carry no ON DELETE action: the composite form cannot null one
-- column of the pair, and no path here hard-deletes finance_account or
-- finance_category rows (both are soft state: closed / tombstone). A future
-- hard delete must decide explicitly what happens to referencing rows.

CREATE UNIQUE INDEX IF NOT EXISTS finance_account_user_id_uq
    ON finance_account (user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS finance_category_user_id_uq
    ON finance_category (user_id, id);

ALTER TABLE finance_schedule
    DROP CONSTRAINT IF EXISTS finance_schedule_user_account_fk;
ALTER TABLE finance_schedule
    ADD CONSTRAINT finance_schedule_user_account_fk
    FOREIGN KEY (user_id, account_id)
    REFERENCES finance_account (user_id, id)
    NOT VALID;

ALTER TABLE finance_transaction
    DROP CONSTRAINT IF EXISTS finance_transaction_user_category_fk;
ALTER TABLE finance_transaction
    ADD CONSTRAINT finance_transaction_user_category_fk
    FOREIGN KEY (user_id, category_id)
    REFERENCES finance_category (user_id, id)
    NOT VALID;

DO $$
BEGIN
    -- Rows where account_id/category_id is NULL cannot violate the composite
    -- FK; the scan looks only at rows that carry both sides.
    IF EXISTS (
        SELECT 1 FROM finance_schedule s
        LEFT JOIN finance_account a ON (a.user_id, a.id) = (s.user_id, s.account_id)
        WHERE s.account_id IS NOT NULL AND a.id IS NULL
    ) OR EXISTS (
        SELECT 1 FROM finance_transaction t
        LEFT JOIN finance_category c ON (c.user_id, c.id) = (t.user_id, t.category_id)
        WHERE t.category_id IS NOT NULL AND c.id IS NULL
    ) THEN
        RAISE EXCEPTION 'finance rows crossing user boundaries exist; resolve them before validating finance_schedule_user_account_fk / finance_transaction_user_category_fk';
    END IF;
END $$;

ALTER TABLE finance_schedule VALIDATE CONSTRAINT finance_schedule_user_account_fk;
ALTER TABLE finance_transaction VALIDATE CONSTRAINT finance_transaction_user_category_fk;
