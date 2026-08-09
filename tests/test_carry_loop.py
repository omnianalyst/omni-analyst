"""One rebalance cycle of the delta-neutral carry book, against a real database.

Every test here asserts an invariant of the *pair*, because the pair is the
whole strategy. A carry book asserts nothing about price; its only exposure is
supposed to be the funding a short perp receives, and every failure that matters
is a failure that quietly leaves it holding a direction instead:

- the perp leg opened LONG -- the book then PAYS the carry it was built to
  receive, and every total still reads as a plausible P&L;
- the two legs opened at different sizes -- the residual is delta one and
  nothing downstream knows it was meant to be half of something;
- one leg filled and the other did not -- the same naked residual, arriving by
  the more likely route;
- an abstention treated as a liquidation -- turnover is the one thing Finding 9
  measured as destroying this strategy outright;
- funding charged to both legs, or a settlement window that skips one -- the
  first double-counts the only thing this book earns and the second silently
  understates it.

The venue is the real `PaperVenue` against recorded bars. Where a leg has to
fail, `_RefusingVenue` wraps it rather than replacing it, so everything except
the refusal is still the real fill path.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from omni.conviction.crosssectional import (
    ABSTAIN_NO_COVERAGE,
    ABSTAIN_UNIVERSE_TOO_SMALL,
)
from omni.portfolio.reconcile import latest_by_venue
from omni.portfolio.state import (
    FundingOutcome,
    PortfolioState,
    create_portfolio,
    load,
    realised_pnl,
)
from omni.trading.carry_loop import (
    CarryConfig,
    CarryHalt,
    CarryRefusal,
    _legs_by_asset,
    _PairSymbols,
    _unpaired,
    run_carry_cycle,
)
from omni.venue.paper_venue import Bar, PaperVenue, RecordedBars
from omni.venue.protocol import (
    Capabilities,
    MarketType,
    Position,
    Side,
    TradeIntent,
    VenueUnavailable,
)

NOW = datetime(2026, 3, 1, tzinfo=UTC)
SETTLEMENT = timedelta(hours=8)

# Wide enough on both sides that no fill this file produces falls outside it.
# `realised_pnl` is being asked whether it can account for the book at all, and a
# window that clipped a fill would answer a different question.
SINCE = NOW - timedelta(days=365)
UNTIL = NOW + timedelta(days=365)

VENUE = "paper"
FUNDING_VENUE = "binance"
PRICE = Decimal(100)
NOTIONAL = Decimal(1000)

# Literals rather than the module's constants: the cost model's arithmetic is
# what is under test, and reading its inputs out of the code under test would
# let a mutation to that code mutate the expectation with it.
TAKER_BPS = Decimal(5)
SPREAD_BPS = Decimal(4)

# Six names ranked cleanly, so "the top two" is unambiguous. enter=2/exit=4
# needs a universe of at least max(exit+1, enter*2) = 5 to rank at all.
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


def _config(**overrides) -> CarryConfig:
    settings = {
        "enter_rank": 2,
        "exit_rank": 4,
        "notional_per_pair": NOTIONAL,
        "funding_venue": FUNDING_VENUE,
        "spread_bps": SPREAD_BPS,
        # Tight on purpose. A tolerance wide enough to absorb a real divergence
        # makes the reconciliation a formality that always passes, and every
        # test below would then be asserting over a check that cannot fail.
        "reconciliation_tolerance": Decimal("0.01"),
        "lookback_days": 7,
    }
    settings.update(overrides)
    return CarryConfig(**settings)


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, users CASCADE")
    yield


@pytest.fixture
async def owner(db) -> UUID:
    return await db.pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1,$2) RETURNING id",
        f"carry-{uuid4().hex}@omni.test",
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
                # Far above any size traded here, so the participation cap never
                # trims a leg -- a trimmed leg is a different failure and it has
                # its own test.
                volume=Decimal(1000000),
                at=NOW - timedelta(days=30),
            )
        )
    return bars


@pytest.fixture
def venue() -> PaperVenue:
    return PaperVenue(
        _bars(), CAPABILITIES, name=VENUE, spread_bps=SPREAD_BPS,
        starting_balances={"USD": Decimal(100000)},
    )


class _RefusingVenue:
    """The real paper venue with chosen intents made to fail at execution.

    A wrapper rather than a stand-in: everything the cycle does apart from the
    refusal still runs through the real fill, position and cash path, so a test
    of the unwind is a test of the unwind and not of a stub.
    """

    def __getattr__(self, name):
        # Delegate everything not deliberately overridden. Without this a
        # wrapper silently lacks whatever the cycle grows a use for next --
        # `positions` and `balances` when reconciliation landed -- and the test
        # fails with an AttributeError describing the harness rather than the
        # behaviour under test.
        return getattr(self._inner, name)

    def __init__(self, inner, refuse):
        self._inner = inner
        self._refuse = refuse

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def capabilities(self) -> Capabilities:
        return self._inner.capabilities

    @property
    def fills(self):
        return self._inner.fills

    async def execute(self, intent: TradeIntent):
        if self._refuse(intent):
            raise VenueUnavailable(f"refused {intent.market_type.value} {intent.side.value}")
        return await self._inner.execute(intent)


class _LedgerWatchingVenue:
    """The real venue, with the order ledger read from inside `execute`.

    The property under test is an ordering, and an ordering is invisible
    afterwards: a ledger written after the venue answered reads exactly like one
    written before it. The only place the difference exists is during the venue
    call, so the observation is taken there.
    """

    def __getattr__(self, name):
        # Delegate everything not deliberately overridden. Without this a
        # wrapper silently lacks whatever the cycle grows a use for next --
        # `positions` and `balances` when reconciliation landed -- and the test
        # fails with an AttributeError describing the harness rather than the
        # behaviour under test.
        return getattr(self._inner, name)

    def __init__(self, inner, pool):
        self._inner = inner
        self._pool = pool
        self.seen: list[tuple[str, str | None]] = []

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def capabilities(self) -> Capabilities:
        return self._inner.capabilities

    @property
    def fills(self):
        return self._inner.fills

    async def execute(self, intent: TradeIntent):
        status = await self._pool.fetchval(
            "SELECT status FROM trade_order WHERE idempotency_key = $1",
            intent.idempotency_key,
        )
        self.seen.append((intent.idempotency_key, status))
        return await self._inner.execute(intent)


async def _orders(db, portfolio_id):
    return await db.pool.fetch(
        "SELECT idempotency_key, symbol, market_type, side, status, quantity, "
        "filled_quantity, average_fill_price, fee_paid "
        "FROM trade_order WHERE portfolio_id = $1 ORDER BY created_at, id",
        portfolio_id,
    )


async def _entity(db, symbol: str) -> UUID:
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('crypto_asset',$1,$1) RETURNING id",
        symbol,
    )


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


async def _funding(db, entity_id, symbol, rate, at, audience, venue=FUNDING_VENUE):
    await _claim(
        db,
        entity_id,
        "funding_rate",
        f"{venue}:{symbol}",
        {"rate": str(rate), "venue": venue, "symbol": symbol},
        at,
        audience,
        "derivatives",
    )


async def _price(db, entity_id, symbol, price, at, audience):
    await _claim(
        db,
        entity_id,
        "price_snapshot",
        f"{VENUE}:{symbol}",
        {"close": str(price)},
        at,
        audience,
        "prices",
    )


async def _world(db, audience, *, rates=None, price_at=None) -> dict[str, UUID]:
    """Six names, two settlements each ending just before NOW, one price each.

    One price claim, dated well before anything, is enough: `_price_at` takes the
    last one knowable at the instant it is valuing, so a single early claim marks
    every settlement and every entry at exactly `PRICE` and the expected accrual
    is arithmetic rather than a snapshot.
    """
    rates = RATES if rates is None else rates
    ids: dict[str, UUID] = {}
    for symbol, rate in rates.items():
        entity_id = await _entity(db, symbol)
        ids[symbol] = entity_id
        for step in (2, 1):
            await _funding(db, entity_id, symbol, rate, NOW - SETTLEMENT * step, audience)
        if price_at is None or symbol in price_at:
            at = NOW - timedelta(days=30) if price_at is None else price_at[symbol]
            await _price(db, entity_id, symbol, PRICE, at, audience)
    return ids


async def _run(db, *, venue, portfolio_id, ids, owner, as_of, since, config=None, universe=None):
    return await run_carry_cycle(
        db.pool,
        venue=venue,
        portfolio_id=portfolio_id,
        config=config or _config(),
        entity_ids=list(ids.values()) if universe is None else universe,
        audience_user_id=owner,
        as_of=as_of,
        funding_since=since,
    )


async def _legs(db, portfolio_id, symbol):
    book = await load(db.pool, portfolio_id)
    return (
        book.position_for(VENUE, symbol, MarketType.SPOT),
        book.position_for(VENUE, symbol, MarketType.PERPETUAL),
    )


class TestThePairIsOneUnit:
    async def test_the_entered_names_open_long_spot_and_short_perp_at_one_quantity(
        self, db, owner, portfolio_id, venue
    ):
        """The sign and the size, which are the whole strategy.

        A LONG perp would make the book pay the funding it exists to receive, and
        the NAV would still look like an ordinary P&L while doing it. Two legs at
        different sizes leaves a residual that is delta one, and nothing
        downstream recognises it as half of anything.

        Both are asserted against the stored positions rather than against the
        result object, because the positions are what the risk engine, the
        reconciler and `apply_funding` all read.
        """
        ids = await _world(db, owner)

        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        assert result.abstention is None
        assert not result.halted
        # The two highest trailing rates, and nothing else.
        assert {p.entity_id for p in result.opened} == {ids["AAA/USD"], ids["BBB/USD"]}
        assert result.held == {ids["AAA/USD"], ids["BBB/USD"]}

        expected_quantity = NOTIONAL / PRICE
        for symbol in ("AAA/USD", "BBB/USD"):
            spot, perp = await _legs(db, portfolio_id, symbol)
            assert spot is not None and perp is not None
            assert spot.quantity == expected_quantity
            assert perp.quantity == -expected_quantity
            assert spot.quantity + perp.quantity == 0

        for symbol in ("CCC/USD", "DDD/USD", "EEE/USD", "FFF/USD"):
            assert await _legs(db, portfolio_id, symbol) == (None, None)

    async def test_a_leg_that_does_not_fill_takes_its_partner_back_off_the_book(
        self, db, owner, portfolio_id, venue
    ):
        """Refusing to proceed is not enough; the filled leg is already there.

        The spot leg fills and the perp leg is refused. Halting at that point
        would leave a long spot position the risk engine reads as an outright
        directional bet in a strategy that has no view on direction. The only
        action that restores neutrality is the opposite trade, so the cycle
        sends it and the book comes back flat.
        """
        ids = await _world(db, owner)
        refusing = _RefusingVenue(
            venue, lambda intent: intent.market_type is MarketType.PERPETUAL
        )

        result = await _run(
            db, venue=refusing, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        assert result.opened == ()
        assert result.held == frozenset()
        assert not result.halted
        assert result.refused[CarryRefusal.PAIR_DID_NOT_BALANCE.value] == 2
        for symbol in RATES:
            assert await _legs(db, portfolio_id, symbol) == (None, None)

    async def test_a_leg_that_cannot_be_unwound_halts_and_leaves_the_evidence(
        self, db, owner, portfolio_id, venue
    ):
        """When neutrality cannot be restored, the cycle stops rather than
        opening more pairs on top of a book that is already wrong.

        The naked leg is deliberately left in place. Deleting it is not available
        -- only a trade removes a position -- and a halt that also erased the
        record would leave an operator with nothing to act on.
        """
        ids = await _world(db, owner)
        refusing = _RefusingVenue(
            venue,
            lambda intent: intent.market_type is MarketType.PERPETUAL or intent.reduce_only,
        )

        result = await _run(
            db, venue=refusing, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        assert result.halted
        assert result.halt_reason is not None
        assert CarryHalt.UNWIND_FAILED.value in result.halt_reason
        assert result.opened == ()
        # Exactly one name was attempted: the halt stops the cycle rather than
        # working through the rest of the basket on top of a naked leg.
        opened_legs = [
            symbol for symbol in RATES if (await _legs(db, portfolio_id, symbol))[0] is not None
        ]
        assert len(opened_legs) == 1
        spot, perp = await _legs(db, portfolio_id, opened_legs[0])
        assert spot.quantity == NOTIONAL / PRICE
        assert perp is None

    async def test_a_book_already_holding_a_naked_leg_halts_before_it_trades(
        self, db, owner, portfolio_id, venue
    ):
        """Reconciliation-first, in the shape `loop.py` established.

        A divergence found after the cycle has sized forty positions against it
        has already compounded into all forty. The same is true of a naked leg:
        it is delta one, it is in NAV, and every size computed while it sits
        there is computed against a book that is wrong.
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
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        assert result.halted
        assert CarryHalt.BOOK_NOT_PAIRED.value in result.halt_reason
        assert "CCC/USD" in result.halt_reason
        assert result.opened == () and result.closed == ()
        assert venue.fills == []


class TestFunding:
    async def test_the_short_perp_receives_a_positive_rate(
        self, db, owner, portfolio_id, venue
    ):
        """The sign the whole strategy rests on, measured end to end.

        A positive funding rate means longs pay shorts, so the book -- short the
        perp precisely to be on the receiving side -- must end the cycle with
        MORE cash than it started it, by exactly `-quantity * mark * rate` per
        settlement. The amount is asserted exactly rather than as "positive": an
        inverted sign and a halved size both leave a positive number.
        """
        ids = await _world(db, owner)
        for symbol, entity_id in ids.items():
            await _funding(db, entity_id, symbol, RATES[symbol], NOW + SETTLEMENT, owner)

        await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )
        before = (await load(db.pool, portfolio_id)).cash

        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW + SETTLEMENT, since=NOW,
        )

        quantity = NOTIONAL / PRICE
        expected = sum(
            (quantity * PRICE * Decimal(RATES[symbol]) for symbol in ("AAA/USD", "BBB/USD")),
            Decimal(0),
        )
        assert expected > 0
        assert result.funding_collected == expected
        assert (await load(db.pool, portfolio_id)).cash - before == expected

    async def test_funding_settles_the_perp_leg_only(
        self, db, owner, portfolio_id, venue
    ):
        """The pair holds two legs and exactly one of them is fundable.

        Charging both would double the only income this book has, on the one
        strategy whose entire return is that income. One accrual per settlement,
        every one of them freshly ACCRUED -- a second application would come back
        ALREADY_SETTLED, which is the signature of a loop settling per leg rather
        than per pair.
        """
        ids = await _world(db, owner)
        for symbol, entity_id in ids.items():
            await _funding(db, entity_id, symbol, RATES[symbol], NOW + SETTLEMENT, owner)

        await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )
        spot_before, _ = await _legs(db, portfolio_id, "AAA/USD")

        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW + SETTLEMENT, since=NOW,
        )

        assert len(result.funding) == 2
        assert all(a.outcome is FundingOutcome.ACCRUED for a in result.funding)
        assert all(a.quantity == -(NOTIONAL / PRICE) for a in result.funding)
        rows = await db.pool.fetchval(
            "SELECT count(*) FROM funding_accrual WHERE portfolio_id = $1", portfolio_id
        )
        assert rows == 2

        spot_after, _ = await _legs(db, portfolio_id, "AAA/USD")
        assert spot_after.quantity == spot_before.quantity
        assert spot_after.average_entry == spot_before.average_entry

    async def test_consecutive_cycles_settle_every_period_exactly_once(
        self, db, owner, portfolio_id, venue
    ):
        """The window boundary, which fails silently in both directions.

        A settlement landing exactly on a rebalance instant belongs to the cycle
        that ends there -- `(since, as_of]`. An exclusive upper bound drops it
        from this cycle and the next cycle's exclusive lower bound drops it
        again, so it is collected by nobody and the carry is understated with
        nothing to show for it. An inclusive lower bound instead re-presents it,
        which the accrual key refuses but which reads back as ALREADY_SETTLED
        rather than as the fresh accrual a correct window produces.

        Asserted over two consecutive cycles whose boundary IS a settlement.
        """
        ids = await _world(db, owner)
        for symbol, entity_id in ids.items():
            for step in (1, 2):
                await _funding(
                    db, entity_id, symbol, RATES[symbol], NOW + SETTLEMENT * step, owner
                )

        await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )
        first = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW + SETTLEMENT, since=NOW,
        )
        second = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW + SETTLEMENT * 2, since=NOW + SETTLEMENT,
        )

        accruals = [*first.funding, *second.funding]
        assert sorted(a.funding_time for a in accruals) == [
            NOW + SETTLEMENT, NOW + SETTLEMENT,
            NOW + SETTLEMENT * 2, NOW + SETTLEMENT * 2,
        ]
        assert all(a.outcome is FundingOutcome.ACCRUED for a in accruals)
        assert first.funding_collected == second.funding_collected
        assert first.funding_collected > 0

    async def test_a_settlement_with_no_visible_mark_is_refused_not_valued(
        self, db, owner, portfolio_id, venue
    ):
        """The honest refusal, in the place a fabricated number would fit best.

        `apply_funding` requires a mark and will not invent one. Neither will
        this: with no price knowable at the settlement, the cycle records the
        refusal by name and accrues nothing for that name. Valuing it at the
        entry price instead would be a number nobody observed, applied three
        times a day for the life of the position.
        """
        ids = await _world(db, owner)
        for symbol, entity_id in ids.items():
            await _funding(db, entity_id, symbol, RATES[symbol], NOW + SETTLEMENT, owner)

        await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )
        await db.pool.execute(
            "DELETE FROM claim WHERE entity_id = $1 AND claim_type = 'price_snapshot'",
            ids["AAA/USD"],
        )

        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW + SETTLEMENT, since=NOW,
        )

        assert result.refused[CarryRefusal.NO_MARK.value] == 1
        assert [a.symbol for a in result.funding] == ["BBB/USD"]
        quantity = NOTIONAL / PRICE
        assert result.funding_collected == quantity * PRICE * Decimal(RATES["BBB/USD"])

    async def test_a_pair_dropped_from_the_universe_is_counted_not_silently_starved(
        self, db, owner, portfolio_id, venue
    ):
        """A name the universe stops naming can be neither ranked nor settled.

        The pair stays on the book -- nothing has told it to leave -- and stops
        collecting, which is the same silent understatement of carry as a skipped
        settlement except that it lasts for as long as the name is out. Selling
        it instead would be turnover the signal never asked for, so it is
        counted rather than acted on.
        """
        ids = await _world(db, owner)
        for symbol, entity_id in ids.items():
            await _funding(db, entity_id, symbol, RATES[symbol], NOW + SETTLEMENT, owner)

        await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW + SETTLEMENT, since=NOW,
            universe=[ids[s] for s in RATES if s != "AAA/USD"],
        )

        assert result.refused[CarryRefusal.OUTSIDE_UNIVERSE.value] == 1
        assert [a.symbol for a in result.funding] == ["BBB/USD"]
        assert ids["AAA/USD"] not in result.held
        spot, perp = await _legs(db, portfolio_id, "AAA/USD")
        assert spot.quantity == -perp.quantity == NOTIONAL / PRICE


class TestAbstention:
    async def test_an_abstention_holds_the_book_still(
        self, db, owner, portfolio_id, venue
    ):
        """Not rebalancing is the null action; selling is a trade nobody asked for.

        Finding 9 measured turnover taking a +8.74% gross strategy to -20.46%
        net at the fastest cadence. A book that liquidates whenever the ranking
        stops meaning anything pays that cost on exactly the cycles where there
        was no signal to act on.
        """
        ids = await _world(db, owner)
        await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )
        before = {symbol: await _legs(db, portfolio_id, symbol) for symbol in RATES}

        # Three names cannot support a rank that enters two and exits four.
        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW,
            universe=[ids["AAA/USD"], ids["BBB/USD"], ids["CCC/USD"]],
        )

        assert result.abstention == ABSTAIN_UNIVERSE_TOO_SMALL
        assert result.opened == () and result.closed == ()
        assert result.held == {ids["AAA/USD"], ids["BBB/USD"]}
        assert {symbol: await _legs(db, portfolio_id, symbol) for symbol in RATES} == before

    async def test_the_wrong_audience_sees_nothing_and_trades_nothing(
        self, db, owner, portfolio_id, venue
    ):
        """Funding is `byo_only`, so the audience is an access-control key.

        `audience_user_id=None` sees the shared network alone, which holds none
        of this. The failure is silent by construction -- zero rows read exactly
        like a quiet market -- so the assertion is that nothing traded, not that
        an error was raised.
        """
        ids = await _world(db, owner)

        result = await run_carry_cycle(
            db.pool,
            venue=venue,
            portfolio_id=portfolio_id,
            config=_config(),
            entity_ids=list(ids.values()),
            audience_user_id=None,
            as_of=NOW,
            funding_since=NOW - timedelta(days=1),
        )

        assert result.abstention == ABSTAIN_NO_COVERAGE
        assert result.opened == ()
        assert venue.fills == []


class TestRotation:
    async def test_a_name_leaving_the_exit_band_is_closed_as_a_pair(
        self, db, owner, portfolio_id, venue
    ):
        """Both legs go, or the exit leaves a naked one behind.

        Closing only the spot leg is the mirror of opening only one: the book is
        left short a perp with no hedge, which the position rows report as an
        ordinary short.
        """
        ids = await _world(db, owner)
        await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )
        assert (await _legs(db, portfolio_id, "AAA/USD"))[0] is not None

        # A later, disjoint window in which the two held names pay the least. A
        # one-day lookback at this instant sees only these settlements.
        later = NOW + timedelta(days=3)
        flipped = {
            "AAA/USD": "-0.0009", "BBB/USD": "-0.0008", "CCC/USD": "0.0009",
            "DDD/USD": "0.0008", "EEE/USD": "0.0007", "FFF/USD": "0.0006",
        }
        for symbol, entity_id in ids.items():
            for step in (2, 1):
                await _funding(
                    db, entity_id, symbol, flipped[symbol], later - SETTLEMENT * step, owner
                )

        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=later, since=NOW, config=_config(lookback_days=1),
        )

        assert {p.entity_id for p in result.closed} == {ids["AAA/USD"], ids["BBB/USD"]}
        assert {p.entity_id for p in result.opened} == {ids["CCC/USD"], ids["DDD/USD"]}
        assert result.held == {ids["CCC/USD"], ids["DDD/USD"]}
        for symbol in ("AAA/USD", "BBB/USD"):
            assert await _legs(db, portfolio_id, symbol) == (None, None)
        for symbol in ("CCC/USD", "DDD/USD"):
            spot, perp = await _legs(db, portfolio_id, symbol)
            assert spot.quantity == -perp.quantity == NOTIONAL / PRICE


class TestCostsAndClock:
    async def test_turnover_is_priced_by_the_cost_model(
        self, db, owner, portfolio_id, venue
    ):
        """The number Finding 9 says decides this strategy, taken from the model.

        Gross carry barely moves with cadence and cost collapses with it, so the
        whole result is a difference between two numbers of similar size. A
        hardcoded friction assumption -- 20bps, 30bps, whatever the last
        backtest used -- makes that difference describe the assumption rather
        than the venue.

        The expectation is built from this file's own constants, so a fee read
        from anywhere but the venue's declared capabilities fails it.
        """
        ids = await _world(db, owner)

        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        leg_bps = TAKER_BPS + SPREAD_BPS / 2
        expected = sum(
            (leg_bps / Decimal(10_000) * fill.notional for fill in venue.fills),
            Decimal(0),
        )
        assert len(venue.fills) == 4
        assert result.modelled_turnover_cost == expected
        assert result.fees_paid == sum(
            (fill.fee_paid for fill in venue.fills), Decimal(0)
        )
        assert result.fees_paid > 0

    async def test_the_cycle_refuses_a_clock_it_was_not_given(
        self, db, owner, portfolio_id, venue
    ):
        """`as_of` and `funding_since` are stated or the call does not happen.

        A replay that reads *now* is a replay with lookahead, and a funding
        window that defaults is a window that silently skips whatever the
        default did not cover.
        """
        ids = await _world(db, owner)
        call = lambda **kw: _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner, **kw
        )

        with pytest.raises(TypeError):
            await run_carry_cycle(
                db.pool,
                venue=venue,
                portfolio_id=portfolio_id,
                config=_config(),
                entity_ids=list(ids.values()),
                audience_user_id=owner,
                funding_since=NOW,
            )
        with pytest.raises(ValueError, match="naive"):
            await call(as_of=NOW.replace(tzinfo=None), since=NOW - timedelta(days=1))
        with pytest.raises(ValueError, match="naive"):
            await call(as_of=NOW, since=(NOW - timedelta(days=1)).replace(tzinfo=None))
        with pytest.raises(ValueError, match="after as_of"):
            await call(as_of=NOW, since=NOW + SETTLEMENT)

    async def test_a_spread_of_zero_must_be_asked_for(self):
        # The permissive value is the one that hands the strategy the spread on
        # every leg of its turnover, so it is stated rather than defaulted.
        with pytest.raises(TypeError):
            CarryConfig(
                enter_rank=2,
                exit_rank=4,
                notional_per_pair=NOTIONAL,
                funding_venue=FUNDING_VENUE,
            )
        with pytest.raises(TypeError, match="Decimal"):
            _config(notional_per_pair=1000.0)


class TestTheOrderLedger:
    """Every leg the cycle sends is an order, and the book replays from them.

    The reason is the kill switch. `portfolio.state.realised_pnl` derives its
    number by replaying the order ledger and refuses to return one the ledger
    cannot account for -- so a carry book whose fills went straight to the
    position rows raises `UnaccountedClose`, and `risk.check`'s daily-loss limit
    has no input at all on it. Every test here is a way that accounting can be
    broken while every other number in the result object still looks right.
    """

    async def test_a_cycle_leaves_a_book_the_ledger_can_account_for(
        self, db, owner, portfolio_id, venue
    ):
        """The headline: realised P&L is derivable after the cycle, not raising.

        Two pairs open and nothing closes, so the honest answer is exactly zero
        -- an open position realises nothing however far it has moved. The
        assertion that matters is that a number comes back at all: before the
        legs went through the ledger this call raised `UnaccountedClose` on any
        carry book that had traded, which is a daily-loss kill switch with no
        input.
        """
        ids = await _world(db, owner)

        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        assert len(result.opened) == 2
        assert await realised_pnl(
            db.pool, portfolio_id, since=SINCE, until=UNTIL
        ) == Decimal(0)

    async def test_the_order_exists_before_the_venue_is_asked_to_fill_it(
        self, db, owner, portfolio_id, venue
    ):
        """Recorded first, or the record is of something that already happened.

        A ledger written after the venue answered cannot survive the case it
        exists for: the process dies between the two, the venue has the position
        and nothing on our side ever instructed it. Read from inside `execute`,
        which is the only instant at which the two orderings differ.
        """
        ids = await _world(db, owner)
        watching = _LedgerWatchingVenue(venue, db.pool)

        await _run(
            db, venue=watching, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        assert len(watching.seen) == 4
        assert [status for _, status in watching.seen] == ["submitted"] * 4

    async def test_each_leg_is_accumulated_into_the_order_that_instructed_it(
        self, db, owner, portfolio_id, venue
    ):
        """The ledger carries what filled, not what was asked for.

        Asserted against the venue's own fills rather than against the requested
        quantity, because the two are the same only when nothing went wrong, and
        an order reporting the requested size on a partial fill misstates its own
        entry and every P&L derived from it.
        """
        ids = await _world(db, owner)

        await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        rows = await _orders(db, portfolio_id)
        assert len(rows) == 4
        assert {row["status"] for row in rows} == {"filled"}
        # Keyed on the intent the venue was handed, not on anything inferred
        # from the fill: the claim is that this order carries the fill it
        # instructed, and matching by symbol and side would still hold if two
        # orders had swapped their fills.
        ledger = {
            row["idempotency_key"]: (
                row["symbol"],
                row["side"],
                row["filled_quantity"],
                row["average_fill_price"],
                row["fee_paid"],
            )
            for row in rows
        }
        assert len(venue.fills) == 4
        for fill in venue.fills:
            assert ledger[fill.intent_id] == (
                fill.symbol,
                fill.side.value,
                fill.filled_quantity,
                fill.average_price,
                fill.fee_paid,
            )

    async def test_an_unwound_leg_is_in_the_ledger_and_can_be_valued(
        self, db, owner, portfolio_id, venue
    ):
        """The leg it is most tempting to skip and least survivable to skip.

        The perp is refused, the spot leg that already filled is bought back, and
        the book comes back flat. An unwind missing from the ledger leaves a
        replay holding a long the position rows do not, which is exactly the
        state `UnaccountedClose` refuses to value -- so the kill switch would go
        blind at the moment something had already gone wrong.

        The realised number is the round trip itself, computed from the venue's
        own fills: crossed twice and paid for twice, so it is a loss, and a
        signed assertion catches an unwind booked on the wrong side.
        """
        ids = await _world(db, owner)
        refusing = _RefusingVenue(
            venue, lambda intent: intent.market_type is MarketType.PERPETUAL
        )

        result = await _run(
            db, venue=refusing, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        assert result.held == frozenset()
        assert not result.halted

        rows = await _orders(db, portfolio_id)
        by_role = {}
        for row in rows:
            by_role.setdefault(row["idempotency_key"].rsplit(":", 1)[1], []).append(row)
        assert {role: len(found) for role, found in by_role.items()} == {
            "open": 4, "unwind": 2
        }
        assert {row["status"] for row in by_role["unwind"]} == {"filled"}
        # The refused perp legs are in the ledger too: an instruction that was
        # sent and turned down has a record of having been sent.
        assert sorted(row["status"] for row in by_role["open"]) == [
            "filled", "filled", "rejected", "rejected"
        ]

        buys = [f for f in venue.fills if f.side is Side.BUY]
        sells = [f for f in venue.fills if f.side is Side.SELL]
        assert len(buys) == len(sells) == 2
        expected = sum(
            (
                sell.filled_quantity * (sell.average_price - buy.average_price)
                - sell.fee_paid
                for buy, sell in zip(buys, sells, strict=True)
            ),
            Decimal(0),
        )
        assert expected < 0
        assert await realised_pnl(
            db.pool, portfolio_id, since=SINCE, until=UNTIL
        ) == expected

    async def test_an_unwind_is_a_separate_order_from_the_close_it_reverses(
        self, db, owner, portfolio_id, venue
    ):
        """Same symbol, same leg, same instant, both reduce_only -- one key apart.

        An exit whose spot leg is refused leaves the perp already covered, so the
        unwind re-shorts it. Nothing but the role distinguishes that intent from
        the close it is undoing, and a shared key would make the ledger read the
        second as a replay of the first: one order, one fill, and a position
        change with no instruction behind it.
        """
        ids = await _world(db, owner)
        await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        later = NOW + timedelta(days=3)
        flipped = {
            "AAA/USD": "-0.0009", "BBB/USD": "-0.0008", "CCC/USD": "0.0009",
            "DDD/USD": "0.0008", "EEE/USD": "0.0007", "FFF/USD": "0.0006",
        }
        for symbol, entity_id in ids.items():
            for step in (2, 1):
                await _funding(
                    db, entity_id, symbol, flipped[symbol], later - SETTLEMENT * step, owner
                )
        refusing = _RefusingVenue(
            venue,
            lambda intent: (
                intent.market_type is MarketType.SPOT and intent.side is Side.SELL
            ),
        )

        result = await _run(
            db, venue=refusing, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=later, since=NOW, config=_config(lookback_days=1),
        )

        assert result.closed == ()
        assert result.refused[CarryRefusal.PAIR_DID_NOT_BALANCE.value] == 2
        # The pair the exit could not take off is intact rather than half gone.
        for symbol in ("AAA/USD", "BBB/USD"):
            spot, perp = await _legs(db, portfolio_id, symbol)
            assert spot.quantity == -perp.quantity == NOTIONAL / PRICE

        stamp = later.isoformat()
        keys = {row["idempotency_key"] for row in await _orders(db, portfolio_id)}
        for symbol in ("AAA/USD", "BBB/USD"):
            assert f"{portfolio_id}:carry:{stamp}:{symbol}:perpetual:close" in keys
            assert f"{portfolio_id}:carry:{stamp}:{symbol}:perpetual:unwind" in keys

        # A short opened at the sell price and covered at the buy price loses the
        # crossing plus the fee on the covering leg; the two names that could not
        # exit are the only closes on the book.
        quantity = NOTIONAL / PRICE
        buy_price = next(f.average_price for f in venue.fills if f.side is Side.BUY)
        sell_price = next(f.average_price for f in venue.fills if f.side is Side.SELL)
        cover_fee = buy_price * quantity * TAKER_BPS / Decimal(10_000)
        expected = 2 * (quantity * (sell_price - buy_price) - cover_fee)
        assert expected < 0
        assert await realised_pnl(
            db.pool, portfolio_id, since=SINCE, until=UNTIL
        ) == expected


class TestTheVenueMustAgreeBeforeItTrades:
    """Reconciliation gates the cycle, the way `loop.py` gates the directional
    one.

    A book that does not match its venue is a book whose every size is computed
    against a position that may not exist. Trading on top of that compounds a
    disagreement instead of surfacing it, and the funding settlement is worse
    still: `apply_funding` is idempotent on `(portfolio, venue, symbol,
    funding_time)`, so carry credited against an unconfirmed position is the
    accrual that stands.
    """

    async def test_a_cash_divergence_halts_before_anything_is_traded(
        self, db, owner, portfolio_id
    ):
        ids = await _world(db, owner)
        # The venue holds a different cash figure from the book. Nothing else
        # differs, so a cycle that trades here is one that never looked.
        venue = PaperVenue(
            _bars(),
            CAPABILITIES,
            name=VENUE,
            spread_bps=SPREAD_BPS,
            starting_balances={"USD": Decimal(90_000)},
        )

        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        assert result.halted is True
        assert CarryHalt.VENUE_DISAGREES.value in result.halt_reason
        # Halted BEFORE trading, not after: an empty book is the proof, and the
        # detail names the asset so an operator has somewhere to look.
        book = await load(db.pool, portfolio_id)
        assert book.positions == ()
        assert "USD" in result.halt_reason

    async def test_the_verdict_is_recorded_even_though_it_halted(
        self, db, owner, portfolio_id
    ):
        ids = await _world(db, owner)
        """Recording only the passes deletes exactly the evidence an operator
        needs: the venue that diverged is the one whose history would then read
        `never_run`."""
        venue = PaperVenue(
            _bars(),
            CAPABILITIES,
            name=VENUE,
            spread_bps=SPREAD_BPS,
            starting_balances={"USD": Decimal(90_000)},
        )

        await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        stored = await latest_by_venue(db.pool, portfolio_id)
        assert VENUE in stored
        assert stored[VENUE].reconciled is False
        assert stored[VENUE].discrepancies

    async def test_an_agreeing_venue_does_not_halt(
        self, db, owner, portfolio_id, venue
    ):
        ids = await _world(db, owner)
        # The pair test: without this, a reconciler that refused everything
        # would satisfy both assertions above and stop the strategy dead.
        result = await _run(
            db, venue=venue, portfolio_id=portfolio_id, ids=ids, owner=owner,
            as_of=NOW, since=NOW - timedelta(days=1),
        )

        assert result.halted is False
        assert (await load(db.pool, portfolio_id)).positions != ()


def _book_with(*rows) -> PortfolioState:
    """A book holding exactly these positions, with no database.

    `_legs_by_asset` is pure over a PortfolioState, so its tests do not need
    one -- and building the book directly is what lets a test state two legs
    under two different symbols, which is the case a real venue always presents
    and no DB fixture here produces.
    """
    return PortfolioState(
        portfolio_id=uuid4(),
        nav=Decimal(0),
        cash=Decimal(0),
        positions=tuple(
            Position(
                venue=venue,
                symbol=symbol,
                market_type=market_type,
                quantity=quantity,
                average_entry=Decimal(100),
                as_of=NOW,
            )
            for venue, symbol, market_type, quantity in rows
        ),
        as_of=NOW,
    )


class TestTheTwoLegsCanHaveDifferentSymbols:
    """A real exchange does not name the two legs alike.

    ccxt spells the spot market `BTC/USDT` and the perpetual `BTC/USDT:USDT`.
    While the loop carried one symbol per pair it could not address both legs,
    so `CCXTVenue` could not drive it at all and only `PaperVenue` -- which
    returns the same string for both -- kept the paper path working.

    Grouping the book by symbol instead of by asset is the specific failure:
    a perfectly hedged pair whose legs carry two symbols reads as two broken
    ones and halts a cycle that should trade.
    """

    def test_a_pair_addresses_each_leg_by_its_own_symbol(self):
        pair = _PairSymbols(asset="BTC", spot="BTC/USDT", perp="BTC/USDT:USDT")

        assert pair.for_market(MarketType.SPOT) == "BTC/USDT"
        assert pair.for_market(MarketType.PERPETUAL) == "BTC/USDT:USDT"

    def test_a_hedged_pair_under_two_symbols_is_still_one_pair(self):
        """The halt this prevents. Both legs are present and offsetting; only
        their names differ."""
        book = _book_with(
            (VENUE, "BTC/USDT", MarketType.SPOT, Decimal(2)),
            (VENUE, "BTC/USDT:USDT", MarketType.PERPETUAL, Decimal(-2)),
        )
        asset_of = {"BTC/USDT": "BTC", "BTC/USDT:USDT": "BTC"}

        legs = _legs_by_asset(book, venue_name=VENUE, asset_of=asset_of)

        assert set(legs) == {"BTC"}
        assert legs["BTC"].is_pair is True
        assert _unpaired(legs) == []

    def test_grouping_by_symbol_would_have_read_it_as_two_naked_legs(self):
        # The same book with no asset mapping: each symbol stands alone, each
        # looks like half a pair, and the cycle halts on a hedged book.
        book = _book_with(
            (VENUE, "BTC/USDT", MarketType.SPOT, Decimal(2)),
            (VENUE, "BTC/USDT:USDT", MarketType.PERPETUAL, Decimal(-2)),
        )

        legs = _legs_by_asset(book, venue_name=VENUE, asset_of={})

        assert set(legs) == {"BTC/USDT", "BTC/USDT:USDT"}
        assert _unpaired(legs) == ["BTC/USDT", "BTC/USDT:USDT"]

    def test_a_position_the_cycle_cannot_map_still_halts(self):
        """A leg at this venue that this cycle does not recognise is exactly
        the directional exposure the pair gate exists to catch. Dropping it
        would make the book look clean by not looking at it."""
        book = _book_with(
            (VENUE, "BTC/USDT", MarketType.SPOT, Decimal(2)),
            (VENUE, "BTC/USDT:USDT", MarketType.PERPETUAL, Decimal(-2)),
            (VENUE, "DOGE/USDT", MarketType.SPOT, Decimal(100)),
        )
        asset_of = {"BTC/USDT": "BTC", "BTC/USDT:USDT": "BTC"}

        legs = _legs_by_asset(book, venue_name=VENUE, asset_of=asset_of)

        assert _unpaired(legs) == ["DOGE/USDT"]
