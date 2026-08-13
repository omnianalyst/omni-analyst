"""The carry decision reads: `/trading/schedule` and `/trading/classification`.

`/trading/cycles` already said what the book did. What the operator could not
read anywhere was the decision: when the next rebalance is due, how much of the
six-week hold is left, and what the runner refused. The last of those used to be
genuinely absent from the database -- a refused cycle wrote no row -- and the
endpoint said so rather than derive a sentence that would read identically
whether the cycle fired and refused or the scheduler never ran. Migration 057
records the refusal, so the endpoint now reports the sentence the runner
produced, and an empty record is dated by when the recording began.

What is pinned here: the hold this page counts down is the hold the runner
enforces and not a second copy of it; a book inside its hold is distinguishable
from a book whose cycles all halted and from a book that has never run one; the
refusal reported is the one the caller was actually given rather than a
re-derived copy, and it stays out of the cycle log; and a symbol the governed
universe does not list is unclassified rather than filed as a stock.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from neutron.test import TestClient

from omni.api import trading
from omni.api.trading import build_router as trading_router
from omni.main import create_app
from omni.trading import carry_runner
from omni.trading.carry_loop import CarryConfig
from omni.trading.carry_runner import CarryRunRefused, run_due_cycle

GOOD_SECRET = "x" * 48
VENUE = "hyperliquid"


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
        msg = await self._send.get()
        assert msg["type"] == "lifespan.startup.complete", msg
        return self._app

    async def __aexit__(self, *exc):
        await self._receive.put({"type": "lifespan.shutdown"})
        await self._send.get()
        await self._task


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("OMNI_JWT_SECRET", GOOD_SECRET)
    yield


@pytest.fixture(autouse=True)
async def _clean_users(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


def _app(database_url):
    app = create_app(database_url)
    app.include_router(trading_router(app))
    return app


async def _operator(client) -> tuple[str, UUID]:
    r = await client.post(
        "/auth/setup", json={"email": "op@example.com", "password": "a" * 16}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["token"], UUID(body["user"]["id"])


async def _second_user(client, token) -> UUID:
    r = await client.post(
        "/auth/register",
        json={"email": "other@example.com", "password": "b" * 16},
        headers={"authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return UUID(r.json()["id"])


async def _portfolio(db, user_id) -> UUID:
    return await db.pool.fetchval(
        """
        INSERT INTO portfolio (user_id, name, base_currency)
        VALUES ($1, $2, 'USD') RETURNING id
        """,
        user_id,
        f"book-{uuid4().hex[:8]}",
    )


async def _cycle(
    db,
    portfolio_id,
    *,
    as_of,
    venue=VENUE,
    halted=False,
    halt_reason=None,
    funding_since=None,
    settled=True,
):
    """One row in the log the runner reads its boundary from."""
    since = funding_since or as_of - timedelta(weeks=6)
    await db.pool.execute(
        """
        INSERT INTO carry_cycle (
            portfolio_id, venue, as_of, funding_since, funding_settled_through,
            halted, halt_reason, abstention,
            funding_collected, fees_paid, modelled_turnover_cost,
            pairs_opened, pairs_closed, pairs_held
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,NULL,$8,$9,$10,1,0,2)
        """,
        portfolio_id,
        venue,
        as_of,
        since,
        as_of if settled and not halted else None,
        halted,
        halt_reason,
        Decimal("1.25"),
        Decimal("0.10"),
        Decimal("0.05"),
    )


async def _hold(db, portfolio_id, *, symbol, venue=VENUE, market_type="spot"):
    await db.pool.execute(
        """
        INSERT INTO position (portfolio_id, venue, symbol, market_type,
                              quantity, average_entry, updated_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
        """,
        portfolio_id,
        venue,
        symbol,
        market_type,
        Decimal(1),
        Decimal(100),
        datetime.now(UTC),
    )


async def _read(client, token, path):
    return await client.get(path, headers={"authorization": f"Bearer {token}"})


def _venue_row(body, name=VENUE):
    matches = [v for v in body["venues"] if v["venue"] == name]
    assert len(matches) == 1, f"{name} appears {len(matches)} times"
    return matches[0]


class _NamedVenue:
    """Enough venue for the guards that refuse before one is contacted.

    `run_due_cycle` reads `venue.name` to find the boundary and refuses on the
    cadence before anything is fetched, traded or settled. A stub that would
    raise on any other access is the point: if the refusal ever stopped being
    the first thing that happens, this test would fail rather than quietly
    reach a live venue.
    """

    def __init__(self, name=VENUE):
        self.name = name


def _config() -> CarryConfig:
    return CarryConfig(
        enter_rank=5,
        exit_rank=15,
        notional_per_pair=Decimal(50),
        funding_venue=VENUE,
        spread_bps=Decimal(2),
        reconciliation_tolerance=Decimal("0.0001"),
    )


class TestSchedule:
    async def test_a_book_inside_its_hold_reports_the_date_the_runner_would_enforce(
        self, database_url, db
    ):
        """The steady state, and the one an empty cycle table looks like."""
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            last_completed = datetime.now(UTC) - timedelta(days=2)
            await _cycle(db, portfolio_id, as_of=last_completed)

            r = await _read(client, token, "/trading/schedule")
            assert r.status_code == 200, r.text
            body = r.json()

            assert body["rebalance_period_days"] == 42
            row = _venue_row(body)
            assert row["state"] == "holding"
            due = datetime.fromisoformat(row["next_rebalance_due_at"])
            assert due == last_completed + timedelta(weeks=6)
            assert row["days_until_due"] == 40
            assert row["last_completed_at"] == last_completed.isoformat()

    async def test_the_countdown_hits_zero_exactly_when_the_runner_stops_refusing(
        self, database_url, db
    ):
        """A hold that has elapsed is `due`, not a negative countdown.

        Both halves are checked against the runner itself: at 41 days it refuses
        and the page says one day left; at 43 the same call gets past the
        cadence guard and the page says due.
        """
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            inside = await _portfolio(db, user_id)
            await _cycle(
                db, inside, as_of=datetime.now(UTC) - timedelta(days=41)
            )

            r = await _read(
                client, token, f"/trading/schedule?portfolio_id={inside}"
            )
            row = _venue_row(r.json())
            assert row["state"] == "holding"
            assert row["days_until_due"] == 1

            elapsed = await _portfolio(db, user_id)
            await _cycle(
                db, elapsed, as_of=datetime.now(UTC) - timedelta(days=43)
            )
            r = await _read(
                client, token, f"/trading/schedule?portfolio_id={elapsed}"
            )
            row = _venue_row(r.json())
            assert row["state"] == "due"
            assert row["days_until_due"] == 0

    async def test_the_hold_counted_down_is_the_hold_the_runner_enforces(
        self, database_url, db, monkeypatch
    ):
        """Not a second copy of six weeks.

        Move the runner's period and the page must move with it. A constant
        restated in the API would keep counting down to a date the runner does
        not recognise, and the page is what the operator reads instead of the
        log.
        """
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            last_completed = datetime.now(UTC) - timedelta(days=2)
            await _cycle(db, portfolio_id, as_of=last_completed)

            monkeypatch.setattr(
                carry_runner, "REBALANCE_PERIOD", timedelta(weeks=8)
            )
            r = await _read(client, token, "/trading/schedule")
            body = r.json()
            assert body["rebalance_period_days"] == 56
            row = _venue_row(body)
            assert datetime.fromisoformat(
                row["next_rebalance_due_at"]
            ) == last_completed + timedelta(weeks=8)
            assert row["days_until_due"] == 54

    async def test_a_refused_cycle_reports_the_reason_it_actually_gave(
        self, database_url, db
    ):
        """The blind spot this endpoint used to describe, now closed.

        Until migration 057 the assertion here was the opposite one: the runner
        was made to refuse, the cycle table was unchanged, and the endpoint
        reported the reason as unavailable because nothing in the database
        distinguished a cycle that fired and refused from a scheduler that never
        ran. The refusal is now written before it is raised, so the endpoint
        reports the sentence the runner produced -- and this test compares it
        against the exception the caller saw rather than against a copy, because
        two independently-worded strings would agree in the test and diverge in
        production.
        """
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            last_completed = datetime.now(UTC).replace(
                hour=5, minute=0, second=0, microsecond=0
            ) - timedelta(days=2)
            await _cycle(db, portfolio_id, as_of=last_completed)

            cycles_before = await db.pool.fetchval(
                "SELECT count(*) FROM carry_cycle WHERE portfolio_id = $1",
                portfolio_id,
            )
            attempted_at = last_completed + timedelta(days=1)

            with pytest.raises(CarryRunRefused) as refusal:
                await run_due_cycle(
                    db.pool,
                    venue=_NamedVenue(),
                    portfolio_id=portfolio_id,
                    config=_config(),
                    entity_ids=[],
                    audience_user_id=user_id,
                    now=attempted_at,
                )
            assert "the hold is 42 days" in str(refusal.value)

            cycles_after = await db.pool.fetchval(
                "SELECT count(*) FROM carry_cycle WHERE portfolio_id = $1",
                portfolio_id,
            )
            assert cycles_after == cycles_before, (
                "the refusal was written into the cycle log; a refusal is not a "
                "cycle that earned zero and every reader of that table would "
                "now have to exclude it"
            )

            body = (await _read(client, token, "/trading/schedule")).json()
            assert body["last_refusal_unavailable"] is None
            recorded = body["last_refusal"]
            assert recorded["reason"] == str(refusal.value)
            assert recorded["guard"] == carry_runner.GUARD_INSIDE_HOLD
            assert datetime.fromisoformat(recorded["attempted_at"]) == attempted_at
            assert datetime.fromisoformat(recorded["last_completed_at"]) == (
                last_completed
            )
            assert datetime.fromisoformat(recorded["next_due_at"]) == (
                last_completed + carry_runner.REBALANCE_PERIOD
            )

            venue_row = _venue_row(body)
            assert venue_row["state"] == "holding"
            assert venue_row["last_refusal"]["reason"] == str(refusal.value)

    async def test_a_book_with_no_recorded_refusal_says_when_the_record_starts(
        self, database_url, db
    ):
        """An empty record is dated, because otherwise it overclaims.

        `last_refusal: null` on a book that ran before migration 057 does not
        mean no cycle was ever refused -- it means none was refused since the
        table existed. Without the date the sentence would be the same silence
        the table was added to remove.
        """
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cycle(db, portfolio_id, as_of=datetime.now(UTC) - timedelta(days=2))

            body = (await _read(client, token, "/trading/schedule")).json()
            assert body["last_refusal"] is None

            began_at = await db.pool.fetchval(
                "SELECT applied_at FROM _neutron_migrations WHERE version = $1",
                trading.REFUSAL_RECORDING_MIGRATION,
            )
            assert began_at is not None, (
                "migration 057 is not in the log, so the endpoint has no instant "
                "to bound the empty record with"
            )
            assert body["refusal_recording_began_at"] == began_at.isoformat()
            assert began_at.isoformat() in body["last_refusal_unavailable"]

    async def test_the_headline_refusal_is_the_newest_across_venues(
        self, database_url, db
    ):
        """Each venue keeps its own, and the top-level one is the most recent.

        Ordered on the timestamp rather than on its ISO string, so the two
        venues here are refused in the opposite order to their alphabetical
        one: a sort that fell back to venue name would return the wrong row and
        an operator would read a stale reason as the current state.
        """
        other = "aaa-earlier-alphabetically"
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            base = datetime.now(UTC).replace(
                hour=5, minute=0, second=0, microsecond=0
            ) - timedelta(days=2)
            await _cycle(db, portfolio_id, as_of=base)
            await _cycle(db, portfolio_id, as_of=base, venue=other)

            refusals = {}
            for venue_name, offset in ((other, 1), (VENUE, 2)):
                with pytest.raises(CarryRunRefused) as refusal:
                    await run_due_cycle(
                        db.pool,
                        venue=_NamedVenue(venue_name),
                        portfolio_id=portfolio_id,
                        config=_config(),
                        entity_ids=[],
                        audience_user_id=user_id,
                        now=base + timedelta(days=offset),
                    )
                refusals[venue_name] = str(refusal.value)

            body = (await _read(client, token, "/trading/schedule")).json()

            # VENUE was refused a day later than `other`, which sorts first.
            assert body["last_refusal"]["venue"] == VENUE
            assert body["last_refusal"]["reason"] == refusals[VENUE]
            assert _venue_row(body, other)["last_refusal"]["reason"] == refusals[other]

    async def test_one_books_refusal_is_not_reported_on_another(
        self, database_url, db
    ):
        """Refusals are per book, like every other read on this page."""
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            mine = await _portfolio(db, user_id)
            theirs = await _portfolio(db, user_id)
            last_completed = datetime.now(UTC).replace(
                hour=5, minute=0, second=0, microsecond=0
            ) - timedelta(days=2)
            await _cycle(db, theirs, as_of=last_completed)
            await _cycle(db, mine, as_of=last_completed)

            with pytest.raises(CarryRunRefused):
                await run_due_cycle(
                    db.pool,
                    venue=_NamedVenue(),
                    portfolio_id=theirs,
                    config=_config(),
                    entity_ids=[],
                    audience_user_id=user_id,
                    now=last_completed + timedelta(days=1),
                )

            body = (
                await _read(client, token, f"/trading/schedule?portfolio_id={mine}")
            ).json()
            assert body["last_refusal"] is None
            assert _venue_row(body)["last_refusal"] is None

    async def test_a_book_whose_every_cycle_halted_is_not_a_book_inside_its_hold(
        self, database_url, db
    ):
        """The hold is measured from the last *completed* cycle.

        A halted cycle did not complete and must be re-runnable at once, so this
        book is not holding and has no due date to count down to. Reporting it
        as `holding` would tell the operator to wait six weeks for a book that
        is free to run now.
        """
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cycle(
                db,
                portfolio_id,
                as_of=datetime.now(UTC) - timedelta(days=1),
                halted=True,
                halt_reason="the two legs did not fill as one unit",
            )

            body = (await _read(client, token, "/trading/schedule")).json()
            row = _venue_row(body)
            assert row["state"] == "no_completed_cycle"
            assert row["next_rebalance_due_at"] is None
            assert row["days_until_due"] is None
            assert row["last_completed_at"] is None

    async def test_a_venue_holding_positions_with_no_cycle_reports_never_run(
        self, database_url, db
    ):
        """Not silence, and not a due date invented from the position's age."""
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(db, portfolio_id, symbol="ETH/USDC", venue="binance")

            body = (await _read(client, token, "/trading/schedule")).json()
            row = _venue_row(body, "binance")
            assert row["state"] == "never_run"
            assert row["funding_window_opens_at"] is None
            assert row["next_rebalance_due_at"] is None

    async def test_the_window_reported_is_the_window_the_runner_refuses_outside(
        self, database_url, db
    ):
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            await _portfolio(db, user_id)

            body = (await _read(client, token, "/trading/schedule")).json()
            assert body["window_opens_hour"] == carry_runner.WINDOW_OPENS_HOUR
            assert body["window_closes_hour"] == carry_runner.WINDOW_CLOSES_HOUR
            assert body["in_rebalance_window"] == carry_runner.in_rebalance_window(
                datetime.fromisoformat(body["as_of"])
            )

    async def test_the_schedule_is_private_to_the_account_that_owns_the_book(
        self, database_url, db
    ):
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, _ = await _operator(client)
            other = await _second_user(client, token)
            theirs = await _portfolio(db, other)
            await _cycle(db, theirs, as_of=datetime.now(UTC) - timedelta(days=2))

            r = await _read(
                client, token, f"/trading/schedule?portfolio_id={theirs}"
            )
            assert r.status_code == 404, r.text

            anonymous = await client.get("/trading/schedule")
            assert anonymous.status_code == 401, anonymous.text

    async def test_reading_the_schedule_records_nothing(self, database_url, db):
        """A read path an operator refreshes while deciding must not write."""
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cycle(
                db, portfolio_id, as_of=datetime.now(UTC) - timedelta(days=2)
            )

            snapshot = await db.pool.fetch(
                "SELECT * FROM carry_cycle WHERE portfolio_id = $1", portfolio_id
            )
            for _ in range(3):
                assert (
                    await _read(client, token, "/trading/schedule")
                ).status_code == 200
            assert (
                await db.pool.fetch(
                    "SELECT * FROM carry_cycle WHERE portfolio_id = $1",
                    portfolio_id,
                )
                == snapshot
            )


class TestClassification:
    async def test_held_symbols_carry_the_class_the_governed_universe_gives_them(
        self, database_url, db
    ):
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(db, portfolio_id, symbol="ETH/USDC")
            await _hold(
                db, portfolio_id, symbol="ETH/USDC:USDC", market_type="perpetual"
            )
            await _hold(db, portfolio_id, symbol="GLD", venue="questrade")
            await _hold(db, portfolio_id, symbol="SPY", venue="questrade")

            body = (await _read(client, token, "/trading/classification")).json()
            classes = {row["symbol"]: row["asset_class"] for row in body["symbols"]}
            assert classes == {
                "ETH/USDC": "crypto",
                "ETH/USDC:USDC": "crypto",
                "GLD": "defensive",
                "SPY": "stocks",
            }
            assert all(row["refusal"] is None for row in body["symbols"])
            assert body["classes"] == ["crypto", "defensive", "stocks"]

    async def test_a_symbol_the_universe_does_not_list_is_unclassified_not_a_stock(
        self, database_url, db
    ):
        """The defect this endpoint exists to remove.

        PURR trades on Hyperliquid and is not in the governed display universe.
        The frontend set this replaces returned `stocks` for it, which put a
        perpetual in the equity filter. Unclassified with a stated reason is the
        honest answer; a class is a measurement and nobody made this one.
        """
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(
                db, portfolio_id, symbol="PURR/USDC:USDC", market_type="perpetual"
            )

            body = (await _read(client, token, "/trading/classification")).json()
            assert len(body["symbols"]) == 1
            row = body["symbols"][0]
            assert row["asset"] == "PURR"
            assert row["asset_class"] is None
            assert "PURR" in row["refusal"]

    async def test_classification_is_private_to_the_account_that_owns_the_book(
        self, database_url, db
    ):
        async with _Lifespan(_app(database_url)) as app, TestClient(app) as client:
            token, _ = await _operator(client)
            other = await _second_user(client, token)
            theirs = await _portfolio(db, other)
            await _hold(db, theirs, symbol="ETH/USDC")

            r = await _read(
                client, token, f"/trading/classification?portfolio_id={theirs}"
            )
            assert r.status_code == 404, r.text

            anonymous = await client.get("/trading/classification")
            assert anonymous.status_code == 401, anonymous.text
