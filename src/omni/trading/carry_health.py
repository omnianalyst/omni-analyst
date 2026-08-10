"""Is the carry book's edge still there?

The book runs itself. Nothing in it learns, and nothing in it notices when the
thing it harvests stops paying -- which is the failure this module exists for.

Finding 32 measured the decay directly: the premium falls with elapsed time
rather than with volatility regime, at roughly -0.80pp/month on the top
quintile, and crowding does not reverse. Finding 44 justified the book at
**14.58%/yr net** on the four executable names. Put those together and the book
has a half-life: at the measured rate the justification erodes over quarters,
the strategy keeps trading exactly as written, and the first visible sign is a
NAV curve that flattens -- months after the decision to stop should have been
made.

So this compares what the universe pays NOW against what it paid when the book
was justified, and says which of three states it is in. It asserts nothing about
the future: it reports a level and a threshold, and the thresholds are stated
here rather than inferred, because a floor that moves with the data is not a
floor.

**It does not trade, halt, or size anything.** A health reading that could stop
the book would be a second controller disagreeing with the first; the cycle's
own guards decide what happens to money. This decides what an operator is told.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from uuid import UUID

logger = logging.getLogger("omni.trading.carry_health")

# What the book was justified at: Finding 44, four executable names, top 2 by
# trailing funding, six-week hold, costs charged at measured round trips.
JUSTIFIED_NET_PCT = Decimal("14.58")

# Half of it. Not a derived number -- a stated one. At this level the book still
# pays, and the point of the threshold is that somebody looks before it is a
# question of whether to keep going.
DEGRADED_PCT = JUSTIFIED_NET_PCT / 2

# Roughly what idle stablecoin earns. Below this the book is taking exchange,
# liquidation and basis risk to underperform doing nothing, which is the one
# level at which continuing is not a judgement call.
FLOOR_PCT = Decimal("4.5")

# Hyperliquid settles hourly. Annualising a per-settlement rate needs the
# venue's own cadence, and using 365 instead of 24*365 understates it 24-fold --
# an error that would read as catastrophic decay on the first run.
SETTLEMENTS_PER_YEAR = Decimal(24 * 365)

_TRAILING = """
WITH visible AS (
{visible}
)
SELECT c.entity_id, avg((c.value->>'rate')::numeric) AS mean_rate, count(*) AS n
FROM visible c
WHERE c.entity_id = ANY($2::uuid[])
  AND c.claim_type = 'funding_rate'
  AND split_part(c.key, ':', 1) = $3
  AND c.event_date > $4
  AND c.event_date <= $5
GROUP BY c.entity_id
"""


class Verdict(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BELOW_FLOOR = "below_floor"
    UNKNOWN = "unknown"

    @property
    def detail(self) -> str:
        if self is Verdict.HEALTHY:
            return "the universe pays at or near the level the book was justified at"
        if self is Verdict.DEGRADED:
            return (
                "the premium has fallen below half its justified level; Finding 32 "
                "measured this decay as elapsed time rather than regime, and it does "
                "not reverse. Worth deciding whether to continue rather than drifting"
            )
        if self is Verdict.BELOW_FLOOR:
            return (
                "the book is taking exchange, liquidation and basis risk to earn less "
                "than idle stablecoin. Continuing is no longer a judgement call"
            )
        return "not enough visible funding coverage to say"


@dataclass(frozen=True)
class Health:
    """What the tradeable universe currently pays, against what justified it."""

    as_of: datetime
    per_asset_pct: dict[str, Decimal]
    basket_gross_pct: Decimal | None
    execution_cost_bps: Decimal | None
    basket_net_pct: Decimal | None
    verdict: Verdict

    def summary(self) -> str:
        gross = "n/a" if self.basket_gross_pct is None else f"{self.basket_gross_pct:.2f}%"
        net = "n/a" if self.basket_net_pct is None else f"{self.basket_net_pct:.2f}%"
        return (
            f"carry health {self.verdict.value}: basket gross {gross}/yr, "
            f"net {net}/yr against {JUSTIFIED_NET_PCT}%/yr justified"
        )


def classify(net_pct: Decimal | None) -> Verdict:
    if net_pct is None:
        return Verdict.UNKNOWN
    if net_pct < FLOOR_PCT:
        return Verdict.BELOW_FLOOR
    if net_pct < DEGRADED_PCT:
        return Verdict.DEGRADED
    return Verdict.HEALTHY


async def assess(
    pool,
    *,
    assets: dict[UUID, str],
    audience_user_id: UUID | None,
    funding_venue: str,
    as_of: datetime,
    enter_rank: int,
    lookback_days: int = 7,
    hold_days: int = 42,
    execution_cost_bps: Decimal | None = None,
    min_settlements: int = 2,
) -> Health:
    """Current annualised funding per name, and the top-`enter_rank` basket.

    Costs are amortised over the hold, matching how the strategy actually pays
    them: a round trip charged once against six weeks of carry, not once against
    a year. Charging them annually would understate the book by roughly eight
    times and manufacture a decay that is not there.
    """
    from omni.coverage.visibility import visible_claims_cte

    if not assets:
        return Health(as_of, {}, None, execution_cost_bps, None, Verdict.UNKNOWN)

    rows = await pool.fetch(
        _TRAILING.format(visible=visible_claims_cte("$1")),
        audience_user_id,
        list(assets),
        funding_venue,
        as_of - timedelta(days=lookback_days),
        as_of,
    )
    per_asset = {
        assets[r["entity_id"]]: Decimal(str(r["mean_rate"])) * SETTLEMENTS_PER_YEAR * 100
        for r in rows
        if r["n"] >= min_settlements and r["mean_rate"] is not None
    }
    if len(per_asset) < enter_rank:
        return Health(as_of, per_asset, None, execution_cost_bps, None, Verdict.UNKNOWN)

    top = sorted(per_asset.values(), reverse=True)[:enter_rank]
    gross = sum(top, Decimal(0)) / enter_rank

    net: Decimal | None = gross
    if execution_cost_bps is not None:
        # bps of notional, paid once, spread across the hold and annualised.
        drag = execution_cost_bps / Decimal(100) * (Decimal(365) / Decimal(hold_days))
        net = gross - drag

    return Health(as_of, per_asset, gross, execution_cost_bps, net, classify(net))
