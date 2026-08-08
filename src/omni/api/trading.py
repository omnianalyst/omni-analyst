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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from neutron import App, Router
from neutron.error import bad_request, unauthorized
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.conviction.gate import MIN_RESOLVED_FOR_CALIBRATION
from omni.conviction.walk_forward_report import (
    DEFAULT_TARGET_HIT_RATE,
    rolling_windows,
    walk_forward,
    wilson_interval,
)
from omni.trading.policy import TradingPhase, eligible
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


def _venue_expectancy(
    profile: VenueCostProfile, *, gross_bps: Decimal, notional: Decimal
) -> dict:
    """What is left of `gross_bps` after this venue, or why nothing is.

    The intent exists only to carry a notional into the cost model -- `costs.py`
    reads `intent.notional` and nothing else off it -- and is never routed
    anywhere. A quantity of `notional` at a reference price of 1 makes that
    notional exact rather than rounded through a price.
    """
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

    intent = TradeIntent(
        venue=profile.name,
        symbol="cost-model-reference",
        side=Side.BUY,
        market_type=MarketType.SPOT,
        quantity=notional,
        reference_price=Decimal(1),
        order_kind=OrderKind.MARKET,
    )
    cost = round_trip_cost(
        intent,
        profile.capabilities,
        gas_quote=profile.gas_quote,
        is_maker=profile.entry_is_maker,
        exit_is_maker=profile.exit_is_maker,
    )
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
            "methods": methods,
        }

    return router
