"""The two operational guards on a carry rebalance, against a real database.

`run_carry_cycle` decides what to trade. This decides *whether* and *when*, and
both halves have a failure mode that produces no error and no bad number:

- a window boundary read from a clock instead of the log skips settlements, and
  a skipped settlement silently understates the only thing this book earns;
- a boundary advanced past a cycle that halted before settling does the same
  thing by a subtler route -- the bookkeeping meant to prevent the gap creates
  it;
- a rebalance run at 14:00 UTC instead of 05:00 crosses the spread on both legs
  of every pair at 2.5x the variance, and that cost lands in the fill price
  where nothing reconciles it.

The venue is the real `PaperVenue`, so the cycles these tests record are cycles
that actually traded.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from omni.portfolio.state import create_portfolio
from omni.trading.carry_loop import CarryConfig, CarryHalt
from omni.trading.carry_runner import (
    GUARD_INSIDE_HOLD,
    GUARD_INSTANT_ALREADY_COVERED,
    GUARD_NO_RECORDED_CYCLE,
    GUARD_OUTSIDE_WINDOW,
    GUARDS,
    REBALANCE_PERIOD,
    CarryRunRefused,
    boundary,
    in_rebalance_window,
    run_due_cycle,
)
from omni.venue.paper_venue import Bar, PaperVenue, RecordedBars
from omni.venue.protocol import Capabilities

# 05:00 UTC: the trough of the measured volatility clock, and inside the window
# every happy path here has to clear.
NOW = datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
SETTLEMENT = timedelta(hours=8)
INCEPTION = NOW - timedelta(days=1)

VENUE = "paper"
FUNDING_VENUE = "binance"
PRICE = Decimal(100)
NOTIONAL = Decimal(1000)
TAKER_BPS = Decimal(5)
SPREAD_BPS = Decimal(4)

RATES = {
    "AAA/USD": "0.0006",
    "BBB/USD": "0.0005",
    "CCC/USD": "0.0004",
    "DDD/USD": "0.0003",
    "EEE/USD": "0.0002",
    "FFF/USD": "0.0001",
}

CAPABILITIES = Capabilities(
    spot=True,
    margin=False,
    perpetuals=True,
    limit_orders=True,
    shorting=True,
    funding_data=True,
    maker_fee_bps=Decimal(0),
    taker_fee_bps=TAKER_BPS,
    min_notional=Decimal(0),
)


def _config() -> CarryConfig:
    return CarryConfig(
        enter_rank=2,
        exit_rank=4,
        notional_per_pair=NOTIONAL,
        funding_venue=FUNDING_VENUE,
        spread_bps=SPREAD_BPS,
        reconciliation_tolerance=Decimal("0.01"),
        lookback_days=7,
    )


def _bars() -> RecordedBars:
    bars = RecordedBars()
    for symbol in RATES:
        bars.add(
            Bar(
                symbol=symbol,
                open=PRICE,
                high=Decimal(110),
                low=Decimal(90),
                close=PRICE,
                volume=Decimal(1000000),
                at=NOW - timedelta(days=30),
            )
        )
    return bars


@pytest.fixture
def venue() -> PaperVenue:
    return PaperVenue(
        _bars(),
        CAPABILITIES,
        name=VENUE,
        spread_bps=SPREAD_BPS,
        starting_balances={"USD": Decimal(100000)},
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, users CASCADE")
    yield


@pytest.fixture
async def owner(db) -> UUID:
    return await db.pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1,$2) RETURNING id",
        f"runner-{uuid4().hex}@omni.test",
        "not-a-real-hash",
    )


@pytest.fixture
async def portfolio_id(db, owner) -> UUID:
    book = await create_portfolio(
        db.pool,
        user_id=owner,
        name="carry book",
        base_currency="USD",
        opening_cash=Decimal(100000),
        cash_venue=VENUE,
    )
    return book.portfolio_id


async def _claim(db, entity_id, claim_type, key, value, at, audience, source):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, event_date, "
        "knowledge_date, confidence, redistributable, audience_user_id) "
        "VALUES ($1,$2::claim_type,$3,$4::jsonb,$5,$6,$6,1.0,'byo_only',$7)",
        entity_id,
        claim_type,
        key,
        json.dumps(value),
        source,
        at,
        audience,
    )


async def _world(db, audience) -> dict[str, UUID]:
    ids: dict[str, UUID] = {}
    for symbol, rate in RATES.items():
        entity_id = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('crypto_asset',$1,$1) "
            "RETURNING id",
            symbol,
        )
        ids[symbol] = entity_id
        for step in (2, 1):
            await _claim(
                db,
                entity_id,
                "funding_rate",
                f"{FUNDING_VENUE}:{symbol}",
                {"rate": str(rate), "venue": FUNDING_VENUE, "symbol": symbol},
                NOW - SETTLEMENT * step,
                audience,
                "derivatives",
            )
        await _claim(
            db,
            entity_id,
            "price_snapshot",
            f"{VENUE}:{symbol}",
            {"close": str(PRICE), "venue": VENUE},
            NOW - timedelta(days=30),
            audience,
            "prices",
        )
    return ids


async def _run(db, *, venue, portfolio_id, ids, owner, now=NOW, **overrides):
    return await run_due_cycle(
        db.pool,
        venue=venue,
        portfolio_id=portfolio_id,
        config=_config(),
        entity_ids=list(ids.values()),
        audience_user_id=owner,
        now=now,
        **overrides,
    )


async def _rows(db, portfolio_id):
    return await db.pool.fetch(
        "SELECT * FROM carry_cycle WHERE portfolio_id = $1 ORDER BY as_of",
        portfolio_id,
    )


async def _refusals(db, portfolio_id):
    return await db.pool.fetch(
        "SELECT * FROM carry_refusal WHERE portfolio_id = $1 ORDER BY attempted_at",
        portfolio_id,
    )


async def _record_a_cycle(
    db, portfolio_id, *, as_of, since=None, settled=True, halted=False
):
    """A cycle in the log without running one, for the cadence and boundary reads."""
    since = as_of - timedelta(days=1) if since is None else since
    await db.pool.execute(
        "INSERT INTO carry_cycle (portfolio_id, venue, as_of, funding_since, "
        "funding_settled_through, halted, halt_reason, abstention, "
        "funding_collected, fees_paid, modelled_turnover_cost, "
        "pairs_opened, pairs_closed, pairs_held) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,NULL,0,0,0,0,0,0)",
        portfolio_id,
        VENUE,
        as_of,
        since,
        as_of if settled else None,
        halted,
        "a_halt" if halted else None,
    )


class TestTheRebalanceWindow:
    """Step 4.5: the free improvement, enforced rather than documented."""

    @pytest.mark.parametrize("hour", [3, 4, 5, 6])
    def test_the_quiet_hours_are_in_the_window(self, hour):
        assert in_rebalance_window(datetime(2026, 3, 1, hour, tzinfo=UTC))

    @pytest.mark.parametrize("hour", [0, 2, 7, 13, 14, 15, 23])
    def test_everything_else_is_outside_it(self, hour):
        assert not in_rebalance_window(datetime(2026, 3, 1, hour, tzinfo=UTC))

    def test_the_window_is_read_in_utc_not_in_the_hosts_timezone(self):
        """14:00 UTC is the measured peak, and it is 06:00 in Los Angeles.

        A local-time comparison would admit the loudest hour of the day through
        the guard built to exclude it, on a machine whose timezone nothing in
        this system chose.
        """
        peak = datetime(2026, 3, 1, 14, tzinfo=UTC)
        assert not in_rebalance_window(peak)
        assert not in_rebalance_window(peak.astimezone(_LosAngeles()))

    def test_a_naive_instant_is_refused_rather_than_assumed_utc(self):
        with pytest.raises(ValueError, match="naive"):
            in_rebalance_window(datetime(2026, 3, 1, 5, 0))  # noqa: DTZ001

    async def test_a_cycle_outside_the_window_is_refused_and_the_book_is_untouched(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)

        with pytest.raises(CarryRunRefused, match="rebalance window"):
            await _run(
                db,
                venue=venue,
                portfolio_id=portfolio_id,
                ids=ids,
                owner=owner,
                now=NOW.replace(hour=14),
                inception=INCEPTION,
            )

        assert await _rows(db, portfolio_id) == []
        assert await db.pool.fetchval(
            "SELECT count(*) FROM position WHERE portfolio_id = $1", portfolio_id
        ) == 0

    async def test_the_window_can_be_overridden_but_only_by_name(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)

        result = await _run(
            db,
            venue=venue,
            portfolio_id=portfolio_id,
            ids=ids,
            owner=owner,
            now=NOW.replace(hour=14),
            inception=INCEPTION,
            ignore_window=True,
        )

        assert not result.halted
        assert len(await _rows(db, portfolio_id)) == 1


class TestTheFundingBoundary:
    async def test_a_book_with_no_recorded_cycle_refuses_to_invent_an_origin(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)

        with pytest.raises(CarryRunRefused, match="recorded no carry cycle"):
            await _run(db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner)

    async def test_a_settled_cycle_moves_the_boundary_to_its_own_instant(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)

        result = await _run(
            db,
            venue=venue,
            portfolio_id=portfolio_id,
            ids=ids,
            owner=owner,
            inception=INCEPTION,
        )

        assert result.funding_settled_through == NOW
        (row,) = await _rows(db, portfolio_id)
        assert row["funding_since"] == INCEPTION
        assert row["funding_settled_through"] == NOW
        assert (await boundary(db.pool, portfolio_id, VENUE)).opens_at == NOW

    async def test_a_cycle_that_halted_before_settling_does_not_move_the_boundary(
        self, db, owner, portfolio_id, venue
    ):
        """The gap the log exists to prevent, created by the log itself.

        A naked leg halts the cycle before the funding loop runs, so nothing in
        `(inception, NOW]` was applied. Recording NOW as settled would open the
        next window above every settlement in it, and no mechanism anywhere
        catches a gap -- `apply_funding` refuses a duplicate, not an absence.
        """
        ids = await _world(db, owner)
        await db.pool.execute(
            "INSERT INTO position (portfolio_id, venue, symbol, market_type, "
            "quantity, average_entry) VALUES ($1,$2,'CCC/USD','perpetual',$3,$4)",
            portfolio_id,
            VENUE,
            Decimal(-3),
            PRICE,
        )

        result = await _run(
            db,
            venue=venue,
            portfolio_id=portfolio_id,
            ids=ids,
            owner=owner,
            inception=INCEPTION,
        )

        assert result.halted
        assert CarryHalt.BOOK_NOT_PAIRED.value in result.halt_reason
        assert result.funding_settled_through is None

        (row,) = await _rows(db, portfolio_id)
        assert row["funding_settled_through"] is None
        assert row["halted"]
        # The origin is read back from the halted cycle's own lower bound, so
        # the operator does not have to restate it -- and cannot restate it
        # differently without that being visible in the log.
        assert (await boundary(db.pool, portfolio_id, VENUE)).opens_at == INCEPTION

    async def test_the_boundary_survives_a_halt_between_two_settled_cycles(
        self, db, owner, portfolio_id
    ):
        first = NOW - REBALANCE_PERIOD
        await _record_a_cycle(db, portfolio_id, as_of=first)
        await _record_a_cycle(
            db, portfolio_id, as_of=NOW, since=first, settled=False, halted=True
        )

        known = await boundary(db.pool, portfolio_id, VENUE)

        assert known.opens_at == first
        assert known.last_cycle == NOW
        assert known.last_completed == first

    async def test_one_venues_boundary_is_not_read_from_anothers(
        self, db, owner, portfolio_id
    ):
        await _record_a_cycle(db, portfolio_id, as_of=NOW, since=INCEPTION)

        assert (await boundary(db.pool, portfolio_id, "hyperliquid")).opens_at is None


class TestTheRebalanceCadence:
    async def test_a_second_cycle_inside_the_hold_is_refused_as_turnover(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)
        await _record_a_cycle(
            db, portfolio_id, as_of=NOW - timedelta(weeks=2)
        )

        with pytest.raises(CarryRunRefused, match="turnover"):
            await _run(db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner)

        assert len(await _rows(db, portfolio_id)) == 1

    async def test_a_cycle_after_the_hold_runs(self, db, owner, portfolio_id, venue):
        ids = await _world(db, owner)
        await _record_a_cycle(db, portfolio_id, as_of=NOW - REBALANCE_PERIOD)

        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner
        )

        assert not result.halted
        assert len(await _rows(db, portfolio_id)) == 2

    async def test_a_halted_cycle_does_not_start_the_clock(
        self, db, owner, portfolio_id, venue
    ):
        """A halt is a cycle that did not complete, and it is repaired by hand.

        Measuring the hold from it would lock the book out for six weeks after
        the one event that most needs a rerun.
        """
        ids = await _world(db, owner)
        await _record_a_cycle(
            db,
            portfolio_id,
            as_of=NOW - timedelta(days=1),
            since=INCEPTION,
            settled=False,
            halted=True,
        )

        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner
        )

        assert not result.halted
        assert result.funding_settled_through == NOW

    async def test_the_cadence_can_be_overridden_but_only_by_name(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)
        await _record_a_cycle(
            db, portfolio_id, as_of=NOW - timedelta(weeks=2)
        )

        result = await _run(
            db,
            venue=venue,
            portfolio_id=portfolio_id,
            ids=ids,
            owner=owner,
            ignore_cadence=True,
        )

        assert not result.halted

    async def test_a_cycle_at_an_instant_already_covered_is_refused(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)
        await _record_a_cycle(db, portfolio_id, as_of=NOW, since=INCEPTION)

        with pytest.raises(CarryRunRefused, match="not before"):
            await _run(
                db,
                venue=venue,
                portfolio_id=portfolio_id,
                ids=ids,
                owner=owner,
                now=NOW - timedelta(hours=1),
                ignore_cadence=True,
                ignore_window=True,
            )


class TestWhatIsRecorded:
    async def test_the_log_carries_what_the_cycle_did(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)

        result = await _run(
            db,
            venue=venue,
            portfolio_id=portfolio_id,
            ids=ids,
            owner=owner,
            inception=INCEPTION,
        )

        (row,) = await _rows(db, portfolio_id)
        assert row["venue"] == VENUE
        assert row["as_of"] == NOW
        assert row["pairs_opened"] == len(result.opened) == 2
        assert row["pairs_closed"] == len(result.closed) == 0
        assert row["pairs_held"] == len(result.held) == 2
        assert row["funding_collected"] == result.funding_collected
        assert row["fees_paid"] == result.fees_paid
        assert not row["halted"]
        assert row["halt_reason"] is None


class TestRefusalsAreRecorded:
    """The blind spot: a refused cycle used to write nothing at all.

    On a six-week hold the runner refuses on roughly 41 of every 42 days, and
    until this table existed each of those days produced no row anywhere. The
    absence was therefore ambiguous in the one direction that matters on a book
    holding real money: a correct refusal and a scheduler that never fired
    looked exactly alike. What is pinned here is that the row is written, that
    it carries the reason the caller was actually given, that it stays out of
    the cycle log, and that a failure to write it does not turn a normal
    refusal into a crash.
    """

    async def test_the_window_refusal_is_recorded_with_the_reason_the_caller_saw(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)

        with pytest.raises(CarryRunRefused) as refusal:
            await _run(
                db,
                venue=venue,
                portfolio_id=portfolio_id,
                ids=ids,
                owner=owner,
                now=NOW.replace(hour=14),
                inception=INCEPTION,
            )

        (row,) = await _refusals(db, portfolio_id)
        assert row["guard"] == GUARD_OUTSIDE_WINDOW
        assert row["reason"] == str(refusal.value) == refusal.value.reason
        assert row["attempted_at"] == NOW.replace(hour=14)
        assert row["venue"] == VENUE

    async def test_the_window_refusal_records_no_boundary_because_it_read_none(
        self, db, owner, portfolio_id, venue
    ):
        """The cheapest guard is deliberately first, so it refuses before the
        boundary is read. Filling those columns in anyway would report a state
        the runner never consulted; `guard` is what makes the NULLs legible."""
        ids = await _world(db, owner)
        await _record_a_cycle(db, portfolio_id, as_of=NOW - timedelta(days=1))

        with pytest.raises(CarryRunRefused):
            await _run(
                db,
                venue=venue,
                portfolio_id=portfolio_id,
                ids=ids,
                owner=owner,
                now=NOW.replace(hour=14),
            )

        (row,) = await _refusals(db, portfolio_id)
        assert row["guard"] == GUARD_OUTSIDE_WINDOW
        assert row["funding_window_opens_at"] is None
        assert row["last_cycle_at"] is None
        assert row["last_completed_at"] is None
        assert row["next_due_at"] is None

    async def test_the_hold_refusal_records_the_boundary_it_was_measured_against(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)
        last = NOW - timedelta(days=2)
        await _record_a_cycle(db, portfolio_id, as_of=last)

        with pytest.raises(CarryRunRefused) as refusal:
            await _run(db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner)

        (row,) = await _refusals(db, portfolio_id)
        assert row["guard"] == GUARD_INSIDE_HOLD
        assert row["reason"] == str(refusal.value)
        assert row["last_cycle_at"] == last
        assert row["last_completed_at"] == last
        assert row["next_due_at"] == last + REBALANCE_PERIOD
        assert row["funding_window_opens_at"] == last

    async def test_the_missing_origin_refusal_is_recorded(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)

        with pytest.raises(CarryRunRefused) as refusal:
            await _run(db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner)

        (row,) = await _refusals(db, portfolio_id)
        assert row["guard"] == GUARD_NO_RECORDED_CYCLE
        assert row["reason"] == str(refusal.value)
        assert row["last_cycle_at"] is None

    async def test_a_covered_instant_refusal_is_recorded(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)
        await _record_a_cycle(db, portfolio_id, as_of=NOW, since=INCEPTION)

        with pytest.raises(CarryRunRefused) as refusal:
            await _run(
                db,
                venue=venue,
                portfolio_id=portfolio_id,
                ids=ids,
                owner=owner,
                now=NOW - timedelta(hours=1),
                ignore_cadence=True,
                ignore_window=True,
            )

        (row,) = await _refusals(db, portfolio_id)
        assert row["guard"] == GUARD_INSTANT_ALREADY_COVERED
        assert row["reason"] == str(refusal.value)

    async def test_every_recorded_guard_is_one_the_module_declares(
        self, db, owner, portfolio_id, venue
    ):
        """A typo in a guard string is silent everywhere else: the row still
        writes, the reason still reads, and only a query grouping by guard ever
        notices -- by quietly splitting one guard into two."""
        ids = await _world(db, owner)

        for now in (NOW.replace(hour=14), NOW):
            with pytest.raises(CarryRunRefused):
                await _run(
                    db,
                    venue=venue,
                    portfolio_id=portfolio_id,
                    ids=ids,
                    owner=owner,
                    now=now,
                )

        rows = await _refusals(db, portfolio_id)
        assert len(rows) == 2
        assert {row["guard"] for row in rows} <= GUARDS
        assert {row["guard"] for row in rows} == {
            GUARD_OUTSIDE_WINDOW,
            GUARD_NO_RECORDED_CYCLE,
        }

    async def test_a_refusal_is_not_written_into_the_cycle_log(
        self, db, owner, portfolio_id, venue
    ):
        """A refusal traded nothing and settled nothing, so every money column
        on `carry_cycle` would be a zero standing in for an absence -- and the
        boundary read would treat it as a cycle the book actually ran."""
        ids = await _world(db, owner)

        with pytest.raises(CarryRunRefused):
            await _run(
                db,
                venue=venue,
                portfolio_id=portfolio_id,
                ids=ids,
                owner=owner,
                now=NOW.replace(hour=14),
                inception=INCEPTION,
            )

        assert await _rows(db, portfolio_id) == []
        assert len(await _refusals(db, portfolio_id)) == 1
        assert (await boundary(db.pool, portfolio_id, VENUE)).last_cycle is None

    async def test_a_cycle_that_ran_records_no_refusal(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)

        result = await _run(
            db,
            venue=venue,
            portfolio_id=portfolio_id,
            ids=ids,
            owner=owner,
            inception=INCEPTION,
        )

        assert not result.halted
        assert await _refusals(db, portfolio_id) == []

    async def test_the_same_instant_refused_twice_records_one_row(
        self, db, owner, portfolio_id, venue
    ):
        """The guards are deterministic in their inputs, so a re-run at the same
        instant reaches the same verdict. A second row would double-count the
        one thing this table is queried for -- whether the loop is firing."""
        ids = await _world(db, owner)
        at = NOW.replace(hour=14)

        for _ in range(2):
            with pytest.raises(CarryRunRefused):
                await _run(
                    db,
                    venue=venue,
                    portfolio_id=portfolio_id,
                    ids=ids,
                    owner=owner,
                    now=at,
                    inception=INCEPTION,
                )

        assert len(await _refusals(db, portfolio_id)) == 1

    async def test_a_refusal_that_cannot_be_recorded_is_still_a_refusal(
        self, db, owner, portfolio_id, venue, caplog
    ):
        """The write failing must not be reported as the cycle failing.

        `cycle_one.py` exits 2 on a refusal and 1 on a halt, and the operator
        reads those differently. A storage error surfacing in place of the
        refusal would turn the most common outcome on this book -- the correct
        one -- into a crash, on a day nothing was wrong with the book.
        """
        ids = await _world(db, owner)

        with (
            caplog.at_level(logging.ERROR, logger="omni.trading.carry"),
            pytest.raises(CarryRunRefused, match="rebalance window") as refusal,
        ):
            await _run(
                _FailingDb(db),
                venue=venue,
                portfolio_id=portfolio_id,
                ids=ids,
                owner=owner,
                now=NOW.replace(hour=14),
                inception=INCEPTION,
            )

        assert refusal.value.guard == GUARD_OUTSIDE_WINDOW
        assert await _refusals(db, portfolio_id) == []
        assert "could not record" in caplog.text
        assert GUARD_OUTSIDE_WINDOW in caplog.text


class _FailingDb:
    """`db`, with a pool whose `carry_refusal` insert fails and nothing else."""

    def __init__(self, db):
        self.pool = _FailingPool(db.pool)


class _FailingPool:
    def __init__(self, pool):
        self._pool = pool

    def __getattr__(self, name):
        return getattr(self._pool, name)

    async def execute(self, query, *args):
        if "carry_refusal" in query:
            raise RuntimeError("carry_refusal is unwritable in this test")
        return await self._pool.execute(query, *args)


def _LosAngeles():
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/Los_Angeles")
