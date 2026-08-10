"""Which names the book can afford to trade, as opposed to which pay the most.

`select_carry_basket` ranks on gross funding and **cannot** see execution cost:
`conviction` is forbidden from importing `venue` by the package boundary, so the
selector has no way to learn what a spread is. That is the right boundary and
the wrong outcome, because on a venue whose richest-funding names are also its
thinnest the two rankings disagree.

Measured on Hyperliquid, 2026-08-10, at $70 a leg:

```
        gross funding   spread(spot)   round trip   net over a six-week hold
PURR        12.2 %/yr       40.5 bps       121 bps            1.65 %/yr
SOL          8.4 %/yr        0.1 bps        18 bps            6.88 %/yr
```

The selector prefers PURR by 45% on the number it can see, and PURR earns a
quarter of SOL on the number that pays. Running that basket puts half the book
into the worst available trade and repeats the choice every six weeks.

So the universe is filtered here, in the bridge layer that may see both, before
the selector ranks what survives. **A ceiling on cost rather than a re-ranking**:
once every candidate is cheap to trade, gross and net order the same way, and
the selector keeps doing the one job it is good at.

`max_execution_bps` has no default, on the same reasoning as `spread_bps` and
`reconciliation_tolerance`: the permissive value here is a large one, and a
ceiling wide enough to admit anything is a filter that always passes while
looking like a control.

**This models the spread, not the depth.** `venue.quote` reads the top of book,
so a name that is tight at the touch and thin behind it is charged too little --
PURR's measured round trip was 121 bps against the 83 bps this would model. The
error is in the permissive direction and is stated rather than hidden; walking
the book belongs in the venue layer, and until it exists the ceiling should be
set well below the carry it is protecting.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from omni.venue.costs import BPS
from omni.venue.protocol import (
    MarketType,
    Side,
    TradeIntent,
    Venue,
    VenueUnavailable,
)

logger = logging.getLogger("omni.trading.tradeable")


@dataclass(frozen=True)
class Affordability:
    """What one pair costs to open and close, as a fraction of its notional."""

    entity_id: UUID
    asset: str
    round_trip_bps: Decimal | None
    reason: str | None = None

    @property
    def affordable(self) -> bool:
        return self.round_trip_bps is not None and self.reason is None


async def _leg_bps(
    venue: Venue,
    *,
    symbol: str,
    side: Side,
    market_type: MarketType,
    notional: Decimal,
    as_of: datetime,
) -> Decimal:
    """Fee plus adverse selection on one leg, in bps of the notional traded.

    Quoted at one unit and scaled. Both components of `CCXTVenue.quote` are
    linear in quantity -- fee is `price * qty * taker`, slippage is
    `(expected - mid) * qty` -- so the scaling is exact rather than an
    approximation. It is exact *because* the quote reads a ticker and never the
    book, which is the same reason it cannot see depth. Charging a size the
    venue never consulted would imply a size-aware model that does not exist.

    `reference_price` is a required positive field on `TradeIntent` and is
    unused by `quote`, so the probe carries 1. This intent is never executed.
    """
    quote = await venue.quote(
        TradeIntent(
            venue=venue.name,
            symbol=symbol,
            side=side,
            market_type=market_type,
            quantity=Decimal(1),
            reference_price=Decimal(1),
            provenance={"as_of": as_of, "strategy": "carry.affordability"},
            idempotency_key=f"probe:{venue.name}:{symbol}:{as_of.isoformat()}",
        )
    )
    price = quote.expected_price
    if price <= 0:
        raise VenueUnavailable(f"{symbol} quoted a non-positive price {price}")
    quantity = notional / price
    cost = (quote.fee + quote.slippage) * quantity
    return cost / notional * BPS


async def affordability(
    venue: Venue,
    *,
    assets: dict[UUID, str],
    notional_per_pair: Decimal,
    as_of: datetime,
) -> list[Affordability]:
    """Model each candidate pair's round-trip cost. Never raises for one name.

    A name the venue cannot quote is returned with a reason rather than an
    exception: one unquotable asset must not remove the whole universe, and an
    absent quote is a fact about that name worth recording next to the others.
    """
    out: list[Affordability] = []
    for entity_id, asset in sorted(assets.items(), key=lambda kv: kv[1]):
        try:
            spot = venue.symbol_for(asset, MarketType.SPOT)
            perp = venue.symbol_for(asset, MarketType.PERPETUAL)
        except (ValueError, VenueUnavailable) as exc:
            # `symbol_for` raises ValueError on an ambiguous asset -- Hyperliquid
            # refuses to guess a quote currency, which is correct and means an
            # unresolvable name is a fact to record, not a crash to propagate.
            out.append(Affordability(entity_id, asset, None, f"unresolvable: {exc}"))
            continue
        if spot is None or perp is None:
            out.append(
                Affordability(entity_id, asset, None, "venue lists no spot/perp pair")
            )
            continue
        try:
            # Both legs, both directions: the pair is opened and later closed,
            # and the close crosses the same spread in the opposite direction.
            legs = [
                await _leg_bps(venue, symbol=spot, side=Side.BUY,
                               market_type=MarketType.SPOT,
                               notional=notional_per_pair, as_of=as_of),
                await _leg_bps(venue, symbol=perp, side=Side.SELL,
                               market_type=MarketType.PERPETUAL,
                               notional=notional_per_pair, as_of=as_of),
                await _leg_bps(venue, symbol=spot, side=Side.SELL,
                               market_type=MarketType.SPOT,
                               notional=notional_per_pair, as_of=as_of),
                await _leg_bps(venue, symbol=perp, side=Side.BUY,
                               market_type=MarketType.PERPETUAL,
                               notional=notional_per_pair, as_of=as_of),
            ]
        except VenueUnavailable as exc:
            out.append(Affordability(entity_id, asset, None, f"unquotable: {exc}"))
            continue
        out.append(Affordability(entity_id, asset, sum(legs, Decimal(0))))
    return out


def affordable_ids(
    measured: Sequence[Affordability], *, max_execution_bps: Decimal
) -> list[UUID]:
    """The subset cheap enough to trade, logged with what was dropped and why.

    Silence about an excluded name is how a universe quietly shrinks to one
    asset and still reports a cross-section.
    """
    if max_execution_bps <= 0:
        raise ValueError(
            f"max_execution_bps must be positive, got {max_execution_bps}; a "
            f"ceiling of zero excludes every tradeable name"
        )
    kept: list[UUID] = []
    for m in measured:
        if not m.affordable:
            logger.info("carry universe drops %s: %s", m.asset, m.reason)
            continue
        assert m.round_trip_bps is not None
        if m.round_trip_bps > max_execution_bps:
            logger.info(
                "carry universe drops %s: round trip %.1f bps exceeds the %.1f bps "
                "ceiling; its funding would have to clear that before it pays",
                m.asset, float(m.round_trip_bps), float(max_execution_bps),
            )
            continue
        kept.append(m.entity_id)
    return kept
