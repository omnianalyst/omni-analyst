"""The eligibility report -- the artefact that decides whether capital moves.

Everything else in the trading tier is a claim *about* the system: a policy that
says what would be allowed, a cost model that says what a trade would cost, a
gate that says what a phase would permit. This endpoint is the one place the
system states something true *about itself*: here is what each method has
actually resolved, over how many outcomes, how much of that was live rather than
backfilled, whether it held out of sample, and what is left of the edge after
each venue takes its cut. GATE A is read off this page or it is not read at all.

Four rules follow from that.

**Read-only, with no exceptions.** It runs SELECTs and nothing else. It does not
create a portfolio, does not start a loop, does not write a walk-forward
"result" row for later. A report endpoint with a side effect is a footgun on a
page an operator refreshes while deciding whether to commit money, and the
refresh is exactly when they are not watching for one.

**An uncalibrated method appears; it is not omitted.** A method with no resolved
predictions is the most important row on the page, because silence is how a
strategy stops being judged rather than how it passes. It appears with
`status: "uncalibrated"`, a null hit rate, and the gate's own refusal.

**Unknown serialises as null, never as zero.** A hit rate of 0.0 is a method
that resolved wrong every time; a hit rate of null is a method nobody has
measured. Reporting the second as the first understates a working strategy;
reporting the first as the second hides a broken one.

**Money is a string.** `Decimal` through JSON floats is `165.00000000000001`
basis points, and a report whose arithmetic visibly does not close is a report
nobody trusts on the numbers that do.

The barrier geometry the expectancy is computed from is measured from the
ledger's own resolved predictions -- the average distance from entry to target
and from entry to stop, per method and kind -- rather than assumed. A method
with no directional resolved predictions has no geometry, so its expectancy is
null with a stated reason.

**Two expectancies are reported, and they answer different questions.**
`expectancy` is modelled: the calibrated hit rate applied to the average barrier
geometry, which is what the method should earn if the next hundred trades look
like the average of the last hundred. `realised` is measured: every resolved
directional prediction's own P&L, pooled trade by trade, which is what it did
earn -- together with the three properties that can make that number untrue
(`effective_n`, `assumed_share`, `concentration`). The gate reads the second.
They agree when every prediction shares one geometry and diverge exactly when
the average barrier stops describing the individual trade, which is the
condition under which a modelled expectancy quietly stops being true.

`/trading/portfolio` and `/trading/reconciliation` are read paths over the same
tier and inherit the same rules. The first exposes exactly what
`portfolio.state.load` returns and derives nothing -- a quantity it does not
hold is a quantity this endpoint does not report. The second reports the last
stored result per venue, and reports the absence of one as the absence of one;
the reasoning is at the endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from neutron import App, Router
from neutron.error import bad_request, not_found, unauthorized
from starlette.requests import Request

from omni.api.scanner import ASSETS
from omni.auth import resolve_audience_from_request
from omni.conviction.gate import MIN_RESOLVED_FOR_CALIBRATION
from omni.conviction.walk_forward_report import (
    DEFAULT_TARGET_HIT_RATE,
    rolling_windows,
    walk_forward,
    wilson_interval,
)
from omni.portfolio.reconcile import latest_by_venue
from omni.portfolio.state import UnknownPortfolio
from omni.portfolio.state import load as load_portfolio
from omni.trading import carry_runner
from omni.trading.policy import MIN_RESOLVED_FOR_PAPER, TradingPhase, eligible
from omni.venue.costs import BPS, gross_expectancy_bps, round_trip_cost, survives_costs
from omni.venue.protocol import Capabilities, MarketType, OrderKind, Side, TradeIntent

# HALTED is deliberately absent: it refuses every method regardless of record,
# so reporting it per method would add a column of identical refusals and hide
# the three verdicts that vary with the evidence.
_REPORTED_PHASES = (TradingPhase.PAPER, TradingPhase.MICRO, TradingPhase.SCALE)

# How many out-of-sample windows the span of resolved predictions is cut into.
# A method choice, not a measurement: more windows means a smaller test sample
# each and a floor that more of them fail to clear.
DEFAULT_WALK_FORWARD_WINDOWS = 4

# The reference venue the gate's single net figure is priced against. The per
# venue rows still price all four; this is the one the verdict turns on, and it
# is the taker row because a triple-barrier position exits when a barrier is
# touched and a barrier exit is a taker exit by definition. Pricing the gate
# against the maker row would admit an edge that exists only if every exit is
# passive, which no stop is.
GATE_COST_VENUE = "cex_taker"

# The gate's risk parameters. `policy.eligible` refuses to default any of these,
# so a report that runs the gate has to state them -- and it echoes them in the
# payload, because a verdict whose thresholds are invisible cannot be argued
# with.
#
# 5 bps is the part of the cost model that is not modelled at all: every profile
# above passes no spread, and a half-spread on a liquid pair is a few bps a leg.
# A net edge below that sits inside the error bar of its own cost estimate.
GATE_MIN_EXPECTANCY_BPS = Decimal(5)
# GATE B's thirty, unchanged. What changed is the quantity it counts: distinct
# horizon dates rather than raw predictions -- see `policy.MIN_RESOLVED_FOR_PAPER`.
GATE_MIN_EFFECTIVE_N = MIN_RESOLVED_FOR_PAPER
# Past a half, most of the pooled P&L is a number nobody observed.
GATE_MAX_ASSUMED_SHARE = Decimal("0.5")
# Past a half, one name is the strategy. This bar is weakest where diversity is
# lowest -- across two entities an even split is already 0.5 -- so it catches a
# book carried by one of nine, not a book that only ever held two.
GATE_MAX_CONCENTRATION = Decimal("0.5")


@dataclass(frozen=True)
class VenueCostProfile:
    """A venue's cost shape for reporting, not a live connection.

    These are the reference venues in `AUTOTRADE_PLAN.md` section 12, which
    `test_costs.py::TestPlanSection12WorkedExample` already pins. They are
    modelled, not quoted: nothing here reads a live fee schedule or a live
    orderbook, and the payload says so. When `ccxt_venue.py` lands (Phase 4) its
    real `Capabilities` replace these.

    `exit_is_maker` is False on every profile, including the maker one. A
    triple-barrier position exits when a barrier is touched, and a barrier exit
    is a taker exit by definition -- `costs.py` warns that assuming a maker exit
    understates the cost of exactly the trades that lose. This is why the maker
    row here nets less than section 12's, which priced both legs passively.
    """

    name: str
    capabilities: Capabilities
    gas_quote: Decimal
    entry_is_maker: bool
    exit_is_maker: bool = False


def _caps(*, maker: str, taker: str, min_notional: str) -> Capabilities:
    return Capabilities(
        spot=True,
        margin=False,
        perpetuals=False,
        limit_orders=True,
        shorting=False,
        funding_data=False,
        maker_fee_bps=Decimal(maker),
        taker_fee_bps=Decimal(taker),
        min_notional=Decimal(min_notional),
    )


REFERENCE_VENUES: tuple[VenueCostProfile, ...] = (
    VenueCostProfile(
        name="cex_taker",
        capabilities=_caps(maker="2", taker="10", min_notional="10"),
        gas_quote=Decimal(0),
        entry_is_maker=False,
    ),
    VenueCostProfile(
        name="cex_maker",
        capabilities=_caps(maker="2", taker="10", min_notional="10"),
        gas_quote=Decimal(0),
        entry_is_maker=True,
    ),
    VenueCostProfile(
        name="onchain_l1",
        # No protocol fee in the plan's row; the whole cost is gas, which is a
        # fixed 40 in the quote currency per leg and therefore a cost whose bps
        # weight is entirely a function of trade size.
        capabilities=_caps(maker="0", taker="0", min_notional="0"),
        gas_quote=Decimal(40),
        entry_is_maker=False,
    ),
    VenueCostProfile(
        name="swap_service",
        capabilities=_caps(maker="75", taker="75", min_notional="0"),
        gas_quote=Decimal(0),
        entry_is_maker=False,
    ),
)


# Every (method, kind) the audience can see, including methods whose predictions
# are all still pending -- those are the rows that must not vanish.
_METHOD_KINDS = """
SELECT p.method,
       e.kind                                            AS entity_kind,
       count(*)                                          AS total_n,
       count(*) FILTER (WHERE p.outcome <> 'pending')    AS resolved_n,
       min(p.resolved_at)                                AS first_resolved_at,
       max(p.resolved_at)                                AS last_resolved_at
FROM prediction p
JOIN entity e ON e.id = p.entity_id
WHERE (p.audience_user_id IS NULL OR p.audience_user_id = $1)
GROUP BY p.method, e.kind
ORDER BY p.method, e.kind
"""

# Barrier distances as fractions of entry, measured over the same resolved,
# directional population the hit rate is measured over. `neutral` is excluded
# because a neutral prediction has no target and no stop -- `bridge.py` refuses
# to build an intent from one, so pricing an expectancy for it would price a
# trade that is never placed.
_BARRIER_GEOMETRY = """
SELECT p.method,
       e.kind AS entity_kind,
       count(*) AS n,
       avg(CASE WHEN p.direction = 'up'
                THEN (p.upper_barrier - p.entry_price) / p.entry_price
                WHEN p.direction = 'down'
                THEN (p.entry_price - p.lower_barrier) / p.entry_price END)
           AS target_frac,
       avg(CASE WHEN p.direction = 'up'
                THEN (p.entry_price - p.lower_barrier) / p.entry_price
                WHEN p.direction = 'down'
                THEN (p.upper_barrier - p.entry_price) / p.entry_price END)
           AS stop_frac
FROM prediction p
JOIN entity e ON e.id = p.entity_id
WHERE p.direction <> 'neutral'
  AND p.outcome <> 'pending'
  AND (p.audience_user_id IS NULL OR p.audience_user_id = $1)
GROUP BY p.method, e.kind
"""


# Portfolios the caller owns. `portfolio.user_id` is nullable, and an unowned
# row is deliberately not matched: a portfolio names positions, cash and NAV,
# and serving one to an account that does not own it is the same class of leak
# the audience scoping on claims exists to prevent.
_PORTFOLIOS_FOR_AUDIENCE = """
SELECT id FROM portfolio WHERE user_id = $1 ORDER BY created_at, id
"""

# Every venue this report has something to say about. Cash is unioned in rather
# than read off positions alone: cash parked at a venue with no open position is
# still cash whose local figure nobody has checked against the venue's. Stored
# results are unioned in for the harder case -- a venue the book holds nothing at
# but whose last check found the venue holding something we have no row for. That
# is a `position_missing_locally`, it is the direction in which our book is most
# wrong, and listing only venues with local exposure would drop it from the page
# precisely because our book is empty there.
_REPORTED_VENUES = """
SELECT venue FROM position                WHERE portfolio_id = $1
UNION
SELECT venue FROM cash_balance            WHERE portfolio_id = $1
UNION
SELECT venue FROM reconciliation_result   WHERE portfolio_id = $1
ORDER BY venue
"""

# How old a reconciliation may be before it stops counting as current, per venue.
#
# It comes from the operator's own `risk_alert` row -- the same value
# `portfolio.alerts` ages results against when it decides whether to raise
# STALE_DATA -- and from nowhere else. A second source would let this page call a
# venue current while the alerting system calls the same reading stale, and the
# page is what gets read before capital moves. It is deliberately not a query
# parameter: a caller-supplied bound is one a caller can widen until nothing is
# ever stale, and one that would need a default, which would be a permission
# granted by omission.
#
# `min` because two alerts on one venue are two bounds the operator has stated
# and the tighter one is the one they committed to. `active` because a switched
# off alert is not a stated bound at all -- which leaves the venue with no bound,
# and a result no bound applies to is not a result shown to be fresh.
_RECONCILIATION_STALENESS = """
SELECT venue, min(stale_after) AS stale_after
FROM risk_alert
WHERE portfolio_id = $1 AND kind = 'reconciliation' AND active
GROUP BY venue
"""


def _decimal_param(params, name: str, *, required: bool) -> Decimal | None:
    raw = params.get(name)
    if raw is None or raw == "":
        if required:
            raise bad_request(
                f"{name} is required. On-chain gas is a fixed amount per "
                f"transaction, so its cost in basis points is meaningless "
                f"until the trade size is stated"
            )
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise bad_request(f"{name} is not a number: {raw!r}") from exc
    if not value.is_finite():
        raise bad_request(f"{name} must be finite, got {raw!r}")
    return value


def _int_param(params, name: str, *, default: int, low: int, high: int) -> int:
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise bad_request(f"{name} is not an integer: {raw!r}") from exc
    if not low <= value <= high:
        raise bad_request(f"{name} must be between {low} and {high}, got {value}")
    return value


def _round_trip(profile: VenueCostProfile, notional: Decimal):
    """This venue's round-trip cost at this size.

    The intent exists only to carry a notional into the cost model -- `costs.py`
    reads `intent.notional` and nothing else off it -- and is never routed
    anywhere. A quantity of `notional` at a reference price of 1 makes that
    notional exact rather than rounded through a price.
    """
    intent = TradeIntent(
        venue=profile.name,
        symbol="cost-model-reference",
        side=Side.BUY,
        market_type=MarketType.SPOT,
        quantity=notional,
        reference_price=Decimal(1),
        order_kind=OrderKind.MARKET,
    )
    return round_trip_cost(
        intent,
        profile.capabilities,
        gas_quote=profile.gas_quote,
        is_maker=profile.entry_is_maker,
        exit_is_maker=profile.exit_is_maker,
    )


def _gate_round_trip_cost_bps(notional: Decimal) -> Decimal:
    """What the gate's net expectancy is measured against.

    Run through the same cost model as the reported rows rather than stated as
    a constant, so the gate cannot come to disagree with the table underneath
    it about what a round trip costs.
    """
    profile = next(p for p in REFERENCE_VENUES if p.name == GATE_COST_VENUE)
    return _round_trip(profile, notional).total_bps


def _venue_expectancy(
    profile: VenueCostProfile, *, gross_bps: Decimal, notional: Decimal
) -> dict:
    """What is left of `gross_bps` after this venue, or why nothing is."""
    if notional < profile.capabilities.min_notional:
        return {
            "venue": profile.name,
            "net_bps": None,
            "cost_bps": None,
            "survives": None,
            "refusal": (
                f"notional {notional} is below the venue's minimum "
                f"{profile.capabilities.min_notional}"
            ),
        }

    cost = _round_trip(profile, notional)
    viability = survives_costs(gross_bps=gross_bps, cost=cost)
    return {
        "venue": profile.name,
        "net_bps": str(viability.net_bps),
        "cost_bps": str(cost.total_bps),
        "fee_bps": str(cost.fee_bps),
        "gas_bps": str(cost.gas_bps),
        "survives": viability.survives,
        "refusal": None,
    }


def _expectancy(
    *,
    hits: int | None,
    measured_n: int,
    geometry,
    notional: Decimal,
) -> dict:
    """Gross expectancy from the measured hit rate and measured barriers.

    Refuses rather than substitutes. No calibrated rate means no expectancy --
    `gross_expectancy_bps` has no default hit rate for the same reason, and a
    0.5 stood in here would produce a confident number describing nothing.
    """
    if hits is None or measured_n == 0:
        return {
            "gross_bps": None,
            "target_bps": None,
            "stop_bps": None,
            "sample_n": 0,
            "refusal": "no calibrated hit rate; expectancy is unknown, not zero",
            "venues": [],
        }
    if (
        geometry is None
        or geometry["target_frac"] is None
        or geometry["stop_frac"] is None
    ):
        return {
            "gross_bps": None,
            "target_bps": None,
            "stop_bps": None,
            "sample_n": 0,
            "refusal": (
                "no resolved directional predictions, so the distance to target "
                "and stop is unmeasured"
            ),
            "venues": [],
        }

    target_bps = Decimal(geometry["target_frac"]) * BPS
    stop_bps = Decimal(geometry["stop_frac"]) * BPS
    if target_bps <= 0 or stop_bps <= 0:
        return {
            "gross_bps": None,
            "target_bps": str(target_bps),
            "stop_bps": str(stop_bps),
            "sample_n": int(geometry["n"]),
            "refusal": "measured barrier distances are not both positive",
            "venues": [],
        }

    # Exact: the hit rate is hits/measured_n, so the ratio is reconstructed in
    # Decimal rather than converted from the float the policy tier reports.
    gross = gross_expectancy_bps(
        hit_rate=Decimal(hits) / Decimal(measured_n),
        target_bps=target_bps,
        stop_bps=stop_bps,
    )
    return {
        "gross_bps": str(gross),
        "target_bps": str(target_bps),
        "stop_bps": str(stop_bps),
        "sample_n": int(geometry["n"]),
        "refusal": None,
        "venues": [
            _venue_expectancy(profile, gross_bps=gross, notional=notional)
            for profile in REFERENCE_VENUES
        ],
    }


def _realised_payload(
    record, *, round_trip_cost_bps: Decimal, notional: Decimal
) -> dict:
    """What the method actually earned, and the shape of the sample it earned it in.

    Every figure here comes off the `Eligibility` the gate returned, so the
    report cannot quote one number while the verdict was taken on another.

    `n` and `effective_n` are both reported and the gap between them is the
    point: 424 predictions across 44 horizon dates is 44 observations for the
    purpose of believing the mean, and a page that showed only the 424 would
    invite exactly the confidence the first real run did not support.
    """
    common = {
        "n": record.expectancy_n,
        "effective_n": record.effective_n,
        "positive_entities": record.positive_entities,
        "round_trip_cost_bps": str(round_trip_cost_bps),
        "cost_venue": GATE_COST_VENUE,
    }
    if record.gross_expectancy_bps is None:
        return {
            **common,
            "gross_bps": None,
            "net_bps": None,
            "assumed_share": None,
            "concentration": None,
            "refusal": (
                "no resolved directional predictions; the realised edge is "
                "unmeasured, which is not the same as flat"
            ),
            "venues": [],
        }
    return {
        **common,
        "gross_bps": str(record.gross_expectancy_bps),
        "net_bps": str(record.net_expectancy_bps),
        "assumed_share": str(record.assumed_share),
        "concentration": str(record.concentration),
        "refusal": None,
        "venues": [
            _venue_expectancy(
                profile,
                gross_bps=record.gross_expectancy_bps,
                notional=notional,
            )
            for profile in REFERENCE_VENUES
        ],
    }


def _walk_forward_payload(result) -> dict:
    interval = result.wilson_interval
    return {
        "windows": len(result.windows),
        "qualifying_windows": len(result.qualifying_windows),
        "min_per_window": result.min_per_window,
        "pooled_n": result.pooled_n,
        "pooled_hits": result.pooled_hits,
        "total_test_n": result.total_test_n,
        "pooled_hit_rate": result.pooled_hit_rate,
        "interval": list(interval) if interval is not None else None,
        "live_pooled_n": result.pooled_live_n,
        "backfilled_pooled_n": result.pooled_backfilled_n,
        "positive": result.positive,
    }


async def _resolve_portfolio(pool, audience: UUID, params) -> UUID:
    """Which portfolio this caller is asking about, among the ones they own.

    An id the caller does not own is reported missing rather than forbidden:
    the two answers differ only in that the second confirms the portfolio
    exists, which is the fact being withheld.

    More than one owned portfolio with nothing naming which is refused rather
    than resolved by picking. Any rule for picking -- oldest, newest, largest --
    would answer a question about one book while the operator was reading it as
    a question about the other.
    """
    owned = [row["id"] for row in await pool.fetch(_PORTFOLIOS_FOR_AUDIENCE, audience)]

    raw = params.get("portfolio_id")
    if raw:
        try:
            asked = UUID(raw)
        except ValueError as exc:
            raise bad_request(f"portfolio_id is not a uuid: {raw!r}") from exc
        if asked not in owned:
            raise not_found(f"No portfolio {asked}")
        return asked

    if not owned:
        raise not_found("No portfolio for this account")
    if len(owned) > 1:
        raise bad_request(
            f"this account holds {len(owned)} portfolios "
            f"({', '.join(str(p) for p in owned)}); name one with portfolio_id"
        )
    return owned[0]


def portfolio_payload(book) -> dict:
    """One book, in the shape `TRADING_API_CONTRACT.md` freezes.

    Public and module-level because two endpoints in two routers return it:
    this module's `GET /trading/portfolio` and `api/portfolio.py`'s create
    response. It was assembled inline here first, and the create endpoint had to
    reproduce the six scalar keys by hand -- two spellings of one contract, each
    passing its own tests while free to drift from the other.

    `gross_exposure` and `net_exposure` are the `PortfolioState` properties, not
    a second computation of them. Two implementations of one quantity is how
    they come to disagree.
    """
    return {
        "portfolio_id": str(book.portfolio_id),
        "as_of": book.as_of.isoformat(),
        "nav": str(book.nav),
        "cash": str(book.cash),
        "gross_exposure": str(book.gross_exposure),
        "net_exposure": str(book.net_exposure),
        "positions": [_position_payload(p) for p in book.positions],
        "cash_positions": [_cash_payload(c) for c in book.cash_positions],
    }


def _position_payload(position) -> dict:
    """One position, signed.

    `notional` and `is_short` are the `Position` properties, not a second
    derivation of them here: `notional` is cost basis (`|quantity| *
    average_entry`) and nothing in this path marks a position, so no field
    here depends on a price the system does not hold.
    """
    return {
        "venue": position.venue,
        "symbol": position.symbol,
        "market_type": position.market_type.value,
        "quantity": str(position.quantity),
        "average_entry": str(position.average_entry),
        "notional": str(position.notional),
        "is_short": position.is_short,
        "as_of": position.as_of.isoformat(),
    }


def _cash_payload(cash) -> dict:
    return {
        "venue": cash.venue,
        "asset": cash.asset,
        "free": str(cash.free),
        "locked": str(cash.locked),
        "as_of": cash.as_of.isoformat(),
    }


def _discrepancy_payload(discrepancy) -> dict:
    """One disagreement, with an absent side left absent.

    `None` stays `null`. A `position_missing_at_venue` rendered with
    `remote: "0"` asserts the venue reported a flat position; it reported no
    position at all, and the two are the difference between a closed trade and a
    trade the venue has never heard of.
    """
    return {
        "kind": discrepancy.kind.value,
        "venue": discrepancy.venue,
        "symbol": discrepancy.symbol,
        "local": None if discrepancy.local is None else str(discrepancy.local),
        "remote": None if discrepancy.remote is None else str(discrepancy.remote),
        "detail": discrepancy.detail,
    }


def _reconciliation_status(
    result, *, stale_after: timedelta | None, now: datetime, configured: bool = True
) -> str:
    """Which of the five verdicts this venue's stored result supports.

    Ordered so that every path to `reconciled` is a path that had evidence.

    - No stored result is `never_run`. Not `reconciled`: a venue nobody looked at
      is not a venue that agreed, and this substitution has shipped here twice.
    - A divergence outranks everything. An old disagreement is still the last
      thing known about the venue, and downgrading it to `stale` or
      `disconnected` would replace a statement about the books with a statement
      about the clock or the config.
    - A venue with no enabled configuration is `disconnected`, not `stale`:
      nothing will ever refresh its check, and rendering an eternal obligation
      where the operator removed the connection trains the page to be ignored
      (observed live: a deconfigured hyperliquid aging as "Stale" forever).
      The stored result is still shown -- it is history, not a current claim.
    - **An unbounded age is not a fresh one.** With no `stale_after` configured
      for the venue there is nothing this result can be shown to be inside, so
      it reports `stale` rather than `reconciled`. Absence of a threshold cannot
      be permission; a missing bound is how "we never set one" comes to render as
      green.
    - A result stamped in the future is stale too. The clocks disagree, and an
      age computed across disagreeing clocks is not a measurement.
    """
    if result is None:
        return "never_run"
    if not result.reconciled:
        return "diverged"
    if not configured:
        return "disconnected"
    if stale_after is None:
        return "stale"
    age = now - result.checked_at
    if age < timedelta(0) or age > stale_after:
        return "stale"
    return "reconciled"


# Venue keys with an enabled configuration under ANY active user's settings --
# the same source of truth reconcile_once reads. A venue here is connected or
# connectable; a venue in the report but not here is history only.
_ENABLED_VENUE_KEYS = """
SELECT DISTINCT v.key AS venue
FROM users u
JOIN user_settings s ON s.user_id = u.id
CROSS JOIN LATERAL jsonb_each(COALESCE((s.data)::jsonb -> 'venues', '{}'::jsonb))
  AS v(key, value)
WHERE u.active AND COALESCE((v.value ->> 'enabled')::boolean, false)
"""


# Every venue the schedule has something to say about. Cycle rows first, because
# a venue that has run cycles is the one the cadence is measured at; position
# rows unioned in so a book holding something at a venue no cycle has ever run
# at reports `never_run` rather than being absent from the page entirely.
_SCHEDULED_VENUES = """
SELECT venue FROM carry_cycle WHERE portfolio_id = $1
UNION
SELECT venue FROM position     WHERE portfolio_id = $1
ORDER BY venue
"""

_HELD_SYMBOLS = """
SELECT DISTINCT symbol FROM position WHERE portfolio_id = $1 ORDER BY symbol
"""

# Refusals are recorded from migration 057 onward (`carry_refusal`). Before it,
# a refused cycle wrote nothing anywhere but the runner's log, so no absence of
# rows could distinguish a correct refusal from a scheduler that never fired.
# The migration's own `applied_at` is what makes an empty table readable: it
# dates the start of the record, so "nothing recorded" is bounded rather than
# open-ended. Deriving a refusal sentence instead would be the fabrication this
# codebase exists to avoid -- it would read identically in both cases.
REFUSAL_RECORDING_MIGRATION = 57

_REFUSAL_RECORDING_BEGAN = """
SELECT applied_at FROM _neutron_migrations WHERE version = $1
"""

# Newest first, one row per venue via DISTINCT ON. The schedule reports per
# venue because the cadence and the funding boundary are per venue, so a
# refusal pooled across venues would describe a decision no runner took.
_LAST_REFUSAL_PER_VENUE = """
SELECT DISTINCT ON (venue)
       venue, attempted_at, guard, reason,
       funding_window_opens_at, last_cycle_at, last_completed_at, next_due_at
FROM carry_refusal
WHERE portfolio_id = $1
ORDER BY venue, attempted_at DESC
"""


def _refusal_payload(row) -> dict:
    return {
        "venue": row["venue"],
        "attempted_at": row["attempted_at"].isoformat(),
        "guard": row["guard"],
        "reason": row["reason"],
        "funding_window_opens_at": (
            None
            if row["funding_window_opens_at"] is None
            else row["funding_window_opens_at"].isoformat()
        ),
        "last_cycle_at": (
            None if row["last_cycle_at"] is None else row["last_cycle_at"].isoformat()
        ),
        "last_completed_at": (
            None
            if row["last_completed_at"] is None
            else row["last_completed_at"].isoformat()
        ),
        "next_due_at": (
            None if row["next_due_at"] is None else row["next_due_at"].isoformat()
        ),
    }


def _no_refusal_recorded(began_at: datetime | None) -> str:
    """Why `last_refusal` is null, bounded by when the record starts.

    Without the date this sentence would be indistinguishable from "this book
    has never refused a cycle", which is false for every book that ran before
    migration 057 -- and the endpoint would be back to reporting a silence it
    cannot account for.
    """
    if began_at is None:
        return (
            "No refusal has been recorded for this book, and the instant "
            "refusal recording began is not readable here -- migration "
            f"{REFUSAL_RECORDING_MIGRATION} is not in the migration log. Until "
            "that is resolved, an empty record cannot be dated and does not "
            "establish that no cycle was refused."
        )
    return (
        "No refusal has been recorded for this book since "
        f"{began_at.isoformat()}, when migration {REFUSAL_RECORDING_MIGRATION} "
        "began recording them. A cycle refused before that instant wrote no "
        "row anywhere and survives only in the runner's log on the host that "
        "ran it."
    )

# The classes the governed universe can put an asset in. Read off the universe
# rather than listed here, so a class added there appears without this file
# being edited -- and so the UI has one vocabulary to render rather than a
# second copy of this list.
_UNIVERSE = {
    asset["symbol"]: asset for bucket in ASSETS.values() for asset in bucket
}
ASSET_CLASSES = sorted({asset["asset_class"] for asset in _UNIVERSE.values()})


def _base_asset(symbol: str) -> str:
    """`ETH/USDC:USDC` -> `ETH`. A venue symbol names the pair; the universe
    classifies the base asset."""
    return symbol.split("/")[0].split(":")[0].upper()


def _classify(symbol: str) -> dict:
    asset = _base_asset(symbol)
    entry = _UNIVERSE.get(asset)
    if entry is None:
        # Not `stocks`. The frontend set this replaces fell through to equities
        # for anything it did not recognise, so every unlisted perp in the book
        # was filed as a stock -- a class nobody measured, printed as one
        # somebody did.
        return {
            "symbol": symbol,
            "asset": asset,
            "asset_class": None,
            "name": None,
            "refusal": (
                f"no entry in the governed display universe classifies {asset}"
            ),
        }
    return {
        "symbol": symbol,
        "asset": asset,
        "asset_class": entry["asset_class"],
        "name": entry["name"],
        "refusal": None,
    }


def _days_until(due: datetime, now: datetime) -> int:
    """Whole days remaining, rounding up, floored at zero.

    Integer timedelta division throughout: a fraction of a day left is a day
    the hold has not finished, and `now` past `due` is nothing remaining rather
    than a negative countdown.
    """
    remaining = due - now
    if remaining <= timedelta(0):
        return 0
    return -((-remaining) // timedelta(days=1))


def _venue_schedule(
    venue: str,
    known,
    *,
    now: datetime,
    period: timedelta,
    last_refusal: dict | None = None,
) -> dict:
    """One venue's rebalance state, derived from the cycle log and nothing else.

    Four states, each a different fact, none of which may be collapsed into
    another:

    - `never_run` -- no cycle recorded here. The funding boundary is unknown, so
      the first run has to state an inception.
    - `no_completed_cycle` -- cycles exist and every one halted. The hold is
      measured from the last *completed* cycle, so it bars nothing: a book that
      halted and was repaired does not wait six weeks.
    - `holding` -- inside the hold. This is the expected steady state, and the
      one an empty cycle table between rebalances looks exactly like.
    - `due` -- the hold has elapsed. The runner still refuses outside the
      rebalance window, which is reported separately.
    """
    base = {
        "venue": venue,
        "last_refusal": last_refusal,
        "last_cycle_at": (
            None if known.last_cycle is None else known.last_cycle.isoformat()
        ),
        "last_completed_at": (
            None if known.last_completed is None else known.last_completed.isoformat()
        ),
        "funding_window_opens_at": (
            None if known.opens_at is None else known.opens_at.isoformat()
        ),
        "next_rebalance_due_at": None,
        "days_until_due": None,
    }
    if known.last_cycle is None:
        return {
            **base,
            "state": "never_run",
            "detail": (
                f"No carry cycle has been recorded at {venue}, so the instant "
                f"its funding window opens at is not knowable here and the "
                f"first run must state an inception."
            ),
        }
    if known.last_completed is None:
        return {
            **base,
            "state": "no_completed_cycle",
            "detail": (
                f"Every cycle recorded at {venue} halted. The hold is measured "
                f"from the last completed cycle, so it bars nothing here."
            ),
        }

    due = known.last_completed + period
    dated = {
        **base,
        "next_rebalance_due_at": due.isoformat(),
        "days_until_due": _days_until(due, now),
    }
    if now - known.last_completed < period:
        return {
            **dated,
            "state": "holding",
            "detail": (
                f"The last completed cycle ran at "
                f"{known.last_completed.isoformat()}. The hold is {period.days} "
                f"days, so the next rebalance is due {due.isoformat()}. "
                f"Rebalancing sooner is turnover the signal did not ask for."
            ),
        }
    return {
        **dated,
        "state": "due",
        "detail": (
            f"The {period.days}-day hold elapsed at {due.isoformat()}. The next "
            f"cycle runs in the next rebalance window."
        ),
    }


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/trading/eligibility")
    async def eligibility_report(request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        if audience is None:
            # Calibration is audience-scoped, so an anonymous caller has no
            # scope to report on -- and the payload names every method the
            # operator runs and how well each works.
            raise unauthorized("Authentication required")

        params = request.query_params
        notional = _decimal_param(params, "notional", required=True)
        if notional <= 0:
            raise bad_request(f"notional must be positive, got {notional}")

        target = _decimal_param(params, "target_hit_rate", required=False)
        target_hit_rate = (
            DEFAULT_TARGET_HIT_RATE if target is None else float(target)
        )
        if not 0.0 <= target_hit_rate <= 1.0:
            raise bad_request(f"target_hit_rate out of range: {target_hit_rate}")

        n_windows = _int_param(
            params, "windows", default=DEFAULT_WALK_FORWARD_WINDOWS, low=1, high=12
        )
        min_per_window = _int_param(
            params,
            "min_per_window",
            default=MIN_RESOLVED_FOR_CALIBRATION,
            low=1,
            high=10_000,
        )

        gate_cost_bps = _gate_round_trip_cost_bps(notional)

        pool = app.db.pool
        rows = await pool.fetch(_METHOD_KINDS, audience)
        geometry_rows = await pool.fetch(_BARRIER_GEOMETRY, audience)
        geometry = {(r["method"], r["entity_kind"]): r for r in geometry_rows}

        methods = []
        for row in rows:
            method = row["method"]
            entity_kind = row["entity_kind"]

            first, last = row["first_resolved_at"], row["last_resolved_at"]
            forward = None
            # A span shorter than one microsecond per slice cannot be cut into
            # distinct windows, which is a method with too little history rather
            # than an error: the walk-forward is absent, and the gate then
            # refuses with NO_WALK_FORWARD, which is the truth about it.
            sliceable = (
                first is not None
                and last is not None
                and (last - first) >= timedelta(microseconds=n_windows)
            )
            if sliceable:
                forward = await walk_forward(
                    pool,
                    method=method,
                    entity_kind=entity_kind,
                    audience_user_id=audience,
                    # The span is closed at `last`, so the end bound is pushed
                    # one tick past it; a half-open range ending exactly at the
                    # final outcome would drop that outcome.
                    windows=rolling_windows(
                        start=first,
                        end=last + timedelta(microseconds=1),
                        n_windows=n_windows,
                    ),
                    min_per_window=min_per_window,
                    target_hit_rate=target_hit_rate,
                )

            walk_forward_positive = None if forward is None else forward.positive

            verdicts = [
                await eligible(
                    pool,
                    method=method,
                    entity_kind=entity_kind,
                    audience_user_id=audience,
                    phase=phase,
                    target_hit_rate=target_hit_rate,
                    walk_forward_positive=walk_forward_positive,
                    round_trip_cost_bps=gate_cost_bps,
                    min_expectancy_bps=GATE_MIN_EXPECTANCY_BPS,
                    min_effective_n=GATE_MIN_EFFECTIVE_N,
                    max_assumed_share=GATE_MAX_ASSUMED_SHARE,
                    max_concentration=GATE_MAX_CONCENTRATION,
                )
                for phase in _REPORTED_PHASES
            ]
            gates = [
                {
                    "phase": v.phase.value,
                    "eligible": v.eligible,
                    "reason": None if v.reason is None else v.reason.value,
                    "detail": v.detail,
                }
                for v in verdicts
            ]

            # The counts and the hit rate are the same in every phase -- a phase
            # changes which refusal applies, never the record it is applied to.
            record = verdicts[0]
            hit_rate = record.hit_rate
            measured_n = record.measured_n
            # hit_rate is hits/measured_n by construction in `policy.eligible`,
            # so rounding the product recovers the integer numerator the Wilson
            # interval needs without a second aggregation of the ledger.
            hits = None if hit_rate is None else round(hit_rate * measured_n)
            interval = None if hits is None else wilson_interval(hits, measured_n)

            methods.append(
                {
                    "method": method,
                    "entity_kind": entity_kind,
                    "status": "uncalibrated" if hit_rate is None else "calibrated",
                    "total_n": row["total_n"],
                    "resolved_n": record.resolved_n,
                    "measured_n": measured_n,
                    "live_resolved_n": record.live_resolved_n,
                    "hit_rate": hit_rate,
                    "hit_rate_interval": (
                        None if interval is None else list(interval)
                    ),
                    "walk_forward": (
                        None if forward is None else _walk_forward_payload(forward)
                    ),
                    "expectancy": _expectancy(
                        hits=hits,
                        measured_n=measured_n,
                        geometry=geometry.get((method, entity_kind)),
                        notional=notional,
                    ),
                    "realised": _realised_payload(
                        record,
                        round_trip_cost_bps=gate_cost_bps,
                        notional=notional,
                    ),
                    "gates": gates,
                }
            )

        return {
            "as_of": datetime.now(UTC).isoformat(),
            "notional": str(notional),
            "target_hit_rate": target_hit_rate,
            "walk_forward_windows": n_windows,
            "min_per_window": min_per_window,
            "venues_are_modelled": True,
            # The thresholds the verdicts above were taken at. A gate whose
            # parameters are not on the page it is read off cannot be argued
            # with, and the whole reason the previous one survived so long is
            # that the quantity it barred on was never printed next to it.
            "gate_parameters": {
                "round_trip_cost_bps": str(gate_cost_bps),
                "cost_venue": GATE_COST_VENUE,
                "min_expectancy_bps": str(GATE_MIN_EXPECTANCY_BPS),
                "min_effective_n": GATE_MIN_EFFECTIVE_N,
                "max_assumed_share": str(GATE_MAX_ASSUMED_SHARE),
                "max_concentration": str(GATE_MAX_CONCENTRATION),
            },
            "methods": methods,
        }

    @router.get("/trading/portfolio")
    async def portfolio_state(request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")

        pool = app.db.pool
        portfolio_id = await _resolve_portfolio(pool, audience, request.query_params)
        try:
            book = await load_portfolio(pool, portfolio_id)
        except UnknownPortfolio as exc:
            # Reachable only if the row is deleted between the resolve above and
            # this load. Still a 404 and never an empty payload: a portfolio
            # that has nothing in it and a portfolio that is not there are
            # different facts, and one of them means the caller is looking at
            # the wrong account.
            raise not_found(str(exc)) from exc

        return portfolio_payload(book)

    @router.get("/trading/cycles")
    async def carry_cycles(request: Request) -> dict:
        """Every rebalance this book has run, newest first.

        The `carry_cycle` log existed with no way to read it, so "did it trade
        last night, and what happened" was answerable only by opening a log file
        over SSH. A book that runs itself has to be legible without one.

        Halts and abstentions are returned like any other row, not filtered out.
        A halt is the most important thing this endpoint can say -- it means the
        cycle stopped rather than trading on top of something it could not
        account for -- and a reader who only sees successes cannot tell a book
        that is working from one that has been refusing for a month.
        """
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")

        pool = app.db.pool
        portfolio_id = await _resolve_portfolio(pool, audience, request.query_params)
        rows = await pool.fetch(
            "SELECT venue, as_of, funding_since, funding_settled_through, halted, "
            "halt_reason, abstention, funding_collected, fees_paid, "
            "modelled_turnover_cost, pairs_opened, pairs_closed, pairs_held "
            "FROM carry_cycle WHERE portfolio_id = $1 ORDER BY as_of DESC",
            portfolio_id,
        )
        return {
            "portfolio_id": str(portfolio_id),
            "cycles": [
                {
                    "venue": r["venue"],
                    "as_of": r["as_of"].isoformat(),
                    "funding_since": r["funding_since"].isoformat(),
                    "funding_settled_through": (
                        r["funding_settled_through"].isoformat()
                        if r["funding_settled_through"]
                        else None
                    ),
                    "halted": r["halted"],
                    "halt_reason": r["halt_reason"],
                    "abstention": r["abstention"],
                    "funding_collected": str(r["funding_collected"]),
                    "fees_paid": str(r["fees_paid"]),
                    "modelled_turnover_cost": str(r["modelled_turnover_cost"]),
                    "pairs_opened": r["pairs_opened"],
                    "pairs_closed": r["pairs_closed"],
                    "pairs_held": r["pairs_held"],
                }
                for r in rows
            ],
        }

    @router.get("/trading/schedule")
    async def carry_schedule(request: Request) -> dict:
        """When the next rebalance is due, and what the runner is holding for.

        `/trading/cycles` answers what the book *did*; this answers what it is
        *about to do and why not yet*, which is the half that was readable only
        over SSH. The cadence and the rebalance window are read off
        `carry_runner` rather than restated here, because two copies of the hold
        would let this page count down to a date the runner does not recognise.

        The schedule is derived from `carry_cycle` through the same `boundary`
        query the runner takes its decision from. The refusals come from
        `carry_refusal`, which the runner writes before it raises -- so a book
        sitting inside its hold reports the decision that kept it there, and an
        empty record is bounded by the instant recording began rather than
        reading as "nothing happened".
        """
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")

        pool = app.db.pool
        portfolio_id = await _resolve_portfolio(pool, audience, request.query_params)
        rows = await pool.fetch(_SCHEDULED_VENUES, portfolio_id)
        refusal_rows = await pool.fetch(_LAST_REFUSAL_PER_VENUE, portfolio_id)
        refusals = {row["venue"]: _refusal_payload(row) for row in refusal_rows}

        period = carry_runner.REBALANCE_PERIOD
        now = datetime.now(UTC)
        venues = []
        for row in rows:
            venue = row["venue"]
            known = await carry_runner.boundary(pool, portfolio_id, venue)
            venues.append(
                _venue_schedule(
                    venue,
                    known,
                    now=now,
                    period=period,
                    last_refusal=refusals.get(venue),
                )
            )

        # The newest across venues. A venue the book has refused at but never
        # run a cycle at has no schedule row, so this reads the refusals
        # directly rather than the assembled venue list -- otherwise the most
        # recent refusal could be dropped by the join it did not need. Ordered
        # on the timestamp rather than its ISO string: the two agree only while
        # every row carries the same UTC offset, which is a property of the
        # driver rather than anything asserted here.
        newest_row = max(
            refusal_rows, key=lambda row: row["attempted_at"], default=None
        )
        newest = None if newest_row is None else refusals[newest_row["venue"]]
        began_at = await pool.fetchval(
            _REFUSAL_RECORDING_BEGAN, REFUSAL_RECORDING_MIGRATION
        )

        return {
            "portfolio_id": str(portfolio_id),
            "as_of": now.isoformat(),
            "rebalance_period_days": period.days,
            "window_opens_hour": carry_runner.WINDOW_OPENS_HOUR,
            "window_closes_hour": carry_runner.WINDOW_CLOSES_HOUR,
            "in_rebalance_window": carry_runner.in_rebalance_window(now),
            "refusal_recording_began_at": (
                None if began_at is None else began_at.isoformat()
            ),
            "last_refusal": newest,
            "last_refusal_unavailable": (
                None if newest is not None else _no_refusal_recorded(began_at)
            ),
            "venues": venues,
        }

    @router.get("/trading/classification")
    async def position_classification(request: Request) -> dict:
        """What class the governed universe puts each held symbol in.

        The filters over the book were classified in the browser from two
        hardcoded symbol sets, which meant the page and Discover could disagree
        about what an asset is, and anything in neither set was filed as a
        stock. This reports the universe's own answer per held symbol, keyed by
        the symbol as stored so no second parser has to agree with this one, and
        reports an unlisted symbol as unclassified with a reason rather than as
        the class that happened to be the fallthrough.
        """
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")

        pool = app.db.pool
        portfolio_id = await _resolve_portfolio(pool, audience, request.query_params)
        rows = await pool.fetch(_HELD_SYMBOLS, portfolio_id)
        return {
            "portfolio_id": str(portfolio_id),
            "classes": ASSET_CLASSES,
            "symbols": [_classify(row["symbol"]) for row in rows],
        }

    @router.get("/trading/nav-history")
    async def nav_history(request: Request) -> dict:
        """The recorded NAV series for one book, oldest first.

        A deliberate addition to a frozen contract, made rather than drifted:
        `/trading/portfolio` answers "what is the book worth now" and nothing
        answered "what has it been worth", so the UI had a number and no curve.

        Reads `nav_snapshot` and returns exactly what is stored. **It does not
        interpolate, forward-fill or synthesise a point for a day with no
        snapshot.** A gap in this series is a day the recorder did not run or
        could not mark the book, and a chart that draws through it would assert
        a valuation nobody took -- which is the same lie as a partially marked
        NAV, drawn instead of stored.
        """
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")

        pool = app.db.pool
        portfolio_id = await _resolve_portfolio(pool, audience, request.query_params)
        rows = await pool.fetch(
            "SELECT nav, cash, gross_exposure, net_exposure, taken_at "
            "FROM nav_snapshot WHERE portfolio_id = $1 ORDER BY taken_at",
            portfolio_id,
        )
        return {
            "portfolio_id": str(portfolio_id),
            "points": [
                {
                    "taken_at": r["taken_at"].isoformat(),
                    "nav": str(r["nav"]),
                    "cash": str(r["cash"]),
                    "gross_exposure": str(r["gross_exposure"]),
                    "net_exposure": str(r["net_exposure"]),
                }
                for r in rows
            ],
        }

    @router.get("/trading/reconciliation")
    async def reconciliation_report(request: Request) -> dict:
        """The last stored reconciliation per venue, and the silences.

        Three lookups, kept separate because they answer different questions and
        one of them is allowed to come back empty for every venue:

        - which venues this report covers -- real rows, not a configured list;
        - the most recent stored result for each, from
          `portfolio.reconcile.latest_by_venue`, which omits a venue it has
          nothing for rather than inventing a verdict;
        - how old a pass at each venue may be, from the operator's own
          reconciliation alert.

        A venue absent from the second is `never_run`, and that is the entire
        reason the status is an enum: a venue nobody checked is not a venue that
        agreed, and the two are indistinguishable to a page that renders a
        missing row as a clean one.

        A result is matched to a venue by name and to nothing else. A pass at
        `binance` says nothing about `kraken`, so a venue with no row of its own
        stays `never_run` however many other venues reconciled.

        Read-only, like everything else here. It records nothing -- not the read,
        not a "checked" marker, not a stale flag -- because the operator refreshes
        this page while deciding whether to commit capital.
        """
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")

        pool = app.db.pool
        portfolio_id = await _resolve_portfolio(pool, audience, request.query_params)
        rows = await pool.fetch(_REPORTED_VENUES, portfolio_id)
        latest = await latest_by_venue(pool, portfolio_id)
        staleness = {
            row["venue"]: row["stale_after"]
            for row in await pool.fetch(_RECONCILIATION_STALENESS, portfolio_id)
        }
        configured_venues = {
            row["venue"] for row in await pool.fetch(_ENABLED_VENUE_KEYS)
        }

        # One reading of the clock for the whole page. Ageing each venue against
        # its own `now` would let two venues checked at the same instant land on
        # different sides of the same bound.
        now = datetime.now(UTC)

        venues = []
        for row in rows:
            venue = row["venue"]
            result = latest.get(venue)
            venues.append(
                {
                    "venue": venue,
                    "status": _reconciliation_status(
                        result,
                        stale_after=staleness.get(venue),
                        now=now,
                        configured=venue in configured_venues,
                    ),
                    "checked_at": (
                        None if result is None else result.checked_at.isoformat()
                    ),
                    "discrepancies": (
                        []
                        if result is None
                        else [_discrepancy_payload(d) for d in result.discrepancies]
                    ),
                }
            )

        return {"as_of": now.isoformat(), "venues": venues}

    return router
