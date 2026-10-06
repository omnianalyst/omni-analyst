"""Venue manager: connects enabled venues from Settings.

Reads the operator's venue configuration from user_settings, instantiates the
appropriate Venue adapter for each enabled venue, and holds them for the
portfolio API to query.

This is the bridge between the Settings page toggle and actual connections:
toggle ON in Settings -> this module connects on the next reconcile -> data
appears in the UI.

Two boundaries this module must not blur:

- **Credentials are encrypted at rest.** Secrets arrive from `user_settings` as
  ciphertext and are decrypted here, in the process that needs them, and never
  returned to a caller. See `omni.credentials.keyring` for what that does and
  does not protect against.
- **Connecting is not trading.** A connected venue can be read. Placing an
  order goes through the trading tier's own gates, and nothing here may widen
  them -- a Settings toggle that enabled live orders by implication would be
  the most dangerous bug this file could carry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from omni.credentials.keyring import decrypt_fields
from omni.scheduler.health import record_loop_health

logger = logging.getLogger("omni.venue.manager")

#: How long a refresh waits for the cross-process rotation lock before
#: refusing honestly. Long enough to queue behind one ordinary connect
#: (Questrade's httpx timeout is 30s); short enough that a settings poll
#: cannot hang an API request indefinitely.
ROTATION_LOCK_TIMEOUT = 15.0
_ROTATION_LOCK_RETRY = 0.25


def _rotation_lock_key(user_id: UUID) -> int:
    return int.from_bytes(
        hashlib.sha256(f"omni:venue:rotation:{user_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )


class RefreshBusy(Exception):
    """Another process holds this user's venue rotation lock."""


@asynccontextmanager
async def _rotation_lock(pool, user_id: UUID) -> AsyncIterator[None]:
    """Serialize venue token rotation across PROCESSES (audit A14).

    The in-process ``_user_lock`` below serializes refreshes inside one
    Python process -- but the scheduler reconciles venues while the API's
    settings endpoints refresh the same user, and Questrade refresh tokens
    are single-use: two processes connecting with the same stored token
    race, the loser's rotation invalidates the winner's, and the stored
    credential can end up consumed-but-persisted. A PostgreSQL session
    advisory lock on a dedicated connection is the cross-process arbiter;
    it releases itself if the connection dies, exactly like the work it
    guards.
    """
    key = _rotation_lock_key(user_id)
    conn = await pool.acquire()
    try:
        deadline = time.monotonic() + ROTATION_LOCK_TIMEOUT
        while True:
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", key)
            if got:
                break
            if time.monotonic() >= deadline:
                raise RefreshBusy(
                    "another venue refresh holds the rotation lock"
                )
            await asyncio.sleep(_ROTATION_LOCK_RETRY)
        try:
            yield
        finally:
            import contextlib

            healthy = True
            try:
                await conn.execute("SELECT pg_advisory_unlock($1)", key)
            except Exception:  # noqa: BLE001 - any failure means the lease connection is dead
                # The connection died mid-refresh. Pool-releasing it would
                # ask for a reset on a socket that will never answer and
                # hang the caller; terminate is synchronous and ends both
                # the session and its lock.
                healthy = False
            with contextlib.suppress(Exception):
                if not healthy:
                    conn.terminate()
    finally:
        await pool.release(conn)


# Which fields of each venue's credential blob are secret. Anything named here
# is stored encrypted and decrypted only at connect time. A field absent from
# this map is stored in the clear, so adding a venue means deciding this
# explicitly rather than inheriting a default.
SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "questrade": ("refresh_token",),
    # IBKR is not connectable, but any credentials already written under the
    # legacy Settings path must keep decrypting; the declaration covers storage,
    # not availability.
    "ibkr": ("username", "password"),
    "hyperliquid": ("api_secret", "private_key", "agent_key"),
}
CONNECTABLE_VENUES = frozenset({"questrade"})

_venues: dict[UUID, dict[str, Any]] = {}
_locks: dict[UUID, asyncio.Lock] = {}
# Reference count per user for the keyed lock above. An entry in _locks used
# to be created by any refresh/disconnect and removed only by disconnect_user,
# so a user who configured and then removed a venue (or never got one
# connected) leaked a lock object forever. The count reaches zero exactly
# when no reconciliation is in flight, and the lock is dropped then -- unless
# a venue is live, which keeps the user's state (and lock) resident on
# purpose.
_lock_refs: dict[UUID, int] = {}


@asynccontextmanager
async def _user_lock(user_id: UUID) -> AsyncIterator[asyncio.Lock]:
    lock = _locks.setdefault(user_id, asyncio.Lock())
    _lock_refs[user_id] = _lock_refs.get(user_id, 0) + 1
    try:
        async with lock:
            yield lock
    finally:
        remaining = _lock_refs.get(user_id, 1) - 1
        if remaining > 0:
            _lock_refs[user_id] = remaining
        else:
            _lock_refs.pop(user_id, None)
            if not _venues.get(user_id):
                _locks.pop(user_id, None)


async def _load_venue_config(pool, user_id) -> dict:
    row = await pool.fetchrow(
        "SELECT data FROM user_settings WHERE user_id = $1", user_id
    )
    if row is None:
        return {}
    data = row["data"]
    return json.loads(data) if isinstance(data, str) else data


async def refresh_venues(pool, user_id) -> dict[str, str]:
    """Check Settings for enabled venues and connect/disconnect as needed.

    Returns a status dict: {venue_key: 'connected' | 'disabled' | 'error: ...'}

    The whole body runs under the cross-process rotation lock (A14): reading
    the stored refresh token, connecting, and persisting a rotated token are
    one serialized critical section per user, across the API and scheduler
    processes alike. A caller that cannot take the lock in bounded time gets
    an honest busy status for each of the user's venues rather than racing
    the rotation. Without a pool there is no cross-process venue state to
    arbitrate (and no stored settings to load), so only the in-process lock
    applies.
    """
    async with _user_lock(user_id):
        config = await _load_venue_config(pool, user_id)
        venues_config = config.get("venues", {})
        try:
            if pool is not None:
                async with _rotation_lock(pool, user_id):
                    return await _refresh_venues_locked(pool, user_id, venues_config)
            return await _refresh_venues_locked(pool, user_id, venues_config)
        except RefreshBusy:
            keys = set(venues_config) | set(_venues.get(user_id, {}))
            if not keys:
                return {}
            status = {
                key: "error: another venue refresh is in progress"
                for key in keys
            }
            logger.info(
                "venue refresh for user %s deferred: rotation lock busy", user_id
            )
            return status


async def _refresh_venues_locked(
    pool, user_id, venues_config: dict
) -> dict[str, str]:
    owner_venues = _venues.setdefault(user_id, {})
    status: dict[str, str] = {}

    for key in set(venues_config) | set(owner_venues):
        vc = venues_config.get(key, {})
        enabled = vc.get("enabled", False)

        if key not in CONNECTABLE_VENUES:
            old = owner_venues.pop(key, None)
            if old is not None and hasattr(old, "aclose"):
                await old.aclose()
            status[key] = "scheduler-only" if key == "hyperliquid" else "unavailable"
            continue

        if not enabled:
            old = owner_venues.pop(key, None)
            if old is not None:
                if hasattr(old, "aclose"):
                    await old.aclose()
                logger.info("venue %s disconnected for user %s", key, user_id)
            status[key] = "disabled"
            continue

        if key in owner_venues:
            status[key] = "connected"
            continue

        credentials: dict = {}
        try:
            credentials = decrypt_fields(
                vc.get("credentials", {}) or {}, SECRET_FIELDS.get(key, ())
            )

            async def persist_rotated_token(
                token: str, venue_key: str = key
            ) -> None:
                await store_venue_refresh_token(pool, user_id, venue_key, token)

            venue = await _connect_venue(key, credentials, persist_rotated_token)
            if venue is not None:
                owner_venues[key] = venue
                status[key] = "connected"
                logger.info("venue %s connected for user %s", key, user_id)
            else:
                status[key] = "no credentials"
        except Exception as exc:  # noqa: BLE001
            error = _safe_error(exc, credentials)
            status[key] = f"error: {error}"
            logger.warning("venue %s failed for user %s: %s", key, user_id, error)

    if not owner_venues:
        _venues.pop(user_id, None)
    return status


async def store_venue_credentials(pool, user_id, venue_key: str, credentials: dict) -> None:
    """Write a venue's credentials, encrypting the secret fields first.

    The encryption happens here rather than at the API edge so there is exactly
    one path into storage. A second writer that forgot to encrypt would leave
    plaintext rows that read back perfectly, and nothing would report it.
    """
    from omni.credentials.keyring import encrypt_fields

    protected = encrypt_fields(credentials or {}, SECRET_FIELDS.get(venue_key, ()))
    await pool.execute(
        """
        INSERT INTO user_settings (user_id, data)
        VALUES (
            $1,
            jsonb_build_object(
                'venues', jsonb_build_object(
                    $2::text, jsonb_build_object('credentials', $3::jsonb)
                )
            )
        )
        ON CONFLICT (user_id) DO UPDATE SET
            data = jsonb_set(
                user_settings.data,
                '{venues}',
                COALESCE(user_settings.data->'venues', '{}'::jsonb)
                || jsonb_build_object(
                    $2::text,
                    COALESCE(user_settings.data #> ARRAY['venues', $2::text], '{}'::jsonb)
                    || jsonb_build_object('credentials', $3::jsonb)
                ),
                true
            ),
            updated_at = now()
        """,
        user_id,
        venue_key,
        json.dumps(protected),
    )


async def store_venue_refresh_token(pool, user_id, venue_key: str, token: str) -> None:
    from omni.credentials.keyring import encrypt

    result = await pool.execute(
        """
        UPDATE user_settings
        SET data = jsonb_set(
                data,
                '{venues}',
                COALESCE(data->'venues', '{}'::jsonb)
                || jsonb_build_object(
                    $2::text,
                    COALESCE(data #> ARRAY['venues', $2::text], '{}'::jsonb)
                    || jsonb_build_object(
                        'credentials',
                        COALESCE(data #> ARRAY['venues', $2::text, 'credentials'], '{}'::jsonb)
                        || jsonb_build_object('refresh_token', $3::text)
                    )
                ),
                true
            ),
            updated_at = now()
        WHERE user_id = $1
        """,
        user_id,
        venue_key,
        encrypt(token),
    )
    if result != "UPDATE 1":
        raise RuntimeError("cannot persist rotated Questrade token: settings row missing")


async def set_venue_enabled(pool, user_id, venue_key: str, enabled: bool) -> bool:
    if enabled:
        result = await pool.execute(
            """
            UPDATE user_settings
            SET data = jsonb_set(
                    data,
                    '{venues}',
                    COALESCE(data->'venues', '{}'::jsonb)
                    || jsonb_build_object(
                        $2::text,
                        COALESCE(data #> ARRAY['venues', $2::text], '{}'::jsonb)
                        || jsonb_build_object('enabled', true)
                    ),
                    true
                ),
                updated_at = now()
            WHERE user_id = $1
              AND COALESCE(
                    data #> ARRAY['venues', $2::text, 'credentials'],
                    '{}'::jsonb
                  ) <> '{}'::jsonb
            """,
            user_id,
            venue_key,
        )
        return result == "UPDATE 1"

    await _merge_venue_entry(pool, user_id, venue_key, {"enabled": False})
    return True


async def clear_venue_credentials(pool, user_id, venue_key: str) -> None:
    await _merge_venue_entry(
        pool,
        user_id,
        venue_key,
        {"credentials": {}, "enabled": False},
    )


async def _merge_venue_entry(pool, user_id, venue_key: str, patch: dict) -> None:
    await pool.execute(
        """
        INSERT INTO user_settings (user_id, data)
        VALUES ($1, jsonb_build_object('venues', jsonb_build_object($2::text, $3::jsonb)))
        ON CONFLICT (user_id) DO UPDATE SET
            data = jsonb_set(
                user_settings.data,
                '{venues}',
                COALESCE(user_settings.data->'venues', '{}'::jsonb)
                || jsonb_build_object(
                    $2::text,
                    COALESCE(user_settings.data #> ARRAY['venues', $2::text], '{}'::jsonb)
                    || $3::jsonb
                ),
                true
            ),
            updated_at = now()
        """,
        user_id,
        venue_key,
        json.dumps(patch),
    )


def _safe_error(exc: Exception, credentials: dict) -> str:
    message = str(exc)
    for value in credentials.values():
        if isinstance(value, str) and value:
            message = message.replace(value, "[redacted]")
    return message[:100]


async def _connect_venue(
    key: str,
    credentials: dict,
    on_refresh_token: Callable[[str], Awaitable[None]] | None = None,
) -> Any | None:

    if key == "questrade":
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            return None
        from omni.venue.questrade_venue import QuestradeVenue
        practice = credentials.get("practice", True)
        return await QuestradeVenue.connect(
            refresh_token=refresh_token,
            practice=practice,
            on_refresh_token=on_refresh_token,
        )

    return None


#: How often the scheduler re-reads venue configuration. Six minutes is short
#: enough that a Settings toggle takes effect while the operator is still
#: looking at the page, and long enough that a venue whose adapter is failing
#: is not retried in a tight loop.
RECONCILE_INTERVAL_SECONDS = 360.0


async def reconcile_once(pool) -> dict[UUID, dict[str, str]]:
    """Bring live connections in line with stored settings, once.

    Every failure is contained. A venue that will not connect must not prevent
    the scheduler from starting, because the scheduler's job is the coverage
    loops and the venues are an adjunct to it.
    """
    rows = await pool.fetch(
        """
        SELECT u.id
        FROM users u
        JOIN user_settings s ON s.user_id = u.id
        WHERE u.active
        ORDER BY u.created_at, u.id
        """
    )
    active_users = {row["id"] for row in rows}
    for user_id in set(_venues) - active_users:
        await disconnect_user(user_id)

    statuses: dict[UUID, dict[str, str]] = {}
    for user_id in active_users:
        try:
            statuses[user_id] = await refresh_venues(pool, user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("venue reconcile failed for user %s: %s", user_id, exc)
            statuses[user_id] = {"_reconcile": f"error: {str(exc)[:100]}"}
    return statuses


async def reconcile_forever(pool, stopping, interval: float = RECONCILE_INTERVAL_SECONDS) -> None:
    """Reconcile at boot and on a bounded interval until asked to stop.

    Previously `refresh_venues` ran only when a Settings endpoint was called,
    so a venue enabled in the UI stayed disconnected until someone reloaded
    that page, and a restart silently dropped every connection. Comments in
    this module implied otherwise, which is worse than the gap itself.
    """
    import asyncio

    from omni.scheduler.heartbeat import expect_loop

    expect_loop("venue_reconciliation")
    while not stopping.is_set():
        try:
            status = await reconcile_once(pool)
            failures = [
                f"{user_id}:{key}={state}"
                for user_id, venues in sorted(status.items(), key=lambda item: str(item[0]))
                for key, state in sorted(venues.items())
                if state.startswith("error:")
            ]
            await record_loop_health(
                pool,
                loop_name="venue_reconciliation",
                ok=not failures,
                error="; ".join(failures) or None,
                result=f"{len(status)} configured users checked",
                expected_interval_seconds=interval,
            )
            if status:
                logger.info(
                    "venues reconciled: %s",
                    ", ".join(
                        f"{user_id}:{key}={state}"
                        for user_id, venues in sorted(
                            status.items(), key=lambda item: str(item[0])
                        )
                        for key, state in sorted(venues.items())
                    ),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await record_loop_health(
                    pool,
                    loop_name="venue_reconciliation",
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    expected_interval_seconds=interval,
                )
            except Exception:
                logger.exception("could not record venue reconciliation failure")
            logger.exception("venue reconciliation failed")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=interval)
        except TimeoutError:
            continue


def get_venue(user_id: UUID, key: str) -> Any | None:
    return _venues.get(user_id, {}).get(key)


def connected_venues(user_id: UUID) -> dict[str, Any]:
    return dict(_venues.get(user_id, {}))


async def disconnect_venue(user_id: UUID, key: str) -> None:
    async with _user_lock(user_id):
        venue = _venues.get(user_id, {}).pop(key, None)
        if venue is not None and hasattr(venue, "aclose"):
            await venue.aclose()
        if user_id in _venues and not _venues[user_id]:
            _venues.pop(user_id, None)


async def disconnect_user(user_id: UUID) -> None:
    async with _user_lock(user_id):
        venues = _venues.pop(user_id, {})
        for key, venue in venues.items():
            try:
                if hasattr(venue, "aclose"):
                    await venue.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("venue %s close failed for user %s: %s", key, user_id, exc)


async def disconnect_all() -> None:
    owners = list(_venues)
    for user_id in owners:
        await disconnect_user(user_id)
    _venues.clear()
    _locks.clear()
    _lock_refs.clear()
