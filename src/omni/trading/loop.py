"""One cycle of the path from a pending prediction to a recorded fill.

Every piece of this path already exists and is tested on its own. What did not
exist is the order they run in, and the order is the entire safety argument:

**Reconciliation runs first and gates everything.** Not at the end as a report.
If the book we keep disagrees with the book the venue keeps, then NAV is wrong,
every position the risk engine reads is wrong, and every size computed from
either is wrong -- so the cycle stops before a single prediction is looked at
and says which symbol diverged. A divergence discovered after forty sized
positions have been opened against it is a divergence that has already
compounded into all forty.

**Every reconciliation is recorded, and recording changes nothing.** The verdict
is persisted whether it passed or failed, because the failed one is the reading
an operator has to act on and a divergence that was found, halted on, and never
written leaves `/trading/reconciliation` reporting `never_run` for the one venue
that actually diverged. The write is a side effect in the strict sense: it
cannot move the verdict in either direction, so a write that fails still halts
the cycle on the divergence it already found, and a write that fails on a clean
check still lets the cycle run. It surfaces instead through the alert pass --
which reads the STORE, never the object in hand, so a verdict nobody can read
back reaches the alerting system as `RECONCILIATION_UNKNOWN`, which is the
honest statement about a check whose answer was lost.

**Every refusal is counted and named.** `CycleResult.refused` is a histogram
over reasons, and `CycleResult` refuses at construction to hold a count that
does not add up: `sum(refused.values())` must equal `considered - executed`. A
loop that considered forty predictions and executed none has to say why forty
times, because a loop that silently does nothing is indistinguishable from a
loop that is broken, and this histogram is the only artefact an operator has to
tell the two apart.

**The ceiling is checked before the venue is called, never after.** A bug
upstream -- a duplicated prediction writer, a calibration that suddenly
qualifies every method -- reaches this loop as forty perfectly ordinary
intents. `max_intents_per_cycle` is what stops forty positions from opening in
one tick, and a ceiling enforced after execution stops nothing.

**Idempotency is the order ledger's, not this module's.** The intent is stamped
with a key derived from `(portfolio, prediction)` and handed to
`orders.record_intent`, whose `ON CONFLICT` collapses the second write onto the
first. A cycle re-run against the same pending predictions therefore finds the
order already past `intent` and refuses it by name. A second mechanism here --
a "have we traded this" query -- would be a check-then-act with a window in it,
and would disagree with the ledger the moment the two got out of step.

**`walk_forward_results` has no default and is never filled in.** A method
absent from the mapping passes `None` to `policy.eligible`, which refuses it as
`NO_WALK_FORWARD`. Substituting `True` for "we have not run the validation"
admits an in-sample hit rate to capital, which is the specific thing GATE A
exists to prevent.

**An empty fill is recorded, never improved.** A venue that filled nothing
because the notional was below its minimum, or because no volume traded, gets
its own reason recorded verbatim against a rejected order. Nothing is resized,
retried, or written to the position table.

Cash IS reconciled. It was not, originally: `cash_balance.free` is signed by
design -- a margin buy legitimately overdraws it -- while
`venue.protocol.Balance` refuses a negative `free`, and the two looked
unbridgeable without inventing a number. The resolution was that they are
different quantities sharing a name. A venue cannot report a negative available
balance; a local book can be overdrawn. So `Balance` keeps its guard (it models
what a venue reports) and `portfolio.state.CashPosition` carries the signed
local figure. `state.load` returns both in one snapshot, so passing
`book.cash_positions` introduces no second read and no torn comparison.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from omni.portfolio import orders, state
from omni.portfolio.orders import OrderLedgerError, OrderStatus
from omni.portfolio.reconcile import reconcile
from omni.portfolio.risk import RiskLimits
from omni.trading import pretrade
from omni.trading.bridge import BridgeRefusal, BridgeResult, prediction_to_intent
from omni.trading.policy import Eligibility, TradingPhase, eligible
from omni.venue.protocol import Fill, MarketType, Venue, VenueUnavailable

logger = logging.getLogger(__name__)

# Pending only, and only while the horizon is still open: a prediction whose
# horizon has elapsed is waiting to be scored, not waiting to be traded, and an
# intent carrying an expiry in the past is a position with no life in it.
_PENDING = """
SELECT p.id, p.entity_id, p.method, p.direction, p.confidence,
       p.entry_price, p.upper_barrier, p.lower_barrier,
       p.horizon_ends_at, p.created_at, p.audience_user_id,
       e.kind, e.symbol
FROM prediction p
JOIN entity e ON e.id = p.entity_id
WHERE p.outcome = 'pending'
  AND p.horizon_ends_at > $1
ORDER BY p.created_at, p.id
"""

_PEAK_NAV = "SELECT max(nav) FROM nav_snapshot WHERE portfolio_id = $1"


class LoopRefusal(str, Enum):
    """Why the loop itself stopped an intent that the bridge had allowed."""

    CEILING_REACHED = "the_cycle_intent_ceiling_was_already_reached"
    ALREADY_ORDERED = "this_prediction_already_has_an_order_in_the_ledger"
    EMPTY_FILL = "the_venue_filled_nothing"
    VENUE_UNAVAILABLE = "the_venue_could_not_execute_the_intent"


@dataclass(frozen=True)
class LoopConfig:
    """What the cycle is permitted to do, decided before it runs.

    `limits` sits here rather than being derived: the bridge raises without it
    (an assumed cap is a disabled one), and it is a property of the portfolio's
    mandate rather than of any one cycle.
    """

    phase: TradingPhase
    target_hit_rate: float
    tolerance: Decimal
    max_intents_per_cycle: int
    market_type: MarketType
    limits: RiskLimits

    # The gate's risk parameters. They live here for the same reason `limits`
    # does -- they are a property of the portfolio's mandate, not of any one
    # cycle -- and, like `limits`, they carry NO defaults. `policy.eligible`
    # refuses to default them because a caller that never thought about the cost
    # of a round trip is exactly the caller whose edge does not survive one, and
    # defaulting them here would reintroduce that at one remove.
    round_trip_cost_bps: Decimal
    min_expectancy_bps: Decimal
    min_effective_n: int
    max_assumed_share: Decimal
    max_concentration: Decimal

    def __post_init__(self) -> None:
        if self.max_intents_per_cycle < 1:
            raise ValueError(
                f"max_intents_per_cycle is a ceiling and must be at least 1, got "
                f"{self.max_intents_per_cycle}; a loop that may take no intent at "
                f"all is expressed by phase={TradingPhase.HALTED.value}, which "
                f"says so in the eligibility record of every method"
            )


@dataclass(frozen=True)
class CycleResult:
    """What one cycle looked at, what it did, and why it did not do the rest.

    The three incoherent states are refused at construction. A result whose
    refusals do not account for every prediction it declined is the exact shape
    of a swallowed refusal, and it would be read by an operator as "nothing
    qualified" rather than "something is dropping candidates on the floor".
    """

    considered: int
    executed: int
    refused: dict[str, int]
    fills: tuple[Fill, ...]
    halted: bool
    halt_reason: str | None

    def __post_init__(self) -> None:
        if self.halted and not self.halt_reason:
            raise ValueError("a halted cycle must name the reason it halted")
        if not self.halted and self.halt_reason is not None:
            raise ValueError(
                f"a cycle that ran to completion carries no halt reason, got "
                f"{self.halt_reason!r}"
            )
        if self.executed != len(self.fills):
            raise ValueError(
                f"executed={self.executed} but {len(self.fills)} fills are "
                f"carried; an execution with no fill behind it is a fabricated one"
            )
        outstanding = self.considered - self.executed
        counted = sum(self.refused.values())
        if counted != outstanding:
            raise ValueError(
                f"{self.considered} considered and {self.executed} executed leaves "
                f"{outstanding} refused, but the histogram counts {counted}; an "
                f"unnamed refusal is indistinguishable from a broken loop"
            )


def _refusal_name(result: BridgeResult) -> str:
    """The name an operator would have to act on, not the layer that said it.

    `METHOD_INELIGIBLE` is the bridge reporting that policy said no; the
    actionable fact is *which* policy bar was missed, so the histogram carries
    the policy reason and the bridge's wrapper is dropped.
    """
    if result.refusal is None:
        raise ValueError("a result carrying an intent has no refusal to name")
    if (
        result.refusal is BridgeRefusal.METHOD_INELIGIBLE
        and result.eligibility is not None
        and result.eligibility.reason is not None
    ):
        return result.eligibility.reason.value
    return result.refusal.value


async def run_cycle(
    pool,
    *,
    venue: Venue,
    portfolio_id: UUID,
    config: LoopConfig,
    walk_forward_results: Mapping[str, bool | None],
    now: datetime,
    realised_pnl_today: Decimal | None = None,
) -> CycleResult:
    """Reconcile, then turn every pending prediction into a fill or a reason.

    `realised_pnl_today` has no derivation available to this module -- realised
    P&L needs a trade ledger the portfolio tier does not keep -- so it is
    supplied by the caller and defaults to `None`, which the risk engine reads
    as "the daily loss limit has not been cleared" and refuses. That is the
    fail-closed direction: a loop wired up without it trades nothing and says
    `daily_pnl_unknown` on every candidate, rather than trading with a limit
    nobody checked.

    `peak_nav` is the high-water mark of the recorded NAV snapshots and the
    current cost-basis NAV, which is always defined -- a portfolio with one
    observation peaked at that observation -- so the drawdown kill switch is
    measured rather than skipped.
    """
    if now.tzinfo is None:
        raise ValueError(
            f"now is naive ({now}); the cycle stamps reconciliation and ages "
            f"prediction data against it, and a naive reading silently shifts both"
        )

    book = await state.load(pool, portfolio_id)

    verified = await reconcile(
        book.positions,
        book.cash_positions,
        venue,
        tolerance=config.tolerance,
        now=now,
    )
    unrecorded = await pretrade.record_reconciliation(pool, verified, portfolio_id=portfolio_id)
    await pretrade.evaluate_risk_alerts(pool, portfolio_id=portfolio_id, now=now)

    if not verified:
        halt_reason = (
            f"{verified.venue} did not reconcile, so no position this cycle "
            f"would size against a verified book: "
            + "; ".join(d.detail for d in verified.discrepancies)
        )
        if unrecorded is not None:
            halt_reason = f"{halt_reason} -- and {unrecorded}"
        return CycleResult(
            considered=0,
            executed=0,
            refused={},
            fills=(),
            halted=True,
            halt_reason=halt_reason,
        )

    recorded_peak = await pool.fetchval(_PEAK_NAV, portfolio_id)
    peak_nav = book.nav if recorded_peak is None else max(recorded_peak, book.nav)

    rows = await pool.fetch(_PENDING, now)

    refused: dict[str, int] = {}
    fills: list[Fill] = []
    eligibilities: dict[tuple[str, str, UUID | None], Eligibility] = {}
    considered = 0
    submitted = 0

    def refuse(reason: str) -> None:
        refused[reason] = refused.get(reason, 0) + 1

    for row in rows:
        considered += 1
        prediction = dict(row)
        method: str = prediction["method"]
        entity_kind: str = prediction["kind"]
        audience: UUID | None = prediction["audience_user_id"]

        # One eligibility per (method, kind, audience) per cycle: the aggregate
        # is over the whole resolved ledger and cannot change between two
        # predictions in the same pass.
        cache_key = (method, entity_kind, audience)
        if cache_key not in eligibilities:
            eligibilities[cache_key] = await eligible(
                pool,
                method=method,
                entity_kind=entity_kind,
                audience_user_id=audience,
                phase=config.phase,
                target_hit_rate=config.target_hit_rate,
                walk_forward_positive=walk_forward_results.get(method),
                round_trip_cost_bps=config.round_trip_cost_bps,
                min_expectancy_bps=config.min_expectancy_bps,
                min_effective_n=config.min_effective_n,
                max_assumed_share=config.max_assumed_share,
                max_concentration=config.max_concentration,
            )
        eligibility = eligibilities[cache_key]

        result = await prediction_to_intent(
            prediction,
            eligibility=eligibility,
            state=book,
            limits=config.limits,
            venue_name=venue.name,
            capabilities=venue.capabilities,
            symbol=prediction["symbol"],
            market_type=config.market_type,
            # The data behind a prediction is as of when the prediction was
            # made; ageing it against `now` is what the staleness limit means.
            data_as_of=prediction["created_at"],
            now=now,
            realised_pnl_today=realised_pnl_today,
            peak_nav=peak_nav,
            reconciled=verified.reconciled,
        )
        if result.intent is None:
            refuse(_refusal_name(result))
            continue

        if submitted >= config.max_intents_per_cycle:
            refuse(LoopRefusal.CEILING_REACHED.value)
            continue

        intent = replace(
            result.intent,
            idempotency_key=f"{portfolio_id}:{prediction['id']}",
        )
        order_id = await orders.record_intent(pool, portfolio_id, intent)
        order = await orders.get(pool, order_id)
        if order is None:
            raise OrderLedgerError(
                f"order {order_id} was recorded for prediction "
                f"{prediction['id']} and cannot be read back"
            )
        if order.status is not OrderStatus.INTENT:
            # A prior cycle already carried this prediction past the ledger, so
            # the venue must not see it a second time.
            refuse(LoopRefusal.ALREADY_ORDERED.value)
            continue

        await orders.transition(pool, order_id, OrderStatus.SUBMITTED)
        submitted += 1

        try:
            fill = await venue.execute(intent)
        except VenueUnavailable as exc:
            await orders.transition(
                pool,
                order_id,
                OrderStatus.REJECTED,
                payload={"venue_unavailable": str(exc)},
            )
            refuse(LoopRefusal.VENUE_UNAVAILABLE.value)
            continue

        if fill.is_empty:
            await orders.transition(
                pool,
                order_id,
                OrderStatus.REJECTED,
                external_id=fill.external_id,
                payload={"empty_fill": fill.raw},
            )
            refuse(LoopRefusal.EMPTY_FILL.value)
            continue

        await orders.record_fill(pool, order_id, fill)
        book = await state.apply_fill(pool, portfolio_id, fill, config.market_type)
        peak_nav = max(peak_nav, book.nav)
        fills.append(fill)

    return CycleResult(
        considered=considered,
        executed=len(fills),
        refused=refused,
        fills=tuple(fills),
        halted=False,
        halt_reason=None,
    )
