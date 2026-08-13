"""Decide whether a carry rebalance is due, run it, and record what it settled.

`run_carry_cycle` is deliberately unopinionated about time: `as_of` and
`funding_since` are required, have no clock defaults, and it is the caller's job
to supply them. Until this module existed the only caller was a test, which is
why the two operational decisions the strategy actually depends on had nowhere
to live:

**Where the funding window opens.** `funding_since` is the previous cycle's
`as_of`, and a process restarted between cycles has no memory of it. Read from
`carry_cycle` (migration 048) rather than from a clock or a configured interval:
a skipped settlement is silent and understates the only thing this book earns,
and no mechanism anywhere catches it. The boundary advances only past windows a
cycle actually settled -- a cycle that halted on pair integrity or on the
reconciler never reached the funding loop, and treating its `as_of` as settled
would open the next window past settlements that were never applied.

**When the rebalance happens.** GOING_LIVE step 4.5, and the only free
improvement in a day of testing roughly 250 hypotheses. Crypto trades around the
clock but its volatility clock is imported from TradFi: measured on BTC/ETH/SOL
one-minute bars, within-day variance troughs at 05:00 UTC and peaks at 14:00,
the US cash open. The ratio is 2.5x pooled and has widened every year since 2022.
Crossing the spread on both legs of every pair in the quiet window rather than
the loud one roughly halves the variance the book eats on the way in and out. It
costs nothing, adds no risk, and says nothing about direction -- volatility
clustering says when the market moves, never which way.

**It is enforced here rather than documented.** A window that lives in a runbook
is a window that holds until the first cycle run by someone in a hurry, and the
cost of missing it is invisible in every report the system produces: it lands in
the fill price, not in a number anything reconciles.

Both guards refuse rather than adjust. A runner that shifted `as_of` into the
window on the operator's behalf would move the instant the selector ranks at and
the funding window closes at, which is a different cycle than the one asked for.

**Every refusal is written to `carry_refusal` before it is raised.** A refused
cycle trades nothing and settles nothing, so before migration 057 it wrote
nothing either, and the reason survived only in the log of the process that ran.
On a six-week hold that is a book producing no row anywhere on roughly 41 of
every 42 days -- and no absence of rows could distinguish a correct refusal from
a scheduler that never fired. The row is what makes the loop's own silence
legible.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NoReturn
from uuid import UUID

from omni.trading.carry_loop import CarryConfig, CarryCycleResult, run_carry_cycle
from omni.trading.tradeable import affordability, affordable_ids
from omni.venue.protocol import Venue

logger = logging.getLogger("omni.trading.carry")

# Inclusive lower bound, exclusive upper, UTC hours. The trough of the measured
# volatility clock sits at 05:00; the window is the four hours around it that
# stay below the daily mean.
WINDOW_OPENS_HOUR = 3
WINDOW_CLOSES_HOUR = 7

# The measured hold. Finding 9 put turnover at 29.19% of cost against 8.74% of
# gross at the fastest cadence tested, which is the strategy destroying itself;
# six weeks is the hold at which the round trip amortises to roughly 0.4%/yr.
REBALANCE_PERIOD = timedelta(weeks=6)

_BOUNDARY = """
SELECT COALESCE(max(funding_settled_through), min(funding_since)) AS opens_at,
       max(as_of)                                                 AS last_cycle,
       max(as_of) FILTER (WHERE NOT halted)                       AS last_completed
FROM carry_cycle
WHERE portfolio_id = $1 AND venue = $2
"""

_RECORD = """
INSERT INTO carry_cycle (
    portfolio_id, venue, as_of, funding_since, funding_settled_through,
    halted, halt_reason, abstention,
    funding_collected, fees_paid, modelled_turnover_cost,
    pairs_opened, pairs_closed, pairs_held
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
"""

# ON CONFLICT DO NOTHING rather than an upsert: the guards are deterministic in
# their inputs, so a second evaluation at the same instant reaches the same
# verdict, and a second row would inflate any count taken over this table --
# including the one that answers "is the scheduler firing daily".
_RECORD_REFUSAL = """
INSERT INTO carry_refusal (
    portfolio_id, venue, attempted_at, guard, reason,
    funding_window_opens_at, last_cycle_at, last_completed_at, next_due_at
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
ON CONFLICT (portfolio_id, venue, attempted_at) DO NOTHING
"""

# The identity of each check, stable across message rewordings. The reason
# sentence is written for the operator reading a log; the guard is what a query
# groups by, so a refusal count survives someone improving the prose.
GUARD_OUTSIDE_WINDOW = "outside_rebalance_window"
GUARD_NO_RECORDED_CYCLE = "no_recorded_cycle"
GUARD_INSTANT_ALREADY_COVERED = "instant_already_covered"
GUARD_INSIDE_HOLD = "inside_hold"
GUARD_NO_AFFORDABLE_NAME = "no_affordable_name"

GUARDS = frozenset(
    {
        GUARD_OUTSIDE_WINDOW,
        GUARD_NO_RECORDED_CYCLE,
        GUARD_INSTANT_ALREADY_COVERED,
        GUARD_INSIDE_HOLD,
        GUARD_NO_AFFORDABLE_NAME,
    }
)


class CarryRunRefused(Exception):
    """The cycle was not run, and the book is untouched.

    Distinct from a halt, which is a cycle that ran and stopped partway. Nothing
    here has read the venue or written a claim.
    """

    def __init__(self, reason: str, *, guard: str) -> None:
        super().__init__(reason)
        self.guard = guard
        self.reason = reason


@dataclass(frozen=True)
class Boundary:
    """Where the next window opens, and what the cadence is measured from.

    `opens_at` is None only for a book that has never recorded a cycle. Every
    other state -- including a first cycle that halted before settling -- reads
    back the origin the first cycle was given, so the operator states it once
    and cannot restate it later without the restatement being visible.
    """

    opens_at: datetime | None
    last_cycle: datetime | None
    last_completed: datetime | None


def in_rebalance_window(at: datetime) -> bool:
    if at.tzinfo is None:
        raise ValueError(
            f"{at} is naive; the rebalance window is stated in UTC and a naive "
            f"instant is whatever the host's timezone happens to be"
        )
    return WINDOW_OPENS_HOUR <= at.astimezone(UTC).hour < WINDOW_CLOSES_HOUR


async def boundary(pool, portfolio_id: UUID, venue: str) -> Boundary:
    row = await pool.fetchrow(_BOUNDARY, portfolio_id, venue)
    if row is None:
        return Boundary(opens_at=None, last_cycle=None, last_completed=None)
    return Boundary(
        opens_at=row["opens_at"],
        last_cycle=row["last_cycle"],
        last_completed=row["last_completed"],
    )


async def record_cycle(
    pool,
    *,
    portfolio_id: UUID,
    venue: str,
    funding_since: datetime,
    result: CarryCycleResult,
) -> None:
    """Write the cycle to the log, halts included.

    A halted cycle is recorded because the log is the audit trail of what the
    book did, not a list of successes -- and because the halt itself is what a
    later reader needs in order to explain a boundary that did not move.
    """
    await pool.execute(
        _RECORD,
        portfolio_id,
        venue,
        result.as_of,
        funding_since,
        result.funding_settled_through,
        result.halted,
        result.halt_reason,
        result.abstention,
        result.funding_collected,
        result.fees_paid,
        result.modelled_turnover_cost,
        len(result.opened),
        len(result.closed),
        len(result.held),
    )


async def record_refusal(
    pool,
    *,
    portfolio_id: UUID,
    venue: str,
    attempted_at: datetime,
    guard: str,
    reason: str,
    known: Boundary | None,
) -> None:
    """Write the refusal, with the boundary state it was taken against.

    `known` is None for a refusal taken before the boundary was read, which is
    every refusal from the window guard -- the cheapest check, and deliberately
    first. The boundary columns are then NULL and `guard` says why, so the
    absence reads as "the runner never got that far" rather than as a book with
    no history.
    """
    due = None
    if known is not None and known.last_completed is not None:
        due = known.last_completed + REBALANCE_PERIOD
    await pool.execute(
        _RECORD_REFUSAL,
        portfolio_id,
        venue,
        attempted_at,
        guard,
        reason,
        None if known is None else known.opens_at,
        None if known is None else known.last_cycle,
        None if known is None else known.last_completed,
        due,
    )


async def _refuse(
    pool,
    *,
    portfolio_id: UUID,
    venue: str,
    attempted_at: datetime,
    guard: str,
    reason: str,
    known: Boundary | None = None,
) -> NoReturn:
    """Record the refusal, then raise it.

    A failure to record is logged and swallowed rather than raised. The refusal
    is the true outcome -- the book is untouched either way -- and letting a
    write error surface in its place would report a normal, correct decision to
    the operator as a crash. The log is the fallback that existed before this
    table, and an ERROR there is the signal that the new record is not being
    kept; it must not become the signal that the cycle failed.
    """
    try:
        await record_refusal(
            pool,
            portfolio_id=portfolio_id,
            venue=venue,
            attempted_at=attempted_at,
            guard=guard,
            reason=reason,
            known=known,
        )
    except Exception:
        logger.exception(
            "could not record the %s refusal for portfolio=%s venue=%s at %s; "
            "the cycle was still refused and the book is untouched: %s",
            guard,
            portfolio_id,
            venue,
            attempted_at.isoformat(),
            reason,
        )
    raise CarryRunRefused(reason, guard=guard)


async def run_due_cycle(
    pool,
    *,
    venue: Venue,
    portfolio_id: UUID,
    config: CarryConfig,
    entity_ids: Sequence[UUID],
    audience_user_id: UUID | None,
    now: datetime,
    inception: datetime | None = None,
    ignore_window: bool = False,
    ignore_cadence: bool = False,
    max_execution_bps: Decimal | None = None,
    assets: dict[UUID, str] | None = None,
) -> CarryCycleResult:
    """Run one rebalance if one is due, and record the boundary it settled.

    Raises `CarryRunRefused` without touching the book when the instant is
    outside the rebalance window, when the last completed cycle is more recent
    than the hold, or when the boundary cannot be established. The two overrides
    exist because both guards are operational judgements rather than invariants
    -- a first cycle has to start somewhere, and a book that halted and was
    repaired should not wait six weeks -- but each has to be named at the call
    site, so neither can be taken by default.
    """
    if now.tzinfo is None:
        raise ValueError(
            f"now is naive ({now}); the window, the funding bounds and every claim "
            f"this cycle reads are stamped UTC"
        )
    if not ignore_window and not in_rebalance_window(now):
        await _refuse(
            pool,
            portfolio_id=portfolio_id,
            venue=venue.name,
            attempted_at=now,
            guard=GUARD_OUTSIDE_WINDOW,
            reason=(
                f"{now.astimezone(UTC):%H:%M} UTC is outside the "
                f"{WINDOW_OPENS_HOUR:02d}:00-{WINDOW_CLOSES_HOUR:02d}:00 UTC rebalance "
                f"window. Both legs of every pair cross the spread at this instant, and "
                f"the measured variance at 14:00 UTC is 2.5x the 05:00 trough -- so a "
                f"cycle run outside the window pays a cost that appears in no report. "
                f"Pass ignore_window to run anyway"
            ),
        )

    known = await boundary(pool, portfolio_id, venue.name)
    if known.opens_at is None and inception is None:
        await _refuse(
            pool,
            portfolio_id=portfolio_id,
            venue=venue.name,
            attempted_at=now,
            guard=GUARD_NO_RECORDED_CYCLE,
            known=known,
            reason=(
                f"portfolio {portfolio_id} has recorded no carry cycle at {venue.name}, so "
                f"the instant its funding window opens at is not knowable here. State it "
                f"as the inception: one settlement period skips every settlement in a "
                f"longer gap, and the portfolio's own inception re-walks history already "
                f"collected"
            ),
        )
    funding_since = known.opens_at if known.opens_at is not None else inception
    assert funding_since is not None

    if known.last_cycle is not None and now <= known.last_cycle:
        await _refuse(
            pool,
            portfolio_id=portfolio_id,
            venue=venue.name,
            attempted_at=now,
            guard=GUARD_INSTANT_ALREADY_COVERED,
            known=known,
            reason=(
                f"the last cycle at {venue.name} ran at {known.last_cycle}, which is not "
                f"before {now}. A cycle settles ({funding_since}, now] and ranks as of "
                f"now; running one at an instant already covered would rank on a book "
                f"the log says has moved since"
            ),
        )
    if (
        not ignore_cadence
        and known.last_completed is not None
        and now - known.last_completed < REBALANCE_PERIOD
    ):
        due = known.last_completed + REBALANCE_PERIOD
        await _refuse(
            pool,
            portfolio_id=portfolio_id,
            venue=venue.name,
            attempted_at=now,
            guard=GUARD_INSIDE_HOLD,
            known=known,
            reason=(
                f"the last completed cycle ran at {known.last_completed}; the hold is "
                f"{REBALANCE_PERIOD.days} days and the next is due {due}. Rebalancing "
                f"sooner is turnover the signal did not ask for, and turnover is what "
                f"Finding 9 measured destroying this strategy. Pass ignore_cadence to "
                f"run anyway"
            ),
        )

    # Execution-cost filter, BEFORE the selector ranks. `select_carry_basket`
    # scores gross funding and cannot see a spread -- `conviction` may not import
    # `venue` -- so on a venue whose richest names are its thinnest, the ranking
    # it produces and the ranking that pays disagree. Measured on Hyperliquid:
    # PURR ranked first at 12.2%/yr gross and earned 1.65% net against SOL's
    # 8.4% gross and 6.88% net. Filtering the universe here keeps the selector
    # doing the one job it can do correctly.
    universe = list(entity_ids)
    if max_execution_bps is not None:
        if assets is None:
            raise ValueError(
                "max_execution_bps was given without `assets`; the filter needs "
                "each entity's asset symbol to ask the venue what it costs"
            )
        measured = await affordability(
            venue,
            assets={e: assets[e] for e in universe if e in assets},
            notional_per_pair=config.notional_per_pair,
            as_of=now,
        )
        universe = affordable_ids(measured, max_execution_bps=max_execution_bps)
        if not universe:
            await _refuse(
                pool,
                portfolio_id=portfolio_id,
                venue=venue.name,
                attempted_at=now,
                guard=GUARD_NO_AFFORDABLE_NAME,
                known=known,
                reason=(
                    f"no name in the universe trades within {max_execution_bps} bps "
                    f"round trip at {config.notional_per_pair} a pair. Trading the "
                    f"cheapest of a bad set is how a cost floor gets crossed by "
                    f"default; raise the ceiling deliberately or wait for spreads"
                ),
            )

    result = await run_carry_cycle(
        pool,
        venue=venue,
        portfolio_id=portfolio_id,
        config=config,
        entity_ids=universe,
        audience_user_id=audience_user_id,
        as_of=now,
        funding_since=funding_since,
    )
    await record_cycle(
        pool,
        portfolio_id=portfolio_id,
        venue=venue.name,
        funding_since=funding_since,
        result=result,
    )
    return result


def _report(result: CarryCycleResult) -> str:
    parts = [
        f"as_of={result.as_of.isoformat()}",
        f"held={len(result.held)}",
        f"opened={len(result.opened)}",
        f"closed={len(result.closed)}",
        f"funding={result.funding_collected}",
        f"fees={result.fees_paid}",
    ]
    if result.abstention is not None:
        parts.append(f"abstained={result.abstention}")
    if result.halted:
        parts.append(f"HALTED={result.halt_reason}")
    if result.refused:
        parts.append(f"refused={result.refused}")
    return " ".join(parts)


async def _check(pool, portfolio_id: UUID, venue_name: str, now: datetime) -> int:
    known = await boundary(pool, portfolio_id, venue_name)
    logger.info(
        "portfolio=%s venue=%s window_opens_at=%s last_cycle=%s last_completed=%s "
        "in_window=%s",
        portfolio_id,
        venue_name,
        known.opens_at,
        known.last_cycle,
        known.last_completed,
        in_rebalance_window(now),
    )
    if known.opens_at is None:
        logger.info("no cycle recorded: the first run must state --inception")
        return 0
    if known.last_completed is not None:
        due = known.last_completed + REBALANCE_PERIOD
        logger.info("next cycle due %s (%s)", due, "now" if now >= due else "not yet")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m omni.trading.carry_runner",
        description=(
            "Report the carry book's funding boundary and whether a rebalance is due. "
            "Running a live cycle is not done from here -- see "
            "_orchestrator/GOING_LIVE.md, which builds the venue with the trading "
            "mode named explicitly at the call site."
        ),
    )
    parser.add_argument("--portfolio", required=True, type=UUID)
    parser.add_argument("--venue", required=True)
    parser.add_argument(
        "--as-of",
        type=datetime.fromisoformat,
        default=None,
        help="ISO instant to evaluate against, default now (UTC)",
    )
    return parser


async def main(argv: Sequence[str] | None = None) -> int:
    from omni.config import settings
    from omni.db import connect

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = _parser().parse_args(argv)
    now = args.as_of or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    client = await connect(settings.database_url)
    try:
        return await _check(client.pool, args.portfolio, args.venue, now)
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
