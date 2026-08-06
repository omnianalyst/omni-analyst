"""User identity: create, authenticate, fetch.

Passwords never touch this module in cleartext storage form -- they go straight
to ``neutron.auth.password.hash_password`` and are verified with
``verify_password``. No crypto is written here; the framework's auth tier is the
whole toolkit, by order of the work order and of not reinventing argon2.

The credential-failure rule: an unknown email and a wrong password are
indistinguishable to the caller. ``authenticate_user`` returns ``None`` for
both, and the API layer renders the identical response from that ``None``.
Revealing that an email exists is an account-enumeration vector, and on a
system where data access is licensed per user it is worse than usual.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from neutron.auth.password import hash_password, verify_password
from neutron.error import conflict

MIN_PASSWORD_LENGTH = 12


class PasswordTooShort(Exception):
    """Raised when a password is shorter than MIN_PASSWORD_LENGTH."""


def _normalise_email(email: str) -> str:
    return email.strip().lower()


async def create_user(pool: asyncpg.Pool, *, email: str, password: str) -> Any:
    """Insert a new active user. Email is canonicalised to lower case.

    Raises PasswordTooShort if the password is below the minimum length, or a
    409 conflict if the email (case-insensitively) is already registered.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordTooShort
    canonical = _normalise_email(email)
    try:
        return await pool.fetchrow(
            """
            INSERT INTO users (email, password_hash)
            VALUES ($1, $2)
            RETURNING id, email, created_at, active
            """,
            canonical,
            hash_password(password),
        )
    except asyncpg.UniqueViolationError:
        raise conflict("email already registered")


async def authenticate_user(
    pool: asyncpg.Pool, *, email: str, password: str
) -> Any | None:
    """Return the user record on valid credentials, else ``None``.

    ``None`` covers unknown email, wrong password, and inactive user. The three
    are deliberately the same return: the caller cannot tell them apart, which
    is the point -- see the module docstring on enumeration.
    """
    canonical = _normalise_email(email)
    row = await pool.fetchrow(
        "SELECT id, email, password_hash, created_at, active "
        "FROM users WHERE lower(email) = $1",
        canonical,
    )
    if row is None:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    if not row["active"]:
        return None
    return row


async def get_user(pool: asyncpg.Pool, user_id: UUID) -> Any | None:
    return await pool.fetchrow(
        "SELECT id, email, created_at, active FROM users WHERE id = $1",
        user_id,
    )
