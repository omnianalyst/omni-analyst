"""Mark the book and record one NAV point, on a schedule.

`snapshot_nav` has existed since migration 033 and **has never had a caller**:
`nav_snapshot` holds zero rows. So the trading UI reports NAV as a single
number with no history behind it, and there is nothing to draw a curve from.

The reason this is worth wiring before any money arrives rather than after:
**a NAV series cannot be backfilled.** It is a mark taken at an instant against
a book as it stood, and neither the book's past composition nor the price
visible at the time can be reconstructed once the positions have moved. A
snapshot not taken on the day is a snapshot lost, which is the same property
that makes the funding boundary and the hypothesis registry append-only.

Marks come from `carry_loop._price_at` rather than from a second query written
here. Two implementations of "the price visible at an instant on a venue" is how
the two come to disagree, and this one would disagree quietly -- a NAV is a
plausible number whatever price produced it. The private import is deliberate.

**An empty book still records.** A portfolio holding nothing marks to its cash,
which is the correct NAV and the reason the first point on the curve is the
deposit rather than the first trade.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from omni.portfolio import state
from omni.portfolio.state import UnmarkedPosition
from omni.trading.carry_loop import _price_at, _symbols
from omni.venue.protocol import MarketType, Venue

logger = logging.getLogger("omni.portfolio.nav")


class Unmarkable(Exception):
    """The book holds a position no visible price can value.

    Distinct from a failure to write. Nothing is recorded, deliberately: a NAV
    missing one position reads as authoritative while understating exactly the
    exposure nobody could price.
    """


async def snapshot(
    pool,
    *,
    venue: Venue,
    portfolio_id: UUID,
    entity_ids: Sequence[UUID],
    audience_user_id: UUID | None,
    at: datetime,
) -> Decimal:
    """Record one NAV point for this book, marked as of `at`.

    `at` is required and has no clock default, for the reason every other
    as-of in this system is required: a snapshot that silently reads *now*
    cannot be replayed, and a replay that reads now has lookahead.

    Raises `Unmarkable` when any held position has no visible price on the
    trading venue, and writes nothing.
    """
    if at.tzinfo is None:
        raise ValueError(
            f"at is naive ({at}); claims are stamped UTC and a naive instant "
            f"marks the book against whatever the host's timezone happens to be"
        )

    by_entity = await _symbols(pool, entity_ids, venue)
    entity_of: dict[str, UUID] = {}
    seen: dict[str, UUID] = {}

    def _claim(symbol: str | None, entity_id: UUID) -> None:
        if symbol is None:
            return
        owner = seen.get(symbol)
        if owner is not None and owner != entity_id:
            # Same rule _symbols holds for tradable spellings: two entities
            # sharing one held name cannot both be this position, and marking
            # the first silently would price the book off the wrong asset.
            raise ValueError(
                f"entities {owner} and {entity_id} both hold as {symbol!r}"
            )
        seen[symbol] = entity_id
        entity_of[symbol] = entity_id

    for entity_id, pair in by_entity.items():
        # The tradable spellings, plus the held spellings the venue may report
        # the same market under (Hyperliquid's spot fills and positions carry
        # UETH/USDC while orders are addressed to ETH/USDC). A wrapped holding
        # is a holding of the same asset and prices as its canonical leg;
        # mapping only the tradable spelling reads a hedged book as unmappable
        # and leaves the NAV curve with a hole no later point can fill.
        for market_type in (MarketType.SPOT, MarketType.PERPETUAL):
            _claim(venue.symbol_for(pair.asset, market_type), entity_id)
            for alias in venue.held_symbol_aliases(pair.asset, market_type):
                _claim(alias, entity_id)

    book = await state.load(pool, portfolio_id)
    marks: dict[tuple[str, str], Decimal] = {}
    for position in book.positions:
        if position.venue != venue.name:
            continue
        entity_id = entity_of.get(position.symbol)
        if entity_id is None:
            raise Unmarkable(
                f"{position.symbol} at {position.venue} maps to no entity in the "
                f"universe this snapshot was given, so it cannot be priced; a NAV "
                f"omitting it would understate the book by a whole position"
            )
        price = await _price_at(
            pool,
            entity_id=entity_id,
            audience=audience_user_id,
            at=at,
            venue=venue.name,
        )
        if price is None:
            raise Unmarkable(
                f"no price for {position.symbol} visible on {venue.name} at {at}; "
                f"refusing to record a partially marked NAV"
            )
        marks[(position.venue, position.symbol)] = price

    try:
        return await state.snapshot_nav(pool, portfolio_id, marks)
    except UnmarkedPosition as exc:
        # `snapshot_nav` owns the completeness rule and re-checks it inside the
        # transaction. Reaching here means the book changed between the load
        # above and the write -- the snapshot is simply skipped rather than
        # retried, because the next run is a minute away and a NAV forced
        # through a race is worth less than a gap.
        raise Unmarkable(str(exc)) from exc
