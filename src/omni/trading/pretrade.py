"""The checks a trading cycle runs before it is allowed to trade.

Both loops need these and they must behave identically in both. `loop.py`
solved them first for the directional book; `carry_loop.py` needs the same
guarantees for the delta-neutral one, and a second copy is how the two come to
disagree about what "reconciled" means.

That is not a hypothetical concern here. `PaperVenue._debit_cash` and
`portfolio.state._cash_delta` were two answers to one question -- what a fill
does to cash -- and when one was corrected the other silently was not, so the
venue and the book disagreed by an entire perpetual notional (Finding 17). The
difference is that those two could not be shared: `omni.venue` sits below
`omni.portfolio` and may not import it. These two can, so they are.

Neither function raises into its caller, and that is the load-bearing property.
A pre-trade check exists to inform a halt decision; a check that can itself
abort the cycle takes the halt with it, along with the detail naming which
symbol diverged -- the one artefact an operator needs. Failures are returned or
logged, never thrown, and never permitted to decide anything.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from omni.portfolio import alerts
from omni.portfolio.reconcile import ReconciliationResult, latest_by_venue, record

logger = logging.getLogger(__name__)


async def record_reconciliation(
    pool, verified: ReconciliationResult, *, portfolio_id: UUID
) -> str | None:
    """Persist a verdict already reached. Returns why it could not be, or None.

    Called on both outcomes. Recording only the passes would delete exactly the
    evidence an operator needs: the venue that diverged is the venue whose
    history would then read `never_run`, and a reconciliation the store cannot
    show ever happened is one nobody can be held to.

    The failure is caught rather than raised because raising would abandon the
    cycle before the halt names which symbol diverged. It is returned so the
    halt can carry it, and logged so it is not lost when there is no halt to
    carry it -- what it must never do is decide anything.
    """
    try:
        await record(pool, verified, portfolio_id=portfolio_id)
    except Exception as exc:  # reported and returned, never swallowed
        logger.exception(
            "the reconciliation of %s could not be recorded for portfolio %s",
            verified.venue,
            portfolio_id,
        )
        return (
            f"the result could not be recorded ({type(exc).__name__}: {exc}), so "
            f"no reader of the reconciliation history can see that this check ran"
        )
    return None


async def evaluate_risk_alerts(pool, *, portfolio_id: UUID, now: datetime) -> None:
    """Run the portfolio's configured risk alerts against what is on record.

    `reconciliations` is read back from the store rather than handed the result
    this cycle is holding. `alerts.evaluate` refuses to read a venue it has no
    result for as healthy, and that refusal is the whole mechanism by which a
    verdict that failed to persist stops looking like a pass: passing the
    in-memory object instead would report the venue as verified on the strength
    of a record nobody can read back.

    Runs on the halting path too. A divergence is the moment the reconciliation
    alert exists for, and an alert pass that only ran on the cycles that got
    past reconciliation would be silent for exactly the venue in trouble.

    Nothing here raises into the cycle. Alerting observes the cycle; a failure
    to observe must not be able to lose the halt the cycle already decided on.
    """
    try:
        reconciliations = await latest_by_venue(pool, portfolio_id)
        for alert in await alerts.load_alerts(pool, portfolio_id):
            await alerts.evaluate(
                pool, alert, reconciliations=reconciliations, now=now
            )
    except Exception:  # logged, and never allowed to move a verdict
        logger.exception(
            "the risk alerts for portfolio %s could not be evaluated", portfolio_id
        )
