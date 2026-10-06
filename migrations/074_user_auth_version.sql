-- Token revocation: users.auth_version (audit finding 3).
--
-- Tokens carry only `sub` + expiry, so a password change leaves every
-- previously issued bearer token valid until it expires -- up to seven days
-- under the documented longer lifetime. A stolen token therefore survives the
-- operator discovering the theft and rotating the password.
--
-- auth_version is the revocation epoch. It is embedded in issued tokens as the
-- `ver` claim and re-checked against this column by ActivePrincipalMiddleware:
-- a token whose ver disagrees with the row is a pre-rotation token and resolves
-- to anonymous. Password change and logout-all increment it transactionally
-- with the write that motivates the revocation, so the row and the epoch can
-- never disagree.
--
-- Existing tokens carry no ver claim; they read as ver 0, which is also the
-- column default, so the migration deploys without invalidating every live
-- session. The first password change on a deployment moves the row to 1 and
-- retires all claim-less tokens at once.

ALTER TABLE users
    ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 0;

-- DOWN
-- ALTER TABLE users DROP COLUMN auth_version;
