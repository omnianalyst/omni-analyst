"""The paper loop, tested for the order its steps run in.

Each module on this path has its own suite; nothing below re-tests Kelly, the
barrier swap, or the tolerance arithmetic. What only this layer can be wrong
about is *sequence* and *accounting*, so the weight sits on four things:

- **Reconciliation before consideration.** The divergence case asserts
  `considered == 0`, not `executed == 0`. A loop that reconciles at the end,
  or that reconciles first but keeps going, still executes nothing when the
  risk engine sees `reconciled=False` -- so `executed == 0` passes for the
  broken implementation and only the considered count separates them.
- **The histogram accounting for every candidate.** A refusal that is dropped
  rather than counted is invisible in every other assertion in this file.
- **The ceiling binding before the venue is called**, with limits deliberately
  loosened so that all ten candidates clear risk and the only thing that can
  stop the last seven is the ceiling itself.
- **Refusals sitting next to the smallest variation that must be allowed.**
  The walk-forward case runs the same cycle twice, once with the method absent
  from the mapping and once with it present, so a loop that substituted `True`
  fails the first half and a loop that refused everything fails the second.
- **The reconciliation being recorded on both outcomes, and recording nothing
  it decides.** Each of those is asserted against its opposite: the divergence
  is checked for a stored row as well as a halt, and the halt is checked with
  the write forced to fail, because "record on success, halt on failure" passes
  every assertion that looks at only one of the two.

Numbers are chosen so the sizing is exact rather than approximately right:
entry 100, barriers 120/90 and a 0.6 calibrated hit rate give b = 2,
f* = (0.6*2 - 0.4)/2 = 0.4, quarter Kelly 0.1, and 0.1 of a 100,000 NAV at 100
is exactly 100 units.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from omni.portfolio.orders import OrderLedgerError
from omni.portfolio.reconcile import Divergence
from omni.portfolio.risk import RiskLimits, RiskRefusal
from omni.trading.bridge import BridgeRefusal
from omni.trading.loop import CycleResult, LoopConfig, LoopRefusal, run_cycle
from omni.trading.policy import Ineligible, TradingPhase
from omni.venue.paper_venue import Bar, PaperVenue, RecordedBars
from omni.venue.protocol import Capabilities, Fill, MarketType, Position

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
HORIZON = NOW + timedelta(days=30)
METHOD = "trend.sma"
KIND = "crypto_asset"
NAV = Decimal(100_000)
EXPECTED_QUANTITY = Decimal(100)

_PROVENANCE = json.dumps({"capability": METHOD, "input_claims": [], "assumptions": {}})


@pytest_asyncio.fixture(autouse=True)
async def _isolated(db):
    """The loop scans every pending prediction, so leftovers change its answers.

    Truncating is safe only because this suite runs against its own database
    (`TEST_DATABASE_URL`); `entity` is left alone because migration 043 seeds a
    row into it that would not come back.
    """
    await db.pool.execute(
        "TRUNCATE prediction, trade_order, order_event, position, cash_balance, "
        "nav_snapshot, portfolio CASCADE"
    )
    yield


@pytest_asyncio.fixture
async def portfolio(db):
    portfolio_id = await db.pool.fetchval(
        "INSERT INTO portfolio (name, base_currency) VALUES ($1, $2) RETURNING id",
        "trading-loop",
        "USD",
    )
    await db.pool.execute(
        "INSERT INTO cash_balance (portfolio_id, venue, asset, free) "
        "VALUES ($1, $2, $3, $4)",
        portfolio_id,
        "paper",
        "USD",
        NAV,
    )
    return portfolio_id


async def _entity(db, kind: str = KIND) -> tuple[object, str]:
    symbol = f"{uuid4().hex[:8].upper()}/USD"
    entity_id = await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ($1, $2, $2) RETURNING id",
        kind,
        symbol,
    )
    return entity_id, symbol


async def _calibrate(db, entity_id, *, method: str = METHOD, n: int = 40, hits: int = 24):
    """Resolved history so `policy.eligible` has a hit rate to read.

    All at confidence 0.85, so one decile bucket carries the whole sample and
    `measured_n` equals `n` -- 24 of 40 is a 0.6 hit rate over a sample that
    clears the 30 the paper phase requires.
    """
    await db.pool.execute(
        """
        INSERT INTO prediction (entity_id, method, direction, confidence,
                                entry_price, upper_barrier, lower_barrier,
                                horizon_ends_at, provenance, outcome, resolved_at)
        SELECT $1, $2, 'up', 0.85, 100, 120, 90, $3, $4::jsonb,
               (CASE WHEN g <= $5 THEN 'upper' ELSE 'lower' END)::prediction_outcome,
               $6
        FROM generate_series(1, $7) AS g
        """,
        entity_id,
        method,
        HORIZON,
        _PROVENANCE,
        hits,
        NOW,
        n,
    )


async def _pending(
    db,
    entity_id,
    *,
    method: str = METHOD,
    direction: str = "up",
    created_at: datetime | None = None,
):
    return await db.pool.fetchval(
        """
        INSERT INTO prediction (entity_id, method, direction, confidence,
                                entry_price, upper_barrier, lower_barrier,
                                horizon_ends_at, provenance, created_at)
        VALUES ($1, $2, $3::prediction_direction, 0.85, 100, 120, 90, $4, $5::jsonb, $6)
        RETURNING id
        """,
        entity_id,
        method,
        direction,
        HORIZON,
        _PROVENANCE,
        created_at or (NOW - timedelta(minutes=1)),
    )


def _caps(**overrides) -> Capabilities:
    base = {
        "spot": True,
        "margin": False,
        "perpetuals": False,
        "limit_orders": True,
        "shorting": False,
        "funding_data": False,
        "maker_fee_bps": Decimal(2),
        "taker_fee_bps": Decimal(10),
        "min_notional": Decimal(10),
    }
    return Capabilities(**{**base, **overrides})


def _venue(*symbols: str, capabilities: Capabilities | None = None) -> PaperVenue:
    bars = RecordedBars()
    for symbol in symbols:
        bars.add(
            Bar(
                symbol=symbol,
                open=Decimal(100),
                high=Decimal(105),
                low=Decimal(95),
                close=Decimal(100),
                volume=Decimal(10_000),
                at=NOW,
            )
        )
    # Seeded with the cash the portfolio believes it holds. Before the paper
    # venue moved cash at all this was irrelevant; now that the loop reconciles
    # cash, a venue reporting no USD against a book holding 100,000 is a real
    # divergence and halting on it is correct. Starting them equal is what a
    # paper account actually is.
    return PaperVenue(
        bars,
        capabilities or _caps(),
        starting_balances={"USD": NAV},
    )


def _limits(**overrides) -> RiskLimits:
    base = {
        "max_position_pct_nav": Decimal("0.20"),
        "max_gross_exposure_pct_nav": Decimal("0.50"),
        "max_net_exposure_pct_nav": Decimal("0.30"),
        "max_positions": 5,
        "max_correlated_exposure_pct_nav": Decimal("0.30"),
        "correlation_threshold": Decimal("0.60"),
        "min_notional": Decimal(100),
        "max_notional": Decimal(50_000),
        "daily_loss_limit_pct_nav": Decimal("0.02"),
        "max_drawdown_pct": Decimal("0.10"),
        "max_data_age": timedelta(minutes=5),
    }
    return RiskLimits(**{**base, **overrides})


def _roomy_limits(**overrides) -> RiskLimits:
    """Limits wide enough that only the thing under test can refuse."""
    return _limits(
        max_position_pct_nav=Decimal("0.50"),
        max_gross_exposure_pct_nav=Decimal(1),
        max_net_exposure_pct_nav=Decimal(1),
        max_correlated_exposure_pct_nav=Decimal(1),
        max_positions=20,
        **overrides,
    )


def _config(**overrides) -> LoopConfig:
    base = {
        "phase": TradingPhase.PAPER,
        "target_hit_rate": 0.55,
        "tolerance": Decimal("0.000001"),
        "max_intents_per_cycle": 5,
        "market_type": MarketType.SPOT,
        "limits": _limits(),
        # The gate's risk parameters. Stated here rather than defaulted for the
        # same reason policy.eligible refuses to default them: a caller that
        # never thought about the cost of a round trip is exactly the caller
        # whose edge does not survive one. These are the loop's own baseline,
        # loosened deliberately -- min_effective_n=1 and max_concentration=1 --
        # because these fixtures test the LOOP's control flow, and a fixture
        # holding one name on one date would otherwise fail the gate's sample-
        # shape checks and turn every test here into a policy test.
        "round_trip_cost_bps": Decimal(20),
        # A positive minimum, not a disabled one: the gate refuses a
        # non-positive bar outright, and it is right to -- a strategy that does
        # not clear its own cost model's error bar has not shown anything. Set
        # low so these fixtures clear it on their geometry alone.
        "min_expectancy_bps": Decimal("0.01"),
        "min_effective_n": 1,
        "max_assumed_share": Decimal(1),
        "max_concentration": Decimal(1),
    }
    return LoopConfig(**{**base, **overrides})


async def _cycle(db, portfolio, venue, *, config=None, walk_forward=None, **kw):
    return await run_cycle(
        db.pool,
        venue=venue,
        portfolio_id=portfolio,
        config=config or _config(),
        walk_forward_results={METHOD: True} if walk_forward is None else walk_forward,
        now=NOW,
        realised_pnl_today=kw.pop("realised_pnl_today", Decimal(0)),
        **kw,
    )


async def _reconciliations(db, portfolio):
    return await db.pool.fetch(
        "SELECT id, venue, reconciled, checked_at FROM reconciliation_result "
        "WHERE portfolio_id = $1 ORDER BY recorded_at, id",
        portfolio,
    )


async def _discrepancies(db, result_id):
    return await db.pool.fetch(
        "SELECT kind, venue, symbol, detail FROM reconciliation_discrepancy "
        "WHERE result_id = $1 ORDER BY seq",
        result_id,
    )


async def _reconciliation_alert(db, portfolio, *, venue: str = "paper"):
    """One configured reconciliation alert, so the alert pass has something to run.

    `threshold` is required by the schema and meaningless to this kind; the hour
    of staleness tolerance is far wider than the zero age of a result stamped
    with the cycle's own clock, so nothing here can fire for being old.
    """
    return await db.pool.fetchval(
        "INSERT INTO risk_alert (portfolio_id, kind, threshold, venue, stale_after) "
        "VALUES ($1, 'reconciliation', $2, $3, $4) RETURNING id",
        portfolio,
        Decimal("0.01"),
        venue,
        timedelta(hours=1),
    )


async def _open_firings(db, alert_id):
    return await db.pool.fetch(
        "SELECT subject, reason, detail FROM risk_alert_firing "
        "WHERE alert_id = $1 AND cleared_at IS NULL ORDER BY subject, reason",
        alert_id,
    )


def _write_fails(monkeypatch, message: str = "the reconciliation write went nowhere"):
    async def _boom(*args, **kwargs):
        raise RuntimeError(message)

    # `record` moved to `trading.pretrade`, which both loops share so the
    # directional and carry books cannot disagree about what a failed
    # reconciliation write means. Patching the old name would no longer
    # reach anything, and the test would pass by not exercising the path.
    monkeypatch.setattr("omni.trading.pretrade.record", _boom)
    return message


async def _orders(db, portfolio):
    return await db.pool.fetch(
        "SELECT id, status, side, quantity, filled_quantity, average_fill_price, "
        "fee_paid, idempotency_key FROM trade_order WHERE portfolio_id = $1 "
        "ORDER BY created_at",
        portfolio,
    )


class TestTheHappyPath:
    async def test_a_calibrated_prediction_becomes_a_position_through_the_ledger(
        self, db, portfolio
    ):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        prediction_id = await _pending(db, entity_id)
        venue = _venue(symbol)

        result = await _cycle(db, portfolio, venue)

        assert result.halted is False
        assert result.halt_reason is None
        assert result.considered == 1
        assert result.executed == 1
        assert result.refused == {}

        (fill,) = result.fills
        assert fill.symbol == symbol
        assert fill.filled_quantity == EXPECTED_QUANTITY

        (order,) = await _orders(db, portfolio)
        assert order["status"] == "filled"
        assert order["side"] == "buy"
        assert order["quantity"] == EXPECTED_QUANTITY
        assert order["filled_quantity"] == EXPECTED_QUANTITY
        assert order["average_fill_price"] == fill.average_price
        assert order["fee_paid"] == fill.fee_paid
        assert order["idempotency_key"] == f"{portfolio}:{prediction_id}"

        events = await db.pool.fetch(
            "SELECT status FROM order_event WHERE order_id = $1 ORDER BY at, id",
            order["id"],
        )
        assert [e["status"] for e in events] == ["intent", "submitted", "filled"]

        (position,) = await db.pool.fetch(
            "SELECT symbol, market_type, quantity, average_entry FROM position "
            "WHERE portfolio_id = $1",
            portfolio,
        )
        assert position["symbol"] == symbol
        assert position["market_type"] == "spot"
        assert position["quantity"] == EXPECTED_QUANTITY
        assert position["average_entry"] == fill.average_price

        cash = await db.pool.fetchval(
            "SELECT free FROM cash_balance WHERE portfolio_id = $1 AND venue = 'paper'",
            portfolio,
        )
        assert cash == NAV - fill.notional - fill.fee_paid

    async def test_the_intent_carries_the_prediction_that_motivated_it(
        self, db, portfolio
    ):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        prediction_id = await _pending(db, entity_id)

        await _cycle(db, portfolio, _venue(symbol))

        provenance = await db.pool.fetchval(
            "SELECT provenance FROM trade_order WHERE portfolio_id = $1", portfolio
        )
        recorded = json.loads(provenance) if isinstance(provenance, str) else provenance
        assert recorded["prediction_id"] == str(prediction_id)
        assert recorded["method"] == METHOD
        assert recorded["direction"] == "up"
        assert recorded["hit_rate"] == pytest.approx(0.6)


class _DivergentVenue:
    """A venue reporting a holding the book has no row for, and counting calls."""

    def __init__(self, inner: PaperVenue, ghost: Position) -> None:
        self.name = inner.name
        self.capabilities = inner.capabilities
        self._inner = inner
        self._ghost = ghost
        self.execute_calls: list[object] = []

    async def quote(self, intent):
        return await self._inner.quote(intent)

    async def execute(self, intent):
        self.execute_calls.append(intent)
        return await self._inner.execute(intent)

    async def positions(self):
        return [self._ghost]

    async def balances(self):
        return []

    async def cancel(self, external_id):
        return False


class _OverfillingVenue:
    def __init__(self, inner: PaperVenue) -> None:
        self.name = inner.name
        self.capabilities = inner.capabilities
        self._inner = inner

    async def quote(self, intent):
        return await self._inner.quote(intent)

    async def execute(self, intent):
        return Fill(
            intent_id=intent.idempotency_key,
            venue=self.name,
            symbol=intent.symbol,
            side=intent.side,
            filled_quantity=intent.quantity + Decimal(1),
            average_price=intent.reference_price,
            fee_paid=Decimal("1.01"),
            filled_at=NOW,
            external_id="OVER-1",
        )

    async def positions(self):
        return await self._inner.positions()

    async def balances(self):
        return await self._inner.balances()

    async def cancel(self, external_id):
        return False


class TestOverfillsStopBeforePortfolioMutation:
    async def test_an_overfill_does_not_change_local_position_or_cash(self, db, portfolio):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id)

        with pytest.raises(OrderLedgerError, match="overfill"):
            await _cycle(db, portfolio, _OverfillingVenue(_venue(symbol)))

        assert await db.pool.fetchval(
            "SELECT count(*) FROM position WHERE portfolio_id = $1", portfolio
        ) == 0
        assert await db.pool.fetchval(
            "SELECT free FROM cash_balance "
            "WHERE portfolio_id = $1 AND venue = 'paper' AND asset = 'USD'",
            portfolio,
        ) == NAV

        (order,) = await _orders(db, portfolio)
        assert order["status"] == "submitted"
        assert order["filled_quantity"] == Decimal(0)
        assert order["average_fill_price"] is None
        assert order["fee_paid"] == Decimal(0)
        events = await db.pool.fetch(
            "SELECT status FROM order_event WHERE order_id = $1 ORDER BY at, id",
            order["id"],
        )
        assert [event["status"] for event in events] == ["intent", "submitted"]


class TestReconciliationGatesTheCycle:
    async def test_a_divergence_halts_before_a_single_prediction_is_considered(
        self, db, portfolio
    ):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id)
        ghost = Position(
            venue="paper",
            symbol=symbol,
            market_type=MarketType.SPOT,
            quantity=Decimal(5),
            average_entry=Decimal(100),
            as_of=NOW,
        )
        venue = _DivergentVenue(_venue(symbol), ghost)

        result = await _cycle(db, portfolio, venue)

        # The load-bearing assertion: not merely that nothing executed, but
        # that nothing was even looked at. A loop that reconciles last, or that
        # reconciles first and carries on, also executes nothing here.
        assert result.considered == 0
        assert result.executed == 0
        assert result.refused == {}
        assert result.halted is True
        assert symbol in result.halt_reason
        assert venue.execute_calls == []
        assert await _orders(db, portfolio) == []

    async def test_the_same_cycle_runs_once_the_venue_agrees(self, db, portfolio):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id)

        result = await _cycle(db, portfolio, _venue(symbol))

        assert result.halted is False
        assert result.considered == 1
        assert result.executed == 1


def _ghost(symbol: str) -> Position:
    """A holding the venue reports and the book has no row for."""
    return Position(
        venue="paper",
        symbol=symbol,
        market_type=MarketType.SPOT,
        quantity=Decimal(5),
        average_entry=Decimal(100),
        as_of=NOW,
    )


class TestTheVerdictIsRecordedWhicheverWayItWent:
    """Recording is a side effect of checking: taken on both outcomes, deciding neither.

    The pair is the point. Storing only the passes satisfies a happy-path
    assertion and deletes the single reading an operator has to act on -- the
    venue that diverged is then the venue whose history reads `never_run`, which
    is the answer given for a check that never happened.
    """

    async def test_a_pass_is_stored_as_a_pass(self, db, portfolio):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id)

        result = await _cycle(db, portfolio, _venue(symbol))

        assert result.executed == 1
        (stored,) = await _reconciliations(db, portfolio)
        assert stored["venue"] == "paper"
        assert stored["reconciled"] is True
        # The cycle's clock, not the write's: a result recorded after a retry is
        # evidence about the moment the books were compared.
        assert stored["checked_at"] == NOW
        assert await _discrepancies(db, stored["id"]) == []

    async def test_a_divergence_is_stored_with_the_evidence_the_halt_named(
        self, db, portfolio
    ):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id)
        venue = _DivergentVenue(_venue(symbol), _ghost(symbol))

        result = await _cycle(db, portfolio, venue)

        assert result.halted is True
        (stored,) = await _reconciliations(db, portfolio)
        assert stored["reconciled"] is False
        assert stored["checked_at"] == NOW

        evidence = await _discrepancies(db, stored["id"])
        assert Divergence.POSITION_MISSING_LOCALLY.value in {d["kind"] for d in evidence}
        assert symbol in {d["symbol"] for d in evidence}
        # What is on record is what the halt acted on, line for line. A store
        # holding a divergence the operator was told nothing about, or a halt
        # naming one the store cannot show, is the two drifting apart.
        for discrepancy in evidence:
            assert discrepancy["detail"] in result.halt_reason


class TestAFailedWriteLosesTheRecordAndNeverTheVerdict:
    """The write cannot move the verdict, in either direction.

    A divergence already found stays a halt when the write fails, because the
    alternative converts a divergence into a pass -- and a pass stays a pass,
    because a bookkeeping failure is not evidence about the books. What the
    failure does instead is surface: named in the halt reason when there is one,
    logged with its traceback either way, and visible to the alert pass, which
    reads the store and therefore sees an unrecordable verdict as a venue nobody
    has checked.
    """

    async def test_a_divergence_still_halts_when_the_write_fails(
        self, db, portfolio, monkeypatch
    ):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id)
        venue = _DivergentVenue(_venue(symbol), _ghost(symbol))
        message = _write_fails(monkeypatch)

        result = await _cycle(db, portfolio, venue)

        assert result.halted is True
        assert result.considered == 0
        assert symbol in result.halt_reason
        assert message in result.halt_reason
        assert venue.execute_calls == []
        assert await _orders(db, portfolio) == []
        # The write genuinely failed, so the assertions above are about the
        # verdict surviving rather than about a store that quietly worked.
        assert await _reconciliations(db, portfolio) == []

    async def test_a_pass_still_runs_when_the_write_fails_and_the_failure_is_not_hidden(
        self, db, portfolio, monkeypatch, caplog
    ):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id)
        alert_id = await _reconciliation_alert(db, portfolio)
        _write_fails(monkeypatch)

        # The failure is now logged by `trading.pretrade`, which owns the
        # write. Watching the old logger would capture nothing and the
        # assertion below would fail for the right reason by accident.
        with caplog.at_level(logging.ERROR, logger="omni.trading.pretrade"):
            result = await _cycle(db, portfolio, _venue(symbol))

        assert result.halted is False
        assert result.executed == 1
        assert await _reconciliations(db, portfolio) == []

        reported = [
            r
            for r in caplog.records
            if r.name == "omni.trading.pretrade" and r.levelno >= logging.ERROR
        ]
        assert reported, "a write that failed and was never reported is a swallowed one"
        assert reported[0].exc_info is not None

        # And it surfaces where an operator is looking: the alert reads the
        # store, so a verdict nobody can read back is an unchecked venue rather
        # than a passing one.
        (firing,) = await _open_firings(db, alert_id)
        assert firing["subject"] == "paper"
        assert firing["reason"] == RiskRefusal.RECONCILIATION_UNKNOWN.value


class TestTheAlertPassIsGivenTheReconciliations:
    """`alerts.evaluate` is wired to the stored results, not left to default.

    Its `reconciliations` argument has no default precisely so that a caller
    which never wired it cannot be mistaken for a healthy one: `None`, `{}` and
    a mapping missing the venue all read as `RECONCILIATION_UNKNOWN`. So the two
    cases below pin the wiring from both sides -- an unwired loop reports the
    divergence as unknown and breaks the first, and reports a clean venue as
    unknown and breaks the second, while a loop that never evaluates alerts at
    all breaks the first.
    """

    async def test_a_divergence_opens_an_episode_naming_what_diverged(
        self, db, portfolio
    ):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id)
        alert_id = await _reconciliation_alert(db, portfolio)
        venue = _DivergentVenue(_venue(symbol), _ghost(symbol))

        result = await _cycle(db, portfolio, venue)

        assert result.halted is True
        (firing,) = await _open_firings(db, alert_id)
        assert firing["subject"] == "paper"
        assert firing["reason"] == RiskRefusal.RECONCILIATION_DIVERGENCE.value
        # The stored evidence reached the alert, not merely the boolean.
        assert Divergence.POSITION_MISSING_LOCALLY.value in firing["detail"]

    async def test_a_recorded_pass_leaves_the_alert_silent(self, db, portfolio):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id)
        alert_id = await _reconciliation_alert(db, portfolio)

        result = await _cycle(db, portfolio, _venue(symbol))

        assert result.executed == 1
        assert await _open_firings(db, alert_id) == []


class TestRefusalsAreNamedAndCounted:
    async def test_an_ineligible_method_is_counted_by_its_policy_reason(
        self, db, portfolio
    ):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id, n=5, hits=5)
        await _pending(db, entity_id)

        result = await _cycle(db, portfolio, _venue(symbol))

        assert result.considered == 1
        assert result.executed == 0
        assert result.refused == {Ineligible.UNCALIBRATED.value: 1}
        assert await _orders(db, portfolio) == []
        assert await db.pool.fetchval(
            "SELECT count(*) FROM position WHERE portfolio_id = $1", portfolio
        ) == 0

    async def test_a_method_absent_from_walk_forward_refuses_rather_than_trades(
        self, db, portfolio
    ):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id)
        venue = _venue(symbol)

        absent = await _cycle(db, portfolio, venue, walk_forward={})

        assert absent.executed == 0
        assert absent.refused == {Ineligible.NO_WALK_FORWARD.value: 1}
        assert await _orders(db, portfolio) == []

        # The smallest variation that must be allowed: the identical cycle with
        # the result present trades. Without this half, a loop that refused
        # every method would pass the assertions above.
        present = await _cycle(db, portfolio, venue, walk_forward={METHOD: True})

        assert present.executed == 1
        assert present.refused == {}

    async def test_a_neutral_prediction_has_no_side_to_take(self, db, portfolio):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id, direction="neutral")

        result = await _cycle(db, portfolio, _venue(symbol))

        assert result.considered == 1
        assert result.executed == 0
        assert result.refused == {BridgeRefusal.NEUTRAL_DIRECTION.value: 1}
        assert await _orders(db, portfolio) == []

    async def test_the_histogram_accounts_for_every_prediction_not_executed(
        self, db, portfolio
    ):
        tradeable, tradeable_symbol = await _entity(db)
        await _calibrate(db, tradeable)
        await _pending(db, tradeable, created_at=NOW - timedelta(minutes=3))

        neutral, neutral_symbol = await _entity(db)
        await _pending(
            db, neutral, direction="neutral", created_at=NOW - timedelta(minutes=2)
        )

        uncalibrated, uncalibrated_symbol = await _entity(db)
        await _pending(
            db, uncalibrated, method="trend.untested", created_at=NOW - timedelta(minutes=1)
        )

        result = await _cycle(
            db,
            portfolio,
            _venue(tradeable_symbol, neutral_symbol, uncalibrated_symbol),
            walk_forward={METHOD: True, "trend.untested": True},
        )

        assert result.considered == 3
        assert result.executed == 1
        assert result.refused == {
            BridgeRefusal.NEUTRAL_DIRECTION.value: 1,
            Ineligible.UNCALIBRATED.value: 1,
        }
        assert sum(result.refused.values()) == result.considered - result.executed

    async def test_an_unsupplied_daily_pnl_refuses_rather_than_assuming_flat(
        self, db, portfolio
    ):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id)

        result = await run_cycle(
            db.pool,
            venue=_venue(symbol),
            portfolio_id=portfolio,
            config=_config(),
            walk_forward_results={METHOD: True},
            now=NOW,
        )

        assert result.executed == 0
        assert result.refused == {BridgeRefusal.RISK_REFUSED.value: 1}


class TestTheCeiling:
    async def test_ten_eligible_predictions_under_a_ceiling_of_three_execute_three(
        self, db, portfolio
    ):
        symbols = []
        for index in range(10):
            entity_id, symbol = await _entity(db)
            symbols.append(symbol)
            if index == 0:
                await _calibrate(db, entity_id)
            # Staggered only to fix the order they are considered in; all well
            # inside the staleness window, so none is refused for its age.
            await _pending(db, entity_id, created_at=NOW - timedelta(seconds=60 - index))

        venue = _venue(*symbols)
        result = await _cycle(
            db,
            portfolio,
            venue,
            config=_config(max_intents_per_cycle=3, limits=_roomy_limits()),
        )

        # The venue count first: it is the property the ceiling exists for. A
        # ceiling applied after execution still reports three executed while
        # having sent all ten.
        assert len(venue.fills) == 3
        assert len(await _orders(db, portfolio)) == 3
        assert result.considered == 10
        assert result.executed == 3
        assert result.refused == {LoopRefusal.CEILING_REACHED.value: 7}
        assert await db.pool.fetchval(
            "SELECT count(*) FROM position WHERE portfolio_id = $1", portfolio
        ) == 3


class TestIdempotency:
    async def test_running_the_same_cycle_twice_executes_once(self, db, portfolio):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        prediction_id = await _pending(db, entity_id)
        venue = _venue(symbol)
        config = _config(limits=_roomy_limits())

        first = await _cycle(db, portfolio, venue, config=config)
        second = await _cycle(db, portfolio, venue, config=config)

        assert first.executed == 1
        assert second.considered == 1
        assert second.executed == 0
        assert second.refused == {LoopRefusal.ALREADY_ORDERED.value: 1}

        assert len(venue.fills) == 1
        orders_written = await _orders(db, portfolio)
        assert len(orders_written) == 1
        assert orders_written[0]["idempotency_key"] == f"{portfolio}:{prediction_id}"
        assert orders_written[0]["filled_quantity"] == EXPECTED_QUANTITY

        quantity = await db.pool.fetchval(
            "SELECT quantity FROM position WHERE portfolio_id = $1", portfolio
        )
        assert quantity == EXPECTED_QUANTITY


class TestAnEmptyFillIsRecordedNotImproved:
    async def test_a_notional_below_the_venue_minimum_opens_no_position(
        self, db, portfolio
    ):
        entity_id, symbol = await _entity(db)
        await _calibrate(db, entity_id)
        await _pending(db, entity_id)
        # The intent's notional is 10,000; the venue will not take it and says so.
        venue = _venue(symbol, capabilities=_caps(min_notional=Decimal(50_000)))

        result = await _cycle(db, portfolio, venue)

        assert result.considered == 1
        assert result.executed == 0
        assert result.fills == ()
        assert result.refused == {LoopRefusal.EMPTY_FILL.value: 1}

        (order,) = await _orders(db, portfolio)
        assert order["status"] == "rejected"
        assert order["filled_quantity"] == Decimal(0)
        assert order["average_fill_price"] is None

        payload = await db.pool.fetchval(
            "SELECT payload FROM order_event WHERE order_id = $1 AND status = 'rejected'",
            order["id"],
        )
        recorded = json.loads(payload) if isinstance(payload, str) else payload
        assert "below venue minimum" in recorded["empty_fill"]["rejected"]

        assert await db.pool.fetchval(
            "SELECT count(*) FROM position WHERE portfolio_id = $1", portfolio
        ) == 0
        cash = await db.pool.fetchval(
            "SELECT free FROM cash_balance WHERE portfolio_id = $1 AND venue = 'paper'",
            portfolio,
        )
        assert cash == NAV


class TestCycleResultRefusesToMisreportItself:
    def test_a_dropped_refusal_cannot_be_constructed(self):
        with pytest.raises(ValueError, match="unnamed refusal"):
            CycleResult(
                considered=40,
                executed=0,
                refused={},
                fills=(),
                halted=False,
                halt_reason=None,
            )

    def test_an_execution_with_no_fill_behind_it_cannot_be_constructed(self):
        with pytest.raises(ValueError, match="fabricated"):
            CycleResult(
                considered=1,
                executed=1,
                refused={},
                fills=(),
                halted=False,
                halt_reason=None,
            )

    def test_a_halt_must_name_its_reason(self):
        with pytest.raises(ValueError, match="must name the reason"):
            CycleResult(
                considered=0,
                executed=0,
                refused={},
                fills=(),
                halted=True,
                halt_reason=None,
            )

    def test_a_ceiling_below_one_is_not_a_ceiling(self):
        with pytest.raises(ValueError, match="at least 1"):
            _config(max_intents_per_cycle=0)


class TestUnusableInputs:
    async def test_a_naive_clock_is_refused(self, db, portfolio):
        with pytest.raises(ValueError, match="naive"):
            await run_cycle(
                db.pool,
                venue=_venue("BTC/USD"),
                portfolio_id=portfolio,
                config=_config(),
                walk_forward_results={},
                now=NOW.replace(tzinfo=None),
            )


def test_the_refusal_names_do_not_collide():
    """Three enums share one histogram; a shared value would merge two reasons."""
    names = (
        [r.value for r in LoopRefusal]
        + [r.value for r in BridgeRefusal]
        + [r.value for r in Ineligible]
        + [r.value for r in RiskRefusal]
    )
    assert len(names) == len(set(names))
