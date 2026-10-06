"""Atomic credential admission (audit findings A01, A02, A03).

The 075 pass made throttle state durable but kept admission
check-then-record: concurrent logins all observed the same admitted state
before any recorded its outcome, the durable key reset when either side
rotated, and password operations hashed without any admission at all. These
tests pin the replacement:

* A01 -- fixed-window IP/account budgets reserved atomically, independent of
  outcome, shared across app instances; a serialized
  check-verify-record transaction per (email, IP); a committed failure
  record surviving the 401 it bought.
* A02 -- hashing submission is bounded (running plus queued, refused with
  503 beyond), every password endpoint takes admission, and unknown-email
  logins verify a hash exactly like wrong-password logins.
* A03 -- password change is compare-and-swap on the exact hash and
  auth_version verified; a concurrent change/logout/deactivation cannot be
  overwritten by a stale request.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from neutron.test import TestClient

from omni.auth import users
from omni.auth.admission import reserve_budgets
from omni.auth.throttle import email_fingerprint
from omni.main import create_app

SECRET = "a" * 48


class _Lifespan:
    def __init__(self, app):
        self._app = app
        self._receive = asyncio.Queue()
        self._send = asyncio.Queue()
        self._task = None

    async def __aenter__(self):
        self._task = asyncio.create_task(
            self._app({"type": "lifespan"}, self._receive.get, self._send.put)
        )
        await self._receive.put({"type": "lifespan.startup"})
        message = await self._send.get()
        assert message["type"] == "lifespan.startup.complete", message
        return self._app

    async def __aexit__(self, *exc):
        await self._receive.put({"type": "lifespan.shutdown"})
        await self._send.get()
        await self._task


def _bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("OMNI_JWT_SECRET", SECRET)
    yield


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    await db.pool.execute("TRUNCATE auth_throttle_event")
    await db.pool.execute("TRUNCATE auth_budget")
    yield


class TestAdmissionBudgets:
    async def test_budget_reservation_is_atomic_and_independent_of_outcome(self, db):
        # Ten reservations of a budget of ten all succeed; the eleventh is
        # refused, and -- the A01 point -- SUCCESS does not refund: the
        # count is of requests, not failures.
        for i in range(10):
            await reserve_budgets(db.pool, [("budget-test", 10, 60)])
        with pytest.raises(Exception) as excinfo:
            await reserve_budgets(db.pool, [("budget-test", 10, 60)])
        assert excinfo.value.status_code == 429
        assert excinfo.value.headers["Retry-After"] == "60"

    async def test_an_expired_window_starts_a_fresh_budget(self, db):
        for i in range(3):
            await reserve_budgets(db.pool, [("expiry-test", 3, 60)])
        from starlette.exceptions import HTTPException as _HTTPException

        with pytest.raises(_HTTPException):
            await reserve_budgets(db.pool, [("expiry-test", 3, 60)])
        await db.pool.execute(
            "UPDATE auth_budget SET expires_at = clock_timestamp() - interval '1 second' "
            "WHERE key = 'expiry-test'"
        )
        await reserve_budgets(db.pool, [("expiry-test", 3, 60)])
        used = await db.pool.fetchval(
            "SELECT used FROM auth_budget WHERE key = 'expiry-test'"
        )
        assert used == 1, "an expired window must reset, not extend"

    async def test_the_ip_budget_is_shared_across_app_instances(self, db, database_url):
        # Two app objects = two processes. Whatever each process's own
        # in-memory limiter allows (reset per request here so only the
        # shared budget is under test), the database IP budget must bound
        # them to one deployment-wide number.
        from omni.auth.ratelimit import reset_for_test
        from omni.config import settings

        app_a = create_app(database_url)
        app_b = create_app(database_url)
        async with _Lifespan(app_a), _Lifespan(app_b), TestClient(app_a) as a, TestClient(app_b) as b:
            # Distinct emails per request so the ACCOUNT budget never trips:
            # the IP budget is the thing under test.
            for i in range(settings.omni_auth_ip_budget):
                reset_for_test()
                email = f"ip-shared-{i}@example.com"
                await a.post(
                    "/auth/login", json={"email": email, "password": "wrong" * 3}
                )
            reset_for_test()
            over = await b.post(
                "/auth/login",
                json={
                    "email": f"ip-shared-{settings.omni_auth_ip_budget}@example.com",
                    "password": "wrong" * 3,
                },
            )
            assert over.status_code == 429, (
                "a second replica exceeded the deployment's IP budget"
            )

    async def test_the_account_budget_bounds_distributed_guessing(self, db):
        # One account fingerprint addressed from rotating IPs: every address
        # has its own IP budget, so only the account budget can bound the
        # guesses. Ten admitted reservations, the eleventh refused.
        from omni.config import settings

        account = f"credential:account:{email_fingerprint('one-account@example.com')}"
        for i in range(settings.omni_auth_account_budget):
            await reserve_budgets(db.pool, [
                (f"credential:ip:198.51.100.{i}", settings.omni_auth_ip_budget, 60),
                (account, settings.omni_auth_account_budget, 60),
            ])
        with pytest.raises(Exception) as excinfo:
            await reserve_budgets(db.pool, [
                ("credential:ip:203.0.113.9", settings.omni_auth_ip_budget, 60),
                (account, settings.omni_auth_account_budget, 60),
            ])
        assert excinfo.value.status_code == 429, (
            "rotating IPs bought attempts beyond the account budget"
        )

    async def test_a_successful_login_does_not_refund_the_ip_budget(
        self, db, database_url
    ):
        from omni.config import settings

        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            password = "correct-horse-battery"
            for i in range(settings.omni_auth_ip_budget - 1):
                await client.post(
                    "/auth/login",
                    json={"email": f"ok-{i}@example.com", "password": password},
                )
            # Every one of those succeeded; the budget still counts them.
            last = await client.post(
                "/auth/login",
                json={"email": "final@example.com", "password": password},
            )
            assert last.status_code == 429, (
                "successful requests refunded the IP budget"
            )


class TestAtomicAdmission:
    async def test_concurrent_failures_for_one_pair_are_serialized(self, db, database_url):
        # Five simultaneous wrong passwords for one (email, ip): the guard
        # admits exactly one at a time per pair, so the overlapping four are
        # refused (429) rather than piling into the same verification -- and
        # crucially, every 401 that IS returned has committed its failure
        # row. Under check-then-record these could interleave and under-record.
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            await client.post(
                "/auth/setup",
                json={"email": "race@example.com", "password": "a" * 16},
            )
            from omni.auth.ratelimit import reset_for_test

            reset_for_test()
            responses = await asyncio.gather(*[
                client.post(
                    "/auth/login",
                    json={"email": "race@example.com", "password": "wrong" * 3},
                )
                for _ in range(5)
            ])
        statuses = sorted(r.status_code for r in responses)
        assert statuses.count(401) >= 1
        refused = statuses.count(429)
        recorded = await db.pool.fetchval(
            "SELECT count(*) FROM auth_throttle_event WHERE client_ip = '127.0.0.1'"
        )
        assert recorded == statuses.count(401), (
            f"{statuses.count(401)} wrong-password 401s but {recorded} committed "
            "failure rows; a refusal escaped without its record, or a record "
            "was written for a request that never verified"
        )
        assert refused + statuses.count(401) == 5

    async def test_a_committed_failure_survives_the_401_it_bought(
        self, db, database_url
    ):
        # The failure record commits BEFORE the 401 is raised; raising
        # inside the guard would roll it back and the next attempt would be
        # judged from a history that forgot this one.
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            await client.post(
                "/auth/setup",
                json={"email": "committed@example.com", "password": "a" * 16},
            )
            r = await client.post(
                "/auth/login",
                json={"email": "committed@example.com", "password": "wrong" * 3},
            )
            assert r.status_code == 401
        rows = await db.pool.fetchval(
            "SELECT count(*) FROM auth_throttle_event "
            "WHERE email_hash = $1 AND client_ip = '127.0.0.1'",
            email_fingerprint("committed@example.com"),
        )
        assert rows == 1, "the 401 rolled its own failure record back with it"

    async def test_serialized_admission_locks_per_pair_not_globally(self, db):
        # Two different (email, ip) pairs must be able to hold the guard at
        # the same time; a lock keyed wrongly (globally) would serialize all
        # logins deployment-wide.
        from omni.auth.admission import credential_guard

        entered = []
        release = asyncio.Event()

        async def hold(email, ip):
            async with credential_guard(db.pool, email=email, ip=ip):
                entered.append((email, ip))
                if email == "first@example.com":
                    await release.wait()

        t1 = asyncio.create_task(hold("first@example.com", "203.0.113.1"))
        while ("first@example.com", "203.0.113.1") not in entered:
            await asyncio.sleep(0)
        t2 = asyncio.create_task(hold("second@example.com", "203.0.113.2"))
        await asyncio.sleep(0.2)
        assert ("second@example.com", "203.0.113.2") in entered, (
            "different pairs were serialized behind one another"
        )
        release.set()
        await asyncio.gather(t1, t2)

    async def test_the_same_pair_admits_one_at_a_time(self, db):
        from omni.auth.admission import credential_guard

        inside = 0
        peak = 0

        async def enter():
            nonlocal inside, peak
            async with credential_guard(
                db.pool, email="pair@example.com", ip="203.0.113.5"
            ):
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.1)
                inside -= 1

        results = await asyncio.gather(enter(), enter(), return_exceptions=True)
        assert peak == 1, "two requests for one pair verified concurrently"
        refused = [r for r in results if getattr(r, "status_code", None) == 429]
        assert refused, "the overlapping request was not refused"


class TestHashingSubmissionBound:
    async def test_beyond_the_queue_depth_submission_fails_promptly(self):
        # Four slots: one running plus three queued. Occupying all four with
        # jobs the single worker cannot finish, the fifth submission is a
        # 503 raised immediately -- not work queued where the executor
        # cannot reach it.
        import threading

        release = threading.Event()

        def _hang(_password: str) -> str:
            release.wait(timeout=10)
            return "x"

        tasks = [
            asyncio.create_task(users._password_call(_hang, "p" * 12))
            for _ in range(users._PASSWORD_SLOTS)
        ]
        await asyncio.sleep(0.1)
        with pytest.raises(Exception) as excinfo:
            await asyncio.wait_for(users._password_call(_hang, "p" * 12), timeout=2)
        assert getattr(excinfo.value, "status", None) == 503, repr(excinfo.value)
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_slot_released_only_when_the_job_actually_finishes(self, monkeypatch):
        import threading

        started = threading.Event()
        finish = threading.Event()

        def _slow_hash(password: str) -> str:
            started.set()
            finish.wait(timeout=5)
            return "x"

        task = asyncio.create_task(users._password_call(_slow_hash, "p" * 12))
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert users._password_slots._value == users._PASSWORD_SLOTS - 1, (
            "a cancelled waiter freed a slot while its job still runs"
        )
        finish.set()
        deadline = asyncio.get_event_loop().time() + 5
        while users._password_slots._value < users._PASSWORD_SLOTS:
            assert asyncio.get_event_loop().time() < deadline, (
                "the finished job never released its slot"
            )
            await asyncio.sleep(0.01)

    async def test_unknown_email_and_wrong_password_both_verify_exactly_once(
        self, db, monkeypatch
    ):
        calls: list[str] = []

        real_verify = users.verify_password

        def _counting_verify(password, hashed):
            calls.append(hashed[:8])
            return real_verify(password, hashed)

        monkeypatch.setattr(users, "verify_password", _counting_verify)
        await users.prime_password_hash()

        ok = await users.authenticate_user(
            db.pool, email="ghost@example.com", password="whatever-password"
        )
        assert ok is None
        assert len(calls) == 1, (
            "an unknown email skipped verification entirely"
        )

        await db.pool.execute(
            "INSERT INTO users (email, password_hash) VALUES ('real@example.com', $1)",
            await users._password_call(users.hash_password, "real-password-123"),
        )
        calls.clear()
        wrong = await users.authenticate_user(
            db.pool, email="real@example.com", password="wrong-password-123"
        )
        assert wrong is None
        assert len(calls) == 1

    async def test_password_change_takes_admission(self, db, database_url):
        from omni.auth.ratelimit import reset_for_test
        from omni.config import settings

        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post(
                "/auth/setup",
                json={"email": "guard@example.com", "password": "a" * 16},
            )
            token = r.json()["token"]
            # Hammer change-password with wrong current passwords, resetting
            # the per-process burst limiter each time so only the shared
            # account budget (keyed on the server-loaded email) is under
            # test: guessing the current password through this endpoint must
            # be bounded exactly like guessing it through login.
            for _ in range(settings.omni_auth_account_budget):
                reset_for_test()
                await client.post(
                    "/auth/change-password",
                    json={"old_password": "wrong" * 2, "new_password": "b" * 16},
                    headers=_bearer(token),
                )
            reset_for_test()
            over = await client.post(
                "/auth/change-password",
                json={"old_password": "wrong" * 2, "new_password": "b" * 16},
                headers=_bearer(token),
            )
            assert over.status_code == 429, (
                "password-change guesses were not bounded by the account budget"
            )


class TestChangePasswordCas:
    async def _user(self, db) -> tuple:
        user_id = uuid4()
        hash_ = await users._password_call(users.hash_password, "old-password-123")
        await db.pool.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, $3)",
            user_id,
            f"cas-{uuid4().hex[:6]}@example.com",
            hash_,
        )
        return user_id, hash_

    async def test_two_concurrent_changes_exactly_one_wins(self, db):
        user_id, _hash = await self._user(db)
        barrier = asyncio.Event()
        released = asyncio.Event()

        real_verify = users.verify_password
        verified = []
        import omni.auth.users as users_module

        original_call = users_module._password_call

        async def _patched_call(func, *args):
            if func is real_verify:
                password, hashed = args
                ok = await original_call(real_verify, password, hashed)
                verified.append(ok)
                if len(verified) == 2:
                    barrier.set()
                await asyncio.wait_for(released.wait(), timeout=5)
                return ok
            return await original_call(func, *args)

        users_module._password_call = _patched_call
        try:
            async def attempt(new_password):
                return await users.change_password(
                    db.pool,
                    user_id=user_id,
                    old_password="old-password-123",
                    new_password=new_password,
                    expected_auth_version=0,
                )

            t1 = asyncio.create_task(attempt("first-new-password"))
            t2 = asyncio.create_task(attempt("second-new-password"))
            while len(verified) < 2:
                await asyncio.sleep(0)
            released.set()
            results = await asyncio.gather(t1, t2, return_exceptions=True)
        finally:
            users_module._password_call = original_call

        outcomes = [
            r if isinstance(r, bool) else type(r).__name__ for r in results
        ]
        assert outcomes.count(True) == 1, outcomes
        assert outcomes.count(False) == 0, outcomes
        stored = await db.pool.fetchval(
            "SELECT password_hash FROM users WHERE id = $1", user_id
        )
        assert await users._password_call(
            users.verify_password, "first-new-password", stored
        ) or await users._password_call(
            users.verify_password, "second-new-password", stored
        )

    async def test_a_stale_auth_version_cannot_change_the_password(self, db):
        user_id, _ = await self._user(db)
        # Move the epoch (logout-all), then replay the request that would
        # have verified against the old epoch.
        await users.bump_auth_version(db.pool, user_id)
        with pytest.raises(users.CredentialChangedConcurrently):
            await users.change_password(
                db.pool,
                user_id=user_id,
                old_password="old-password-123",
                new_password="new-password-456",
                expected_auth_version=0,
            )
        ok = await users.authenticate_user(
            db.pool,
            email=(await db.pool.fetchval(
                "SELECT email FROM users WHERE id = $1", user_id
            )),
            password="old-password-123",
        )
        assert ok is not None, "a stale request overwrote a concurrent logout"

    async def test_deactivation_between_verification_and_update_is_refused(self, db):
        user_id, _ = await self._user(db)
        await db.pool.execute(
            "UPDATE users SET active = FALSE WHERE id = $1", user_id
        )
        with pytest.raises(users.CredentialChangedConcurrently):
            await users.change_password(
                db.pool,
                user_id=user_id,
                old_password="old-password-123",
                new_password="new-password-456",
                expected_auth_version=0,
            )

    async def test_a_token_whose_epoch_moved_is_unauthenticated_not_409(
        self, db, database_url
    ):
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post(
                "/auth/setup",
                json={"email": "cas409@example.com", "password": "a" * 16},
            )
            token = r.json()["token"]
            user_id = r.json()["user"]["id"]
            # The token carries ver 0; bump the epoch behind its back and
            # the middleware refuses the token before change-password runs.
            await db.pool.execute(
                "UPDATE users SET auth_version = 1 WHERE id = $1", user_id
            )
            r = await client.post(
                "/auth/change-password",
                json={"old_password": "a" * 16, "new_password": "b" * 16},
                headers=_bearer(token),
            )
        assert r.status_code == 401, (
            "a token whose epoch moved is no longer authenticated"
        )

    async def test_the_endpoint_maps_a_concurrent_credential_change_to_409(
        self, db, database_url, monkeypatch
    ):
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post(
                "/auth/setup",
                json={"email": "casrace@example.com", "password": "a" * 16},
            )
            token = r.json()["token"]

            async def _moved_under_us(*args, **kwargs):
                raise users.CredentialChangedConcurrently

            monkeypatch.setattr("omni.api.auth.change_password", _moved_under_us)
            r = await client.post(
                "/auth/change-password",
                json={"old_password": "a" * 16, "new_password": "b" * 16},
                headers=_bearer(token),
            )
        assert r.status_code == 409, r.text
