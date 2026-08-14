ALTER TABLE users
    ADD COLUMN role TEXT NOT NULL DEFAULT 'member';

ALTER TABLE users
    ADD CONSTRAINT users_role_check CHECK (role IN ('operator', 'member'));

UPDATE users
SET role = 'operator'
WHERE id = (
    SELECT id
    FROM users
    ORDER BY created_at, id
    LIMIT 1
);

CREATE UNIQUE INDEX users_single_operator
    ON users (role)
    WHERE role = 'operator';

-- DOWN

DROP INDEX IF EXISTS users_single_operator;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users DROP COLUMN IF EXISTS role;
