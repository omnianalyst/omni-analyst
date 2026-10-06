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

That rule is enforced structurally, not by wording: authentication verifies
a hash for EVERY login, known user or not (against the dummy hash below), so
the code path -- and its argon2 cost -- is the same either way. Identical
wording with a missing-hash shortcut would leak the account's existence in
the response time instead.
"""

from __future__ import annotations

import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from typing import Any
from uuid import UUID

import asyncpg
from neutron.auth.password import hash_password, verify_password
from neutron.error import AppError, conflict

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_SUBMISSION_LENGTH = 1024

# Argon2 is deliberately expensive, and it runs inside async request paths.
# Called inline it stalls that worker's event loop for tens to hundreds of
# milliseconds on login, setup and password change, delaying every concurrent
# request. One worker: hashing is also serialised, which blunts parallel CPU
# abuse through the same endpoints.
_password_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="omni-pw")

# The executor bounds RUNNING jobs, not QUEUED ones (audit A02): with a bare
# submit, a session that can reach a password endpoint can enqueue unlimited
# hashing work and every later login waits behind it. One slot for the
# running job plus three queued; beyond that the endpoint refuses promptly
# with 503 instead of accepting work it cannot reach.
_PASSWORD_SLOTS = 4
_password_slots = BoundedSemaphore(_PASSWORD_SLOTS)


async def _password_call(func, *args):
    if not _password_slots.acquire(blocking=False):
        raise AppError(
            503,
            "password-busy",
            "Service Unavailable",
            "Password service busy; retry later.",
        )
    try:
        future = _password_pool.submit(func, *args)
    except BaseException:
        _password_slots.release()
        raise
    # Release when the job itself finishes, not when the HTTP task awaiting
    # it is cancelled: a cancelled waiter cannot un-run argon2, and freeing
    # the slot early would let the same request flood the queue again.
    future.add_done_callback(lambda _f: _password_slots.release())
    return await asyncio.wrap_future(future)


class PasswordTooShort(Exception):
    """Raised when a password is shorter than MIN_PASSWORD_LENGTH."""


class CredentialChangedConcurrently(Exception):
    """The credential row moved between verification and update (A03).

    A password change must bind its UPDATE to the exact hash and auth
    version it verified. Without that, two requests can both verify the same
    old password and the later write silently overwrites the earlier new
    password -- a concurrent rotation undone by a stale request.
    """


_dummy_hash: str | None = None


async def prime_password_hash() -> str:
    """Hash one random secret at startup; the hash primes the dummy path.

    Generated once per process, never per request: an unknown-email login
    verifies the submitted password against this hash, so both outcomes of
    the user lookup pay the same argon2 cost. This hides the missing-hash
    branch; it does not make response times mathematically identical, and
    does not claim to.
    """
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = await _password_call(
            hash_password, secrets.token_urlsafe(24)
        )
    return _dummy_hash


def _normalise_email(email: str) -> str:
    return email.strip().lower()


def _check_new_password(new_password: str) -> None:
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise PasswordTooShort
    if (
        len(new_password) > MAX_PASSWORD_SUBMISSION_LENGTH
    ):
        raise ValueError("password exceeds supported length")


async def create_user(pool: asyncpg.Pool, *, email: str, password: str) -> Any:
    """Insert a new active user. Email is canonicalised to lower case.

    Raises PasswordTooShort if the password is below the minimum length, or a
    409 conflict if the email (case-insensitively) is already registered.
    """
    _check_new_password(password)
    canonical = _normalise_email(email)
    try:
        return await pool.fetchrow(
            """
            INSERT INTO users (email, password_hash)
            VALUES ($1, $2)
            RETURNING id, email, created_at, active, role, auth_version
            """,
            canonical,
            await _password_call(hash_password, password),
        )
    except asyncpg.UniqueViolationError:
        raise conflict("email already registered")


async def create_initial_operator(
    pool: asyncpg.Pool, *, email: str, password: str
) -> Any:
    _check_new_password(password)
    canonical = _normalise_email(email)
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO users (email, password_hash, role)
            SELECT $1, $2, 'operator'
            WHERE NOT EXISTS (SELECT 1 FROM users)
            RETURNING id, email, created_at, active, role, auth_version
            """,
            canonical,
            await _password_call(hash_password, password),
        )
    except asyncpg.UniqueViolationError:
        raise conflict("setup is already complete")
    if row is None:
        raise conflict("setup is already complete")
    return row


async def authenticate_user(
    pool: asyncpg.Pool, *, email: str, password: str
) -> Any | None:
    """Return the user record on valid credentials, else ``None``.

    ``None`` covers unknown email, wrong password, and inactive user. The three
    are deliberately the same return: the caller cannot tell them apart, which
    is the point -- see the module docstring on enumeration. The password is
    verified against a real hash in EVERY case (the user's, or the process's
    dummy hash when the user does not exist), so the argon2 work is paid
    whether or not the account exists.
    """
    global _dummy_hash
    canonical = _normalise_email(email)
    row = await pool.fetchrow(
        "SELECT id, email, password_hash, created_at, active, role, auth_version "
        "FROM users WHERE lower(email) = $1",
        canonical,
    )
    if row is not None:
        target = row["password_hash"]
    else:
        if _dummy_hash is None:
            # Lifespan primes this at startup; the lazy path covers callers
            # that never ran one (tests, one-shot scripts). One hash per
            # process lifetime, never per request.
            _dummy_hash = await _password_call(
                hash_password, secrets.token_urlsafe(24)
            )
        target = _dummy_hash
    verified = await _password_call(verify_password, password, target)
    if row is None or not verified or not row["active"]:
        return None
    return row


async def get_user(pool: asyncpg.Pool, user_id: UUID) -> Any | None:
    return await pool.fetchrow(
        "SELECT id, email, created_at, active, role FROM users WHERE id = $1",
        user_id,
    )


async def change_password(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    old_password: str,
    new_password: str,
    expected_auth_version: int,
) -> bool:
    """Rotate a user's password after verifying the current one (A03).

    Compare-and-swap: verification reads the exact ``password_hash`` and
    ``auth_version``, and the UPDATE succeeds only if that row is unchanged
    and still active. Anything else -- a concurrent password change, a
    logout-all, a deactivation between the read and the write -- raises
    CredentialChangedConcurrently rather than overwriting the newer state.

    Returns True on success, False when the old password does not match (the
    caller renders the same response as a wrong login -- no enumeration).
    Raises PasswordTooShort before touching the row if the new password is
    weak, and ValueError for lengths the hasher must not be given.

    The auth_version increment rides the same UPDATE: every bearer token
    issued before this moment carries the old epoch and dies with it, so a
    stolen token does not outlive the password rotation that displaced it.
    """
    if len(old_password) > MAX_PASSWORD_SUBMISSION_LENGTH:
        raise ValueError("password exceeds supported length")
    _check_new_password(new_password)
    row = await pool.fetchrow(
        "SELECT password_hash, auth_version FROM users "
        "WHERE id = $1 AND active",
        user_id,
    )
    if row is None or row["auth_version"] != expected_auth_version:
        raise CredentialChangedConcurrently
    if not await _password_call(
        verify_password, old_password, row["password_hash"]
    ):
        return False
    updated = await pool.fetchval(
        "UPDATE users SET password_hash = $1, auth_version = auth_version + 1 "
        "WHERE id = $2 AND active AND auth_version = $3 "
        "AND password_hash = $4 RETURNING auth_version",
        await _password_call(hash_password, new_password),
        user_id,
        row["auth_version"],
        row["password_hash"],
    )
    if updated is None:
        raise CredentialChangedConcurrently
    return True


async def bump_auth_version(pool: asyncpg.Pool, user_id: UUID) -> int:
    """Invalidate every bearer token issued to the user so far.

    The revocation half of logout-all-sessions: the increment moves the
    user's epoch past whatever ver claim existing tokens carry, without
    touching the password.
    """
    return await pool.fetchval(
        "UPDATE users SET auth_version = auth_version + 1 WHERE id = $1 "
        "RETURNING auth_version",
        user_id,
    )


async def user_count(pool: asyncpg.Pool) -> int:
    """Number of registered users. Drives the first-run setup gate: the
    ``/auth/setup`` endpoint is the only way to create the first user, and
    it refuses once any user exists, so an open deployment cannot be claimed
    by a stranger."""
    return await pool.fetchval("SELECT count(*) FROM users")
