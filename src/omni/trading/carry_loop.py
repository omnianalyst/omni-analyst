"""One rebalance cycle of the delta-neutral funding-carry book.

`trading/loop.py` runs the directional shape: one prediction, one symbol, one
barrier, one fill. This is the other shape MONEY_PLAN section 1 says the system
was missing. A carry harvest asserts **nothing about price**. It holds a *pair*
-- long spot, short perp, equal quantity, same symbol, same venue -- and its
entire return is the funding the short perp receives. There is no direction, no
barrier and no horizon.

**The two legs are one economic unit, and that is the whole safety argument.** A
pair with one leg on the book is not a smaller carry position; it is an outright
directional bet in a strategy that has no view on direction, and the risk engine
sizes it as an ordinary long or short because that is all it can see. So:

- The book is checked for pair integrity **before anything happens**, the way
  `loop.py` reconciles before it looks at a prediction. A venue position that is
  not half of a matched, equal-and-opposite pair halts the cycle by name. A
  cycle that traded on top of an already-naked leg would have compounded it.
- A pair whose two legs do not fill as one -- one leg empty, one leg partial,
  two different quantities, two identical sides -- is **unwound immediately**.
  Refusing to proceed is not enough on its own: refusing leaves the leg that did
  fill exactly where it is, which is the naked position itself. Only a
  compensating trade takes it off.
- If the unwind does not fully restore the leg, the cycle **halts**. At that
  point the book holds an exposure nothing downstream recognises, and every
  further size computed against it is computed against a book that is wrong.

**Every leg goes through the order ledger, including the unwinds.**
`portfolio.state.realised_pnl` derives its number by replaying that ledger and
refuses to report one the ledger cannot account for, so a book whose fills were
applied straight to the position rows raises `UnaccountedClose` -- and the
daily-loss kill switch that reads realised P&L has no input at all on it. The
unwind is the leg it would be most tempting to skip and the one it is least
survivable to skip: it is a position change made precisely when something has
already gone wrong, and a position change the ledger cannot see is the exact
case the kill switch exists for.

The write is a side effect of a decision already taken. Nothing is read back
from the ledger to decide what to trade -- no "have we sent this" gate, no
status check that could refuse a leg -- because a pair whose second leg were
refused by bookkeeping is a naked first leg. What the ledger does provide is
idempotency: the key is
`(portfolio, as_of, symbol, market type, role)`, and the role is what keeps an
unwind distinct from the close it is reversing, which are otherwise the same
symbol, the same leg, the same instant and both `reduce_only`.

**An abstention is not a liquidation.** When the selector abstains -- no visible
funding coverage, too small a universe, a non-finite score -- the book holds
still and no intent is built. Finding 9 measured turnover destroying this
strategy outright (29.19% of cost against 8.74% of gross at the fastest
cadence); selling the basket because the data thinned is turnover the signal
never asked for.

**The funding window is stated, never inferred.** `funding_since` is the
previous cycle's `as_of` and has no default. Settlements are applied over
`(funding_since, as_of]` -- open below because the previous cycle already closed
that boundary, closed above so a settlement landing exactly on a rebalance
instant belongs to this cycle rather than to neither. `apply_funding` is
idempotent on `(portfolio, venue, symbol, funding_time)` so an overlap is
refused by the database, but no mechanism anywhere catches a *gap*: a skipped
settlement is silent and simply understates the only thing this book earns.

**`as_of` is required and has no clock default**, for the same reason
`select_carry_basket` requires one. A replay that silently reads *now* is a
replay with lookahead.

**Funding settles before the rebalance trades.** Position rows carry no history,
so `apply_funding` values a settlement against whatever is held at the moment it
is applied. Applying this cycle's settlements after opening this cycle's pairs
would credit carry to legs that were not held when it settled, and would miss
the carry owed to legs about to be closed.

**Funding and price claims are `byo_only`.** They are visible only to the
credential owner, so `audience_user_id` is the operator whose credential fetched
them; `None` sees the shared network alone, which holds no venue data, and every
read here returns nothing while the selector abstains silently.

This loop assumes the portfolio is a **dedicated** carry book at this venue.
Every position it finds at `venue.name` must be half of a pair, so a directional
position parked in the same portfolio reads as a broken pair and halts it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from uuid import UUID

from omni.conviction.crosssectional import (
    DEFAULT_LOOKBACK_DAYS,
    MIN_SETTLEMENTS,
    select_carry_basket,
)
from omni.coverage.visibility import visible_claims_cte
from omni.portfolio import orders, state
from omni.portfolio.orders import OrderStatus
from omni.portfolio.reconcile import reconcile
from omni.portfolio.state import FundingAccrual, FundingOutcome, PortfolioState
from omni.trading import pretrade
from omni.venue.costs import BPS, entry_cost
from omni.venue.protocol import (
    Fill,
    MarketType,
    Side,
    TradeIntent,
    Venue,
    VenueUnavailable,
)

# Perp first on the way in: perpetuals typically have coarser amount precision
# than spot (SOL perp = 0.01 vs SOL spot = 0.001), and sizing the spot leg at
# the perp's fill quantity ensures the pair balances. Spot first on the way
# out. Both unwind directions work: spot sells no longer carry reduceOnly, and
# perp buy-backs always did.
_OPEN_ORDER = (MarketType.PERPETUAL, MarketType.SPOT)
_CLOSE_ORDER = (MarketType.PERPETUAL, MarketType.SPOT)


class _LegRole(str, Enum):
    """What a leg was sent to do, and the part of its ledger key that says so.

    An unwind reverses a close on the same symbol, the same leg and the same
    instant, and both are `reduce_only`; without the role the two collapse onto
    one idempotency key and the second is silently read as a replay of the
    first.
    """

    OPEN = "open"
    CLOSE = "close"
    UNWIND = "unwind"


_SYMBOLS = "SELECT id, symbol FROM entity WHERE id = ANY($1::uuid[])"

# One settlement is one (entity, event_date) once the venue is fixed. DISTINCT ON
# keeps the most recently knowable version, so a restated rate corrects rather
# than settles twice. `split_part` pins the data venue from the claim key
# (`binance:BTCUSDT`): two venues filing the same asset at the same instant would
# otherwise collide on `apply_funding`'s key and one would vanish silently.
_SETTLEMENTS = """
WITH visible AS (
{visible}
)
SELECT DISTINCT ON (c.entity_id, c.event_date)
       c.entity_id, c.value, c.event_date
FROM visible c
WHERE c.entity_id = ANY($1::uuid[])
  AND c.claim_type = 'funding_rate'
  AND split_part(c.key, ':', 1) = $2
  AND c.knowledge_date <= $3
  AND c.event_date > $4
  AND c.event_date <= $3
ORDER BY c.entity_id, c.event_date, c.knowledge_date DESC
"""

# The mark, point-in-time on both axes against the instant it values: a price
# that happened later, or became knowable later, is lookahead.
#
# And **from the venue the book trades on**. BTC carries price_snapshot claims
# from six sources (binance, okx, bybit, kraken, hyperliquid, and coingecko
# which names no venue at all), so without this filter the mark is whichever one
# published most recently -- a source chosen by luck, changing between cycles,
# and never stated anywhere. Spot arbitrages tightly enough that the error is
# small, which is exactly what makes it survive: it values a book against a
# venue it does not trade on, and the reconciler then compares that valuation to
# the real one and calls the difference a divergence.
_PRICE_AT = """
WITH visible AS (
{visible}
)
SELECT c.value
FROM visible c
WHERE c.entity_id = $2
  AND c.claim_type = 'price_snapshot'
  AND c.value->>'venue' = $4
  AND c.event_date <= $3
  AND c.knowledge_date <= $3
ORDER BY c.event_date DESC, c.knowledge_date DESC
LIMIT 1
"""


class CarryRefusal(str, Enum):
    """Why a name the selector chose did not become a pair on the book."""

    NO_SYMBOL = "the_entity_carries_no_tradeable_symbol"
    NO_REFERENCE_PRICE = "no_visible_price_to_size_the_pair"
    NO_MARK = "no_visible_mark_to_value_the_settlement"
    PAIR_DID_NOT_BALANCE = "the_two_legs_did_not_fill_as_one_unit"
    OUTSIDE_UNIVERSE = "a_held_pair_is_not_in_the_universe_this_cycle_was_given"
    DUST_BELOW_VENUE_MINIMUM = "a_single_leg_below_the_venues_minimum_order_size"


_PAIR_DUST_FRACTION = Decimal("0.005")


class CarryHalt(str, Enum):
    """Why the cycle stopped rather than refusing one name."""

    BOOK_NOT_PAIRED = "the_book_already_held_a_leg_that_is_not_half_of_a_pair"
    UNWIND_FAILED = "a_half_filled_pair_could_not_be_unwound"
    VENUE_DISAGREES = "the_venue_and_the_book_do_not_agree"
    RISK_POLICY = "the_carry_risk_policy_refused_the_cycle"
    UNRESOLVED_PAIR = "a_held_pair_cannot_be_mapped_for_funding_accrual"
    ACCRUAL_INCOMPLETE = "a_held_pair_cannot_accrue_the_complete_funding_window"


@dataclass(frozen=True)
class CarryRiskPolicy:
    max_gross_notional: Decimal
    daily_loss_limit_pct_nav: Decimal
    max_drawdown_pct: Decimal

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_gross_notional, Decimal)
            or not self.max_gross_notional.is_finite()
            or self.max_gross_notional <= 0
        ):
            raise ValueError(
                f"max_gross_notional must be a positive finite Decimal, got "
                f"{self.max_gross_notional!r}"
            )
        for name in ("daily_loss_limit_pct_nav", "max_drawdown_pct"):
            value = getattr(self, name)
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value <= 0
                or value > 1
            ):
                raise ValueError(
                    f"{name} must be a finite Decimal in (0, 1], got {value!r}"
                )


@dataclass(frozen=True)
class CarryConfig:
    """What the cycle is permitted to do, decided before it runs.

    `notional_per_pair` is stated rather than derived from NAV. The basket is
    capped at `enter_rank` names (Finding 13), which is what makes per-name
    capital knowable in advance -- but the spot leg consumes cash while the perp
    leg consumes margin, so the capital a pair actually ties up is a property of
    the account, not of this module, and inventing a split here would put a
    fabricated number under every size.

    `spread_bps` has no default. The cost model charges half the spread on a
    taker leg, and a default of zero is the permissive value: it hands the
    strategy the spread on every leg of the turnover Finding 9 measured this
    strategy as being destroyed by.

    `reconciliation_tolerance` has no default either, and for a sharper reason:
    the permissive value is not zero here but a LARGE one. A tolerance wide
    enough to swallow a real divergence turns the reconciliation into a
    formality that always passes, and the wider it is the more it looks like
    working code. It is stated per deployment because what counts as noise
    depends on the venue's rounding and the size of the book, and nothing in
    this module knows either.
    """

    enter_rank: int
    exit_rank: int
    notional_per_pair: Decimal
    funding_venue: str
    spread_bps: Decimal
    reconciliation_tolerance: Decimal
    risk_policy: CarryRiskPolicy
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    min_settlements: int = MIN_SETTLEMENTS

    def __post_init__(self) -> None:
        if not isinstance(self.notional_per_pair, Decimal):
            raise TypeError(
                f"notional_per_pair must be a Decimal, got "
                f"{type(self.notional_per_pair).__name__}; a float notional carries a "
                f"binary error into the quantity of both legs"
            )
        if not self.notional_per_pair.is_finite() or self.notional_per_pair <= 0:
            raise ValueError(
                f"notional_per_pair must be a positive amount, got "
                f"{self.notional_per_pair}"
            )
        if not isinstance(self.spread_bps, Decimal):
            raise TypeError(
                f"spread_bps must be a Decimal, got {type(self.spread_bps).__name__}"
            )
        if not self.spread_bps.is_finite() or self.spread_bps < 0:
            raise ValueError(f"spread_bps must not be negative, got {self.spread_bps}")
        if not isinstance(self.reconciliation_tolerance, Decimal):
            raise TypeError(
                f"reconciliation_tolerance must be a Decimal, got "
                f"{type(self.reconciliation_tolerance).__name__}"
            )
        if (
            not self.reconciliation_tolerance.is_finite()
            or self.reconciliation_tolerance < 0
        ):
            raise ValueError(
                f"reconciliation_tolerance must not be negative, got "
                f"{self.reconciliation_tolerance}"
            )
        if not self.funding_venue or not self.funding_venue.strip():
            raise ValueError(
                "funding_venue must name the data venue whose funding stream is "
                "harvested; without it two venues' settlements for one asset collide "
                "on the accrual key and one is silently dropped"
            )
        if not isinstance(self.risk_policy, CarryRiskPolicy):
            raise TypeError(
                "risk_policy must be an explicit CarryRiskPolicy; a missing policy "
                "is a carry cycle with no loss, gross-notional, or drawdown boundary"
            )


@dataclass(frozen=True)
class PairExecution:
    """A pair that moved as one unit, with the fill for each leg.

    Constructed only after both legs came back matched, so the guards here are a
    second reading of the same fact rather than the check itself -- but they are
    the fact the whole strategy rests on, and a result object that can hold an
    unbalanced pair is a result object an operator can be misled by.

    `closed_at_own_size` is the close path for a pair the venue holds at
    slightly unequal sizes (fill-replay dust): each leg closed exactly the
    quantity it held, which is what makes the book flat -- the equal-quantity
    invariant is the OPEN's statement about a pair born this cycle, and
    demanding it on close would refuse to finish unwinding the dust it exists
    to remove.
    """

    entity_id: UUID
    symbol: str
    spot: Fill
    perp: Fill
    closed_at_own_size: bool = False

    def __post_init__(self) -> None:
        if self.spot.side is self.perp.side:
            raise ValueError(
                f"both legs of {self.symbol} are {self.spot.side.value}; a pair whose "
                f"legs point the same way is a doubled directional position, not a "
                f"delta-neutral one"
            )
        if (
            not self.closed_at_own_size
            and self.spot.filled_quantity != self.perp.filled_quantity
        ):
            raise ValueError(
                f"{self.symbol} filled {self.spot.filled_quantity} spot against "
                f"{self.perp.filled_quantity} perp; the residual is naked exposure"
            )

    @property
    def quantity(self) -> Decimal:
        return self.spot.filled_quantity


@dataclass(frozen=True)
class CarryCycleResult:
    """What one rebalance did, what it collected, and why it did not do the rest.

    `held` is read back from the book after trading rather than predicted from
    the decision, so a name the selector chose and the venue refused is absent
    here rather than reported as held.

    `funding_settled_through` is `as_of` once the complete settlement window has
    been applied and `None` when any held pair could not accrue. A caller
    persisting the boundary for the next cycle needs that distinction, and the
    safe direction is `None` -- a partial window is re-walked into idempotent
    writes, while a skipped settlement is silent.
    """

    as_of: datetime
    held: frozenset[UUID]
    opened: tuple[PairExecution, ...]
    closed: tuple[PairExecution, ...]
    funding: tuple[FundingAccrual, ...]
    funding_collected: Decimal
    modelled_turnover_cost: Decimal
    fees_paid: Decimal
    refused: dict[str, int]
    abstention: str | None
    halted: bool
    halt_reason: str | None
    funding_settled_through: datetime | None = None

    def __post_init__(self) -> None:
        if self.halted and not self.halt_reason:
            raise ValueError("a halted cycle must name the reason it halted")
        if (
            self.funding_settled_through is not None
            and self.funding_settled_through != self.as_of
        ):
            raise ValueError(
                f"funding_settled_through is {self.funding_settled_through} against "
                f"an as_of of {self.as_of}; the window a cycle settles closes at its "
                f"own rebalance instant and nowhere else"
            )
        if not self.halted and self.halt_reason is not None:
            raise ValueError(
                f"a cycle that ran to completion carries no halt reason, got "
                f"{self.halt_reason!r}"
            )
        if self.abstention is not None and (self.opened or self.closed):
            raise ValueError(
                f"the cycle abstained ({self.abstention}) and still traded "
                f"{len(self.opened)} opens and {len(self.closed)} closes; an "
                f"abstention is the book holding still, not a liquidation"
            )
        accrued = sum(
            (
                a.amount
                for a in self.funding
                if a.outcome is FundingOutcome.ACCRUED and a.amount is not None
            ),
            Decimal(0),
        )
        if self.funding_collected != accrued:
            raise ValueError(
                f"funding_collected is {self.funding_collected} but the accruals "
                f"recorded sum to {accrued}; carry per unit time is the only thing "
                f"this strategy is graded on and a total that does not reconcile to "
                f"its own settlements cannot grade it"
            )


def _decimal(raw: object) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _payload(raw: object) -> dict | None:
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    return raw if isinstance(raw, dict) else None


async def _symbols(
    pool, entity_ids: Sequence[UUID], venue: Venue
) -> dict[UUID, _PairSymbols]:
    """The VENUE symbol per entity. Raises when two entities share one.

    An entity row carries an asset -- `BTC` -- and a venue trades an instrument
    on a pair. The asset is resolved through `venue.symbol_for` rather than used
    directly, because the two namespaces are not the same and passing a ticker
    straight through is how a fill came to settle in an asset called `MKR` while
    the venue's cash never moved (Finding 21). A live venue is blunter: it does
    not list `MKR`, so the order is rejected and the pair half-opens on whichever
    leg went first.

    Both legs must resolve. A name the venue lists for spot but not as a
    perpetual cannot be held delta neutral at all, and taking the spot leg alone
    is an outright long in a strategy with no view on direction.

    An asset the venue does not list is simply absent from the mapping, which
    the caller reads as `NO_SYMBOL` and skips -- routine for a 30-name universe
    against one exchange, where two names are currently delisted.

    Two entities on one symbol cannot both be held: their legs would net into a
    single pair of position rows and the book would report one name holding
    twice the size.
    """
    rows = await pool.fetch(_SYMBOLS, list(entity_ids))
    by_entity: dict[UUID, _PairSymbols] = {}
    seen: dict[str, UUID] = {}
    for row in rows:
        asset = row["symbol"]
        if not asset or not asset.strip():
            continue
        spot = venue.symbol_for(asset, MarketType.SPOT)
        perp = venue.symbol_for(asset, MarketType.PERPETUAL)
        if spot is None or perp is None:
            # Not listed here, or listed on only one side. Either way there is
            # no delta-neutral pair to hold, and taking the leg that does exist
            # is an outright position in a strategy with no view on direction.
            continue
        for symbol in {spot, perp}:
            # Collision on EITHER leg. Two assets sharing one venue symbol would
            # net into a single pair of position rows and the book would report
            # one name holding twice the size.
            if symbol in seen and seen[symbol] != row["id"]:
                raise ValueError(
                    f"entities {seen[symbol]} and {row['id']} both trade as "
                    f"{symbol!r}; their legs would net into one pair of position "
                    f"rows"
                )
            seen[symbol] = row["id"]
        by_entity[row["id"]] = _PairSymbols(asset=asset, spot=spot, perp=perp)
    return by_entity


def _pairing_map(by_entity: Mapping[UUID, _PairSymbols], venue: Venue) -> dict[str, str]:
    """Every leg symbol -> its asset, including held spellings.

    `held_symbol_aliases` adds the venue's raw market ids beside the tradable
    symbols: a position recorded through a venue-side fill (the wrapped
    `UETH/USDC` spelling on Hyperliquid) is the same market as the tradable
    `ETH/USDC`, and a pairing map that recognises only the tradable spelling
    reads a hedged wrapped book as unpaired legs and halts on it. An alias
    colliding with another asset's symbol is the same netting hazard as a
    symbol collision and is refused here rather than read ambiguously.
    """
    asset_of: dict[str, str] = {}
    # Structural default: a venue that has not measured any raw/tradable
    # divergence answers nothing, which reads the book exactly as before.
    aliases = getattr(venue, "held_symbol_aliases", None)
    for pair in by_entity.values():
        for symbol, market_type in (
            (pair.spot, MarketType.SPOT),
            (pair.perp, MarketType.PERPETUAL),
        ):
            if symbol in asset_of and asset_of[symbol] != pair.asset:
                raise ValueError(
                    f"symbol {symbol!r} resolves to both {asset_of[symbol]!r} "
                    f"and {pair.asset!r}; one leg symbol must name one asset"
                )
            asset_of[symbol] = pair.asset
            if aliases is None:
                continue
            for alias in aliases(pair.asset, market_type):
                if alias in asset_of and asset_of[alias] != pair.asset:
                    raise ValueError(
                        f"held alias {alias!r} resolves to both "
                        f"{asset_of[alias]!r} and {pair.asset!r}; one leg "
                        f"symbol must name one asset"
                    )
                asset_of[alias] = pair.asset
    return asset_of



async def _price_at(
    pool,
    *,
    entity_id: UUID,
    audience: UUID | None,
    at: datetime,
    venue: str,
) -> Decimal | None:
    """The last price knowable at `at` **on `venue`**, or None. Never substituted.

    `venue` is required rather than defaulted, on the same reasoning as
    `funding_venue`: a book marked against the wrong venue is not obviously
    wrong at any single price, and every default here picks one silently.
    Returning None when the trading venue has not priced an asset is the safe
    outcome -- the cycle refuses that name with `NO_MARK` rather than valuing it
    off a venue it cannot trade.
    """
    row = await pool.fetchrow(
        _PRICE_AT.format(visible=visible_claims_cte("$1")),
        audience,
        entity_id,
        at,
        venue,
    )
    if row is None:
        return None
    payload = _payload(row["value"])
    if payload is None:
        return None
    scalar = payload.get("close")
    if scalar is None:
        scalar = payload.get("price")
    price = _decimal(scalar)
    if price is None or price <= 0:
        return None
    return price


async def _settlements(
    pool,
    *,
    entity_ids: Sequence[UUID],
    audience: UUID | None,
    funding_venue: str,
    since: datetime,
    until: datetime,
) -> list[tuple[datetime, UUID, Decimal]]:
    """Every settlement in `(since, until]`, oldest-first across all names.

    Chronological across the whole book rather than per name: `apply_funding`
    values a settlement against the position as it stands when it is applied, so
    the order settlements are applied in is the order they happened in.
    """
    if not entity_ids:
        return []
    rows = await pool.fetch(
        _SETTLEMENTS.format(visible=visible_claims_cte("$5")),
        list(entity_ids),
        funding_venue,
        until,
        since,
        audience,
    )
    settlements: list[tuple[datetime, UUID, Decimal]] = []
    for row in rows:
        payload = _payload(row["value"])
        if payload is None:
            continue
        rate = _decimal(payload.get("rate"))
        if rate is None:
            continue
        settlements.append((row["event_date"], row["entity_id"], rate))
    settlements.sort(key=lambda s: (s[0], str(s[1])))
    return settlements


@dataclass(frozen=True)
class _PairSymbols:
    """What one asset is called on this venue, per leg.

    A real exchange does not name the two legs alike: ccxt spells the spot
    market `BTC/USDT` and the perpetual `BTC/USDT:USDT`. So a pair is addressed
    by ASSET and traded by two symbols, and everything downstream that reaches
    the venue -- the intent, the order ledger key, the funding accrual -- has to
    carry the one that belongs to its leg.

    `PaperVenue` returns the same string for both, which is why the paper path
    worked while this was a single symbol and live could not have.
    """

    asset: str
    spot: str
    perp: str

    def for_market(self, market_type: MarketType) -> str:
        return self.spot if market_type is MarketType.SPOT else self.perp


@dataclass(frozen=True)
class _Legs:
    """The two position quantities held for one asset at one venue."""

    spot: Decimal
    perp: Decimal

    @property
    def is_pair(self) -> bool:
        # Decimal addition is exact, so an intact pair lands on exactly zero
        # rather than near it; the float tolerance rule does not apply here.
        # The one exception is measured dust: a book whose legs came back from
        # a venue-side fill replay can differ by the venue's own amount step
        # (live 2026-08-19: spot 0.912 against perp -0.91 -- 0.22%), and an
        # exact-zero rule would refuse to exit a hedged book over the
        # remainder, stranding it forever. The bound is relative and small:
        # half a percent of the larger leg. A naked leg (one side zero) is a
        # 100% residual and stays unpaired; wrong-sign legs stay unpaired.
        if self.spot <= 0 or self.perp >= 0:
            return False
        residual = abs(self.spot + self.perp)
        return residual == 0 or residual <= _PAIR_DUST_FRACTION * max(
            self.spot, -self.perp
        )


def _legs_by_asset(
    book: PortfolioState, *, venue_name: str, asset_of: Mapping[str, str]
) -> dict[str, _Legs]:
    """The book's two legs per asset at this venue.

    Grouped by ASSET rather than by symbol, because the two legs of one pair
    carry different symbols wherever the venue names them differently, and
    grouping by symbol would then read every leg as half of a broken pair and
    halt the cycle on a book that is perfectly hedged.

    A position whose symbol this cycle cannot map back to an asset is keyed by
    its raw symbol. It is still counted, and still halts as unpaired, because a
    position at this venue that the cycle does not recognise is exactly the
    directional exposure the pair gate exists to catch -- and dropping it would
    make the book look clean by not looking.
    """
    legs: dict[str, tuple[Decimal, Decimal]] = {}
    for position in book.positions:
        if position.venue != venue_name:
            continue
        if position.market_type not in (MarketType.SPOT, MarketType.PERPETUAL):
            continue
        key = asset_of.get(position.symbol, position.symbol)
        spot, perp = legs.get(key, (Decimal(0), Decimal(0)))
        if position.market_type is MarketType.SPOT:
            spot = position.quantity
        else:
            perp = position.quantity
        legs[key] = (spot, perp)
    return {symbol: _Legs(spot=s, perp=p) for symbol, (s, p) in legs.items()}


def _unpaired(legs: dict[str, _Legs]) -> list[str]:
    return sorted(symbol for symbol, held in legs.items() if not held.is_pair)


_PEAK_NAV = "SELECT max(nav) FROM nav_snapshot WHERE portfolio_id = $1"


async def _carry_risk_reason(
    pool,
    *,
    book: PortfolioState,
    config: CarryConfig,
    as_of: datetime,
) -> str | None:
    policy = config.risk_policy
    failures: list[str] = []

    if book.nav <= 0:
        failures.append(f"NAV is {book.nav}, so percentage limits cannot be measured")
    else:
        authorised_gross = (
            Decimal(2) * Decimal(config.enter_rank) * config.notional_per_pair
        )
        if authorised_gross > policy.max_gross_notional:
            failures.append(
                f"configured pair authority is {authorised_gross} gross against the "
                f"{policy.max_gross_notional} cap"
            )
        if book.gross_exposure > policy.max_gross_notional:
            failures.append(
                f"current gross exposure is {book.gross_exposure} against the "
                f"{policy.max_gross_notional} cap"
            )

        day_opens = as_of.astimezone(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        realised = await state.realised_pnl(
            pool, book.portfolio_id, since=day_opens, until=as_of
        )
        daily_limit = policy.daily_loss_limit_pct_nav * book.nav
        if realised < 0 and -realised > daily_limit:
            failures.append(
                f"today's realised loss {-realised} exceeds the {daily_limit} limit"
            )

        recorded_peak = await pool.fetchval(_PEAK_NAV, book.portfolio_id)
        peak_nav = book.nav if recorded_peak is None else max(recorded_peak, book.nav)
        drawdown = (peak_nav - book.nav) / peak_nav
        if drawdown > policy.max_drawdown_pct:
            failures.append(
                f"drawdown {drawdown} from peak NAV {peak_nav} exceeds "
                f"{policy.max_drawdown_pct}"
            )

    return "; ".join(failures) if failures else None


def _leg_intent(
    *,
    portfolio_id: UUID,
    venue_name: str,
    symbol: str,
    market_type: MarketType,
    side: Side,
    quantity: Decimal,
    reference_price: Decimal,
    as_of: datetime,
    reduce_only: bool,
    role: _LegRole,
) -> TradeIntent:
    return TradeIntent(
        venue=venue_name,
        symbol=symbol,
        side=side,
        market_type=market_type,
        quantity=quantity,
        reference_price=reference_price,
        reduce_only=reduce_only,
        provenance={"as_of": as_of, "strategy": "carry.crosssectional"},
        idempotency_key=(
            f"{portfolio_id}:carry:{as_of.isoformat()}:{symbol}:"
            f"{market_type.value}:{role.value}"
        ),
    )


def _leg_cost(intent: TradeIntent, fill: Fill, *, venue: Venue, spread_bps: Decimal) -> Decimal:
    """What the cost model says this leg cost, in quote currency.

    Priced off the fill's own notional rather than the intent's, so a partial
    fill is not charged for size that never traded. `is_maker=False` is stated
    rather than defaulted: every intent here is a market order, and a maker
    assumption would halve the modelled friction on the exact quantity Finding 9
    measured the strategy against.
    """
    if fill.is_empty:
        return Decimal(0)
    cost = entry_cost(intent, venue.capabilities, spread_bps=spread_bps, is_maker=False)
    return cost.total_bps / BPS * fill.notional


class _Cycle:
    """Mutable bookkeeping for one cycle, so the steps below stay readable."""

    def __init__(self, pool, *, venue: Venue, portfolio_id: UUID, config: CarryConfig):
        self.pool = pool
        self.venue = venue
        self.portfolio_id = portfolio_id
        self.config = config
        self.refused: dict[str, int] = {}
        self.modelled_cost = Decimal(0)
        self.fees_paid = Decimal(0)

    def refuse(self, reason: CarryRefusal) -> None:
        self.refused[reason.value] = self.refused.get(reason.value, 0) + 1

    async def send(self, intent: TradeIntent) -> Fill | None:
        """Execute one leg, record it, and apply it to the book. None when refused.

        The ledger write happens before the venue is called and the venue's
        answer is written back to it, on every outcome. That ordering is what
        makes a leg the ledger cannot explain impossible rather than unlikely: a
        fill that arrives against an order nobody instructed is a position
        `realised_pnl` refuses to value, and the daily-loss kill switch reading
        it goes blind on the whole book rather than on the one leg.

        Nothing is read back from the ledger to decide anything. There is no
        status gate here of the kind `loop.py` carries, because a leg refused by
        bookkeeping is half a pair, and half a pair is the naked directional
        position this entire strategy is built to not hold.

        The fill is applied here rather than after both legs are known, because a
        fill that happened is on the book whether or not its partner filled --
        recording it only on the happy path is how a real position stops being
        visible to everything downstream.
        """
        order_id = await orders.record_intent(self.pool, self.portfolio_id, intent)
        await orders.transition(self.pool, order_id, OrderStatus.SUBMITTED)
        try:
            fill = await self.venue.execute(intent)
        except VenueUnavailable as exc:
            await orders.transition(
                self.pool,
                order_id,
                OrderStatus.REJECTED,
                payload={"venue_unavailable": str(exc)},
            )
            return None
        self.modelled_cost += _leg_cost(
            intent, fill, venue=self.venue, spread_bps=self.config.spread_bps
        )
        if fill.is_empty:
            await orders.transition(
                self.pool,
                order_id,
                OrderStatus.REJECTED,
                external_id=fill.external_id,
                payload={"empty_fill": fill.raw},
            )
            return fill
        self.fees_paid += fill.fee_paid
        await orders.record_fill(self.pool, order_id, fill)
        await state.apply_fill(self.pool, self.portfolio_id, fill, intent.market_type)
        return fill

    async def unwind(
        self,
        *,
        symbols: _PairSymbols,
        filled: dict[MarketType, Fill],
        reference_price: Decimal,
        as_of: datetime,
    ) -> str | None:
        """Take the legs that did fill back off the book. Returns why it could not.

        This is the answer to the single most dangerous failure in this strategy.
        Refusing to proceed does not help: the leg that filled is already on the
        book, it is delta one, and nothing downstream knows it was meant to be
        half of something. The only action that restores neutrality is the
        opposite trade, so it is sent immediately and its fill is checked against
        the quantity it was meant to remove.
        """
        for market_type, fill in filled.items():
            if fill.is_empty:
                continue
            opposite = Side.SELL if fill.side is Side.BUY else Side.BUY
            back = await self.send(
                _leg_intent(
                    portfolio_id=self.portfolio_id,
                    venue_name=self.venue.name,
                    symbol=symbols.for_market(market_type),
                    market_type=market_type,
                    side=opposite,
                    quantity=fill.filled_quantity,
                    reference_price=reference_price,
                    as_of=as_of,
                    reduce_only=True,
                    role=_LegRole.UNWIND,
                )
            )
            if back is None:
                return (
                    f"{symbols.asset} filled {fill.filled_quantity} on the "
                    f"{market_type.value} leg and the venue could not execute the "
                    f"unwind; the book holds a naked {fill.side.value}"
                )
            if back.filled_quantity != fill.filled_quantity:
                return (
                    f"{symbols.asset} filled {fill.filled_quantity} on the "
                    f"{market_type.value} leg and only {back.filled_quantity} of it "
                    f"could be unwound; the remainder is naked exposure"
                )
        return None

    async def trade_pair(
        self,
        *,
        symbols: _PairSymbols,
        entity_id: UUID,
        quantity: Decimal,
        reference_price: Decimal,
        as_of: datetime,
        opening: bool,
        leg_quantities: dict[MarketType, Decimal] | None = None,
    ) -> tuple[PairExecution | None, str | None]:
        """Send both legs of one pair. Returns the pair, or the halt it caused.

        A pair that did not come back matched is unwound and refused; the cycle
        carries on with the other names, because two pairs are two independent
        economic units and one venue rejection says nothing about the next name.
        Only an unwind that fails halts, and it halts because at that point the
        book is holding an exposure nothing else can see.

        `leg_quantities` is the close path only, for a pair the venue holds at
        slightly unequal sizes (measured fill-replay dust): each leg is sent at
        ITS OWN held quantity, so closing leaves both legs exactly flat rather
        than buying back more perp than was short and minting a new residual.
        """
        # `symbols` rather than one string: on a real venue the two legs are
        # different instruments with different names, and sending the spot
        # symbol on the perpetual leg opens a second spot position instead of
        # the hedge.
        order = _OPEN_ORDER if opening else _CLOSE_ORDER
        sides = (
            {MarketType.SPOT: Side.BUY, MarketType.PERPETUAL: Side.SELL}
            if opening
            else {MarketType.SPOT: Side.SELL, MarketType.PERPETUAL: Side.BUY}
        )

        role = _LegRole.OPEN if opening else _LegRole.CLOSE

        filled: dict[MarketType, Fill] = {}
        balanced = True
        # The second leg is sized at the first leg's fill quantity, not the raw
        # quantity, so both legs fill the same amount even when the two markets
        # have different amount precisions (SOL perp = 0.01 vs spot = 0.001).
        # With per-leg quantities every leg carries its own size and no leg is
        # resized off another's fill.
        send_qty = quantity
        for market_type in order:
            if leg_quantities is not None:
                send_qty = leg_quantities[market_type]
            fill = await self.send(
                _leg_intent(
                    portfolio_id=self.portfolio_id,
                    venue_name=self.venue.name,
                    symbol=symbols.for_market(market_type),
                    market_type=market_type,
                    side=sides[market_type],
                    quantity=send_qty,
                    reference_price=reference_price,
                    as_of=as_of,
                    reduce_only=not opening,
                    role=role,
                )
            )
            if fill is None or fill.is_empty or fill.raw.get("partial", False):
                balanced = False
                if fill is not None:
                    filled[market_type] = fill
                break
            filled[market_type] = fill
            if leg_quantities is None:
                send_qty = fill.filled_quantity

        spot = filled.get(MarketType.SPOT)
        perp = filled.get(MarketType.PERPETUAL)
        # Stated across the two legs, not per leg. A per-leg check ("did this leg
        # fill what it was asked for") passes cleanly on a pair sized wrong in
        # the first place, which is the same naked residual arriving by a
        # different route and with nothing left to notice it.
        neutral = (
            spot is not None
            and perp is not None
            and spot.side is not perp.side
            and (
                spot.filled_quantity == perp.filled_quantity
                if leg_quantities is None
                else all(
                    abs(fill.filled_quantity - leg_quantities[market_type])
                    <= _PAIR_DUST_FRACTION * abs(leg_quantities[market_type])
                    for market_type, fill in filled.items()
                )
            )
        )
        if balanced and neutral:
            return (
                PairExecution(
                    entity_id=entity_id,
                    symbol=symbols.asset,
                    spot=spot,
                    perp=perp,
                    closed_at_own_size=leg_quantities is not None,
                ),
                None,
            )

        self.refuse(CarryRefusal.PAIR_DID_NOT_BALANCE)
        halt = await self.unwind(
            symbols=symbols, filled=filled, reference_price=reference_price, as_of=as_of
        )
        return None, halt


def _result(
    *,
    as_of: datetime,
    held: frozenset[UUID],
    cycle: _Cycle,
    opened: Sequence[PairExecution] = (),
    closed: Sequence[PairExecution] = (),
    funding: Sequence[FundingAccrual] = (),
    abstention: str | None = None,
    halt_reason: str | None = None,
    settled: bool = False,
) -> CarryCycleResult:
    collected = sum(
        (
            a.amount
            for a in funding
            if a.outcome is FundingOutcome.ACCRUED and a.amount is not None
        ),
        Decimal(0),
    )
    return CarryCycleResult(
        as_of=as_of,
        held=held,
        opened=tuple(opened),
        closed=tuple(closed),
        funding=tuple(funding),
        funding_collected=collected,
        modelled_turnover_cost=cycle.modelled_cost,
        fees_paid=cycle.fees_paid,
        refused=dict(cycle.refused),
        abstention=abstention,
        halted=halt_reason is not None,
        halt_reason=halt_reason,
        # Default False, so a return added later that forgets to say it settled
        # reports the conservative answer: the boundary does not advance and the
        # window is re-walked into an idempotent write.
        funding_settled_through=as_of if settled else None,
    )


async def run_carry_cycle(
    pool,
    *,
    venue: Venue,
    portfolio_id: UUID,
    config: CarryConfig,
    entity_ids: Sequence[UUID],
    audience_user_id: UUID | None,
    as_of: datetime,
    funding_since: datetime,
) -> CarryCycleResult:
    """Settle funding, ask the selector, and move the book to the basket it names.

    In order, and the order is the argument:

    1. **Pair integrity first.** Every position at this venue must be half of a
       matched, equal-and-opposite pair. A cycle that traded on top of a naked
       leg would have compounded it into every size it computed afterwards.
    2. **Funding for `(funding_since, as_of]`**, applied to the perpetual leg
       only, against the book as it stood -- before this cycle's trades move it.
    3. **The selector**, as-of `as_of`. An abstention returns here having traded
       nothing.
    4. **Exits, then entries**, each as a pair, each unwound if its legs do not
       come back matched.

    `funding_since` is the previous cycle's `as_of`. It has no default because
    every default is wrong in the direction that loses money quietly: one
    settlement period skips every settlement in a longer gap, and the portfolio's
    inception re-walks history that was already collected.
    """
    for name, value in (("as_of", as_of), ("funding_since", funding_since)):
        if value.tzinfo is None:
            raise ValueError(
                f"{name} is naive ({value}); settlements and claims are stamped UTC "
                f"and a naive bound silently shifts the window"
            )
    if funding_since > as_of:
        raise ValueError(
            f"funding_since {funding_since} is after as_of {as_of}; the window a "
            f"cycle settles runs forward from the previous cycle"
        )
    if not entity_ids:
        raise ValueError(
            "entity_ids is empty; the universe a cross-sectional rank is taken over "
            "must be stated, and an empty one abstains as though coverage were missing"
        )
    cycle = _Cycle(pool, venue=venue, portfolio_id=portfolio_id, config=config)
    by_entity = await _symbols(pool, entity_ids, venue)
    # Asset -> entity, and every leg symbol -> asset. Two maps because the pair
    # is identified by its asset and addressed by two symbols.
    by_asset = {pair.asset: entity_id for entity_id, pair in by_entity.items()}
    asset_of = _pairing_map(by_entity, venue)

    book = await state.load(pool, portfolio_id)
    legs = _legs_by_asset(book, venue_name=venue.name, asset_of=asset_of)
    held_ids = frozenset(
        by_asset[symbol]
        for symbol, held in legs.items()
        if held.is_pair and symbol in by_asset
    )

    unpaired = _unpaired(legs)
    if unpaired:
        return _result(
            as_of=as_of,
            held=held_ids,
            cycle=cycle,
            halt_reason=(
                f"{CarryHalt.BOOK_NOT_PAIRED.value}: "
                + "; ".join(
                    f"{symbol} holds {legs[symbol].spot} spot against "
                    f"{legs[symbol].perp} perp"
                    for symbol in unpaired
                )
            ),
        )

    unresolved = sorted(
        symbol for symbol, held in legs.items() if held.is_pair and symbol not in by_asset
    )
    if unresolved:
        for _ in unresolved:
            cycle.refuse(CarryRefusal.OUTSIDE_UNIVERSE)
        return _result(
            as_of=as_of,
            held=held_ids,
            cycle=cycle,
            halt_reason=(
                f"{CarryHalt.UNRESOLVED_PAIR.value}: " + ", ".join(unresolved)
            ),
        )

    risk_reason = await _carry_risk_reason(
        pool, book=book, config=config, as_of=as_of
    )
    if risk_reason is not None:
        return _result(
            as_of=as_of,
            held=held_ids,
            cycle=cycle,
            halt_reason=f"{CarryHalt.RISK_POLICY.value}: {risk_reason}",
        )

    for market_type in (MarketType.SPOT, MarketType.PERPETUAL):
        if not venue.capabilities.supports(market_type):
            raise ValueError(
                f"{venue.name} does not support {market_type.value}; a carry pair "
                f"needs both legs at one venue and half of one is a directional bet"
            )

    # Reconciliation, AFTER the pair-integrity gate and before anything else.
    #
    # The order is deliberate and was arrived at by getting it wrong first. With
    # reconciliation first, a book already holding a naked leg never reaches
    # BOOK_NOT_PAIRED: a leg the book holds and the venue does not is also a
    # position divergence, so the cash and position comparison fires and reports
    # the same fact in a less useful vocabulary. The pair gate names the symbol
    # and which side is missing; the reconciler names a quantity mismatch. Both
    # are true, and the operator wants the first.
    #
    # Before funding, because a settlement applied against a book the venue does
    # not confirm is carry credited to a position that may not exist, and
    # `apply_funding` is idempotent on `(portfolio, venue, symbol, funding_time)`
    # -- so the wrong accrual, once written, is the one that stands.
    verified = await reconcile(
        book.positions,
        book.cash_positions,
        venue,
        tolerance=config.reconciliation_tolerance,
        now=as_of,
    )
    unrecorded = await pretrade.record_reconciliation(
        pool, verified, portfolio_id=portfolio_id
    )
    await pretrade.evaluate_risk_alerts(pool, portfolio_id=portfolio_id, now=as_of)

    if not verified:
        halt_reason = (
            f"{CarryHalt.VENUE_DISAGREES.value}: "
            + "; ".join(d.detail for d in verified.discrepancies)
        )
        # The halt is computed from `verified` alone. A write that failed is
        # named here so the operator learns the evidence is not in the store,
        # and it must never be able to turn a divergence into a pass.
        if unrecorded is not None:
            halt_reason = f"{halt_reason} -- and {unrecorded}"
        return _result(
            as_of=as_of, held=held_ids, cycle=cycle, halt_reason=halt_reason
        )

    settlements = await _settlements(
        pool,
        entity_ids=sorted(held_ids, key=str),
        audience=audience_user_id,
        funding_venue=config.funding_venue,
        since=funding_since,
        until=as_of,
    )
    valued: list[tuple[datetime, UUID, Decimal, Decimal]] = []
    for funding_time, entity_id, rate in settlements:
        mark = await _price_at(
            pool,
            entity_id=entity_id,
            audience=audience_user_id,
            at=funding_time,
            venue=venue.name,
        )
        if mark is None:
            cycle.refuse(CarryRefusal.NO_MARK)
            continue
        valued.append((funding_time, entity_id, rate, mark))

    if len(valued) != len(settlements):
        return _result(
            as_of=as_of,
            held=held_ids,
            cycle=cycle,
            halt_reason=(
                f"{CarryHalt.ACCRUAL_INCOMPLETE.value}: "
                f"{len(settlements) - len(valued)} settlement(s) have no visible mark"
            ),
        )

    funding: list[FundingAccrual] = []
    funding_complete = True
    for funding_time, entity_id, rate, mark in valued:
        accrual = await state.apply_funding(
            pool,
            portfolio_id,
            venue=venue.name,
            # The perpetual leg: funding exists only there, and the accrual
            # key must name the instrument that actually pays it.
            symbol=by_entity[entity_id].perp,
            funding_time=funding_time,
            funding_rate=rate,
            mark=mark,
        )
        funding.append(accrual)
        if accrual.outcome is FundingOutcome.NO_POSITION:
            funding_complete = False
        # A real exchange settles funding itself and a caller learns of it by
        # reading balances. A simulated venue has no schedule of its own, so it
        # is told -- and only if it says it can be told. Omitting this is not
        # neutral: the book credits the settlement and the venue does not, so
        # the two diverge by exactly the carry earned, compounding every cycle
        # until reconciliation halts the book. Guarded by capability rather than
        # by venue type, because a live venue that needed telling would be a
        # venue whose exchange was not paying us.
        credit = getattr(venue, "credit_funding", None)
        if credit is not None and accrual.outcome is FundingOutcome.ACCRUED:
            credit(by_entity[entity_id].perp, accrual.amount)

    if not funding_complete:
        return _result(
            as_of=as_of,
            held=held_ids,
            cycle=cycle,
            funding=funding,
            halt_reason=(
                f"{CarryHalt.ACCRUAL_INCOMPLETE.value}: a mapped perpetual position "
                f"was absent while its settlement was applied"
            ),
        )

    decision = await select_carry_basket(
        pool,
        entity_ids=list(entity_ids),
        audience_user_id=audience_user_id,
        as_of=as_of,
        held=held_ids,
        enter_rank=config.enter_rank,
        exit_rank=config.exit_rank,
        # The same venue the accrual above reads. Scoring one venue's funding
        # while settling another's is two strategies sharing a portfolio.
        funding_venue=config.funding_venue,
        lookback_days=config.lookback_days,
        min_settlements=config.min_settlements,
    )
    if decision.abstention is not None:
        return _result(
            as_of=as_of,
            held=held_ids,
            cycle=cycle,
            funding=funding,
            abstention=decision.abstention,
            settled=funding_complete,
        )

    closed: list[PairExecution] = []
    opened: list[PairExecution] = []
    halt_reason: str | None = None

    for entity_id in sorted(decision.exited, key=str):
        symbols = by_entity.get(entity_id)
        if symbols is None:
            cycle.refuse(CarryRefusal.NO_SYMBOL)
            continue
        price = await _price_at(
            pool,
            entity_id=entity_id,
            audience=audience_user_id,
            at=as_of,
            venue=venue.name,
        )
        if price is None:
            cycle.refuse(CarryRefusal.NO_REFERENCE_PRICE)
            continue
        pair, halt_reason = await cycle.trade_pair(
            symbols=symbols,
            entity_id=entity_id,
            quantity=legs[symbols.asset].spot,
            reference_price=price,
            as_of=as_of,
            opening=False,
            # Each leg closes at its own held size: a fill-replay book can
            # carry the venue's rounding difference between the legs, and
            # closing both at one quantity would buy back more perpetual than
            # was short, minting a fresh residual instead of leaving flat.
            leg_quantities={
                MarketType.SPOT: legs[symbols.asset].spot,
                MarketType.PERPETUAL: -legs[symbols.asset].perp,
            },
        )
        if pair is not None:
            closed.append(pair)
        if halt_reason is not None:
            break

    if halt_reason is None:
        for entity_id in sorted(decision.entered, key=str):
            symbols = by_entity.get(entity_id)
            if symbols is None:
                cycle.refuse(CarryRefusal.NO_SYMBOL)
                continue
            price = await _price_at(
                pool,
                entity_id=entity_id,
                audience=audience_user_id,
                at=as_of,
                venue=venue.name,
            )
            if price is None:
                cycle.refuse(CarryRefusal.NO_REFERENCE_PRICE)
                continue
            pair, halt_reason = await cycle.trade_pair(
                symbols=symbols,
                entity_id=entity_id,
                # One quantity for both legs, derived once from one price. Sizing
                # each leg off its own price is how a pair opens delta-imbalanced
                # by the basis and still reports two equal notionals.
                quantity=config.notional_per_pair / price,
                reference_price=price,
                as_of=as_of,
                opening=True,
            )
            if pair is not None:
                opened.append(pair)
            if halt_reason is not None:
                break

    final = _legs_by_asset(
        await state.load(pool, portfolio_id), venue_name=venue.name, asset_of=asset_of
    )
    still_unpaired = _unpaired(final)
    if halt_reason is None and still_unpaired:
        halt_reason = (
            f"{CarryHalt.BOOK_NOT_PAIRED.value}: "
            + "; ".join(
                f"{symbol} holds {final[symbol].spot} spot against "
                f"{final[symbol].perp} perp"
                for symbol in still_unpaired
            )
        )
    elif halt_reason is not None:
        halt_reason = f"{CarryHalt.UNWIND_FAILED.value}: {halt_reason}"

    return _result(
        as_of=as_of,
        held=frozenset(
            by_asset[symbol]
            for symbol, held in final.items()
            if held.is_pair and symbol in by_asset
        ),
        cycle=cycle,
        opened=opened,
        closed=closed,
        funding=funding,
        halt_reason=halt_reason,
        # Reached only after the funding loop, so the window is applied whether
        # or not the trading that followed it halted.
        settled=funding_complete,
    )


async def wind_down_book(
    pool,
    *,
    venue: Venue,
    portfolio_id: UUID,
    config: CarryConfig,
    entity_ids: Sequence[UUID],
    audience_user_id: UUID | None,
    as_of: datetime,
    funding_since: datetime,
    ownership: object | None = None,
) -> CarryCycleResult:
    """Close every held pair. The terminal action; no selector, no entries.

    The cycle is a rebalancer -- `run_carry_cycle` always moves the book
    toward the basket the selector names, so it cannot express "hold
    nothing": exiting through it just re-enters the top-ranked names. A wind-
    down is a different statement, and it gets its own entry point with the
    same load-bearing preamble rather than a flag that disables half a cycle:

    - Pair integrity first, exactly as the cycle does. A book that cannot be
      read as pairs is a book whose closes cannot be sized.
    - Funding settles for `(funding_since, as_of]` before any close, because
      the accrual is owed whether or not the book keeps running, and a close
      without settlement silently forfeits it.
    - Each pair closes at its OWN leg quantities, so fill-replay dust goes
      with the close instead of surviving as a fresh residual.
    - Sub-minimum single legs are left in place and named in the result's
      refusals: dust below the venue's own minimum order size cannot be
      traded at all, and zeroing it here would fabricate a price nobody paid.

    Deliberately absent, with reasons: the window and hold guards (a wind-
    down is the named operational override those exist to gate -- running it
    outside the quiet hour once, to stop holding risk, is the judgement the
    `ignore_*` flags formalise for the cycle) and the risk policy (its limits
    bound what the strategy may PUT ON; refusing an exit because the day's
    loss is large is how a drawdown becomes a trap).
    """
    if as_of.tzinfo is None:
        raise ValueError(
            f"as_of is naive ({as_of}); every stamp this path writes is UTC"
        )
    if ownership is None:
        raise ValueError(
            "wind_down_book requires active carry ownership; acquire "
            "carry_cycle_ownership (carry_runner) before any venue call"
        )
    if (
        not getattr(ownership, "active", False)
        or ownership.venue != venue.name
        or ownership.proof is None
    ):
        raise ValueError(
            f"carry ownership is not active for {venue.name}; venue calls require "
            f"the matching database lock"
        )

    cycle = _Cycle(pool, venue=venue, portfolio_id=portfolio_id, config=config)
    by_entity = await _symbols(pool, entity_ids, venue)
    by_asset = {pair.asset: entity_id for entity_id, pair in by_entity.items()}
    asset_of = _pairing_map(by_entity, venue)

    book = await state.load(pool, portfolio_id)
    legs = _legs_by_asset(book, venue_name=venue.name, asset_of=asset_of)
    unpaired = _unpaired(legs)
    held_ids = frozenset(
        by_asset[symbol]
        for symbol, held in legs.items()
        if held.is_pair and symbol in by_asset
    )

    # Dust naming, not dust trading: a single leg below the venue's minimum
    # order size can never be closed there, and the honest result reports it
    # as a refusal with the number rather than quietly leaving a row.
    tradeable_unpaired: list[str] = []
    for symbol in unpaired:
        entity_id = by_asset.get(symbol)
        symbols = by_entity.get(entity_id) if entity_id is not None else None
        price = None
        minimum = None
        if symbols is not None:
            price = await _price_at(
                pool,
                entity_id=entity_id,
                audience=audience_user_id,
                at=as_of,
                venue=venue.name,
            )
            minimum = venue.min_notional_for(symbols.spot)
        held = legs[symbol]
        quantity = held.spot if held.spot != 0 else held.perp
        notional = abs(quantity) * price if price is not None else None
        if (
            notional is not None
            and minimum is not None
            and notional < minimum
        ):
            cycle.refuse(CarryRefusal.DUST_BELOW_VENUE_MINIMUM)
            continue
        tradeable_unpaired.append(symbol)
    if tradeable_unpaired:
        return _result(
            as_of=as_of,
            held=held_ids,
            cycle=cycle,
            halt_reason=(
                f"{CarryHalt.BOOK_NOT_PAIRED.value}: "
                + "; ".join(
                    f"{symbol} holds {legs[symbol].spot} spot against "
                    f"{legs[symbol].perp} perp"
                    for symbol in tradeable_unpaired
                )
            ),
        )

    settlements = await _settlements(
        pool,
        entity_ids=sorted(held_ids, key=str),
        audience=audience_user_id,
        funding_venue=config.funding_venue,
        since=funding_since,
        until=as_of,
    )
    funding: list[FundingAccrual] = []
    funding_complete = True
    for funding_time, entity_id, rate in settlements:
        mark = await _price_at(
            pool,
            entity_id=entity_id,
            audience=audience_user_id,
            at=funding_time,
            venue=venue.name,
        )
        if mark is None:
            cycle.refuse(CarryRefusal.NO_MARK)
            funding_complete = False
            continue
        accrual = await state.apply_funding(
            pool,
            portfolio_id,
            venue=venue.name,
            symbol=by_entity[entity_id].perp,
            funding_time=funding_time,
            funding_rate=rate,
            mark=mark,
        )
        funding.append(accrual)
        credit = getattr(venue, "credit_funding", None)
        if credit is not None and accrual.outcome is FundingOutcome.ACCRUED:
            credit(by_entity[entity_id].perp, accrual.amount)

    closed: list[PairExecution] = []
    halt_reason: str | None = None
    for entity_id in sorted(held_ids, key=str):
        symbols = by_entity.get(entity_id)
        if symbols is None:
            cycle.refuse(CarryRefusal.NO_SYMBOL)
            continue
        price = await _price_at(
            pool,
            entity_id=entity_id,
            audience=audience_user_id,
            at=as_of,
            venue=venue.name,
        )
        if price is None:
            cycle.refuse(CarryRefusal.NO_REFERENCE_PRICE)
            continue
        held = legs[symbols.asset]
        pair, halt_reason = await cycle.trade_pair(
            symbols=symbols,
            entity_id=entity_id,
            quantity=held.spot,
            reference_price=price,
            as_of=as_of,
            opening=False,
            leg_quantities={
                MarketType.SPOT: held.spot,
                MarketType.PERPETUAL: -held.perp,
            },
        )
        if pair is not None:
            closed.append(pair)
        if halt_reason is not None:
            break

    return _result(
        as_of=as_of,
        held=held_ids,
        cycle=cycle,
        closed=closed,
        funding=funding,
        halt_reason=halt_reason,
        settled=funding_complete,
    )
