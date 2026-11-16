"""How much, given a calibrated hit rate and the prediction's own barriers.

Kelly is the only sizing rule that ties size to edge rather than to the
language a finding is written in, and it needs exactly two numbers: a
probability and a payoff ratio. Both are things the system already records --
`p` from the calibration pass over resolved predictions, `b` from the barriers
fixed at write time -- which is why neither has a default here. An uncalibrated
method has no `p`, and substituting one produces a confident size derived from
nothing: the size still comes out, still looks like a number, and is
attributable to no evidence at all. This module raises instead. A method that
cannot be sized is a method that does not trade yet.

Full Kelly is optimal only if `p` is exact. It is not -- it is an estimate from
a finite sample, and Kelly's penalty for an overstated `p` is severe and
asymmetric. So the result is multiplied by a fraction, quarter Kelly by
default, and that fraction is an argument rather than a constant folded into
the formula, because a "Kelly size" that is secretly a quarter of Kelly is
exactly the kind of hidden multiplier this codebase's predecessor was full of.

**The Kelly fraction is NAV at RISK, not NAV deployed.** The binary Kelly
formula `f* = (p*b - q) / b` is the fraction of bankroll LOST when the bet
loses. A position of notional N with a stop `d` away from entry (as a fraction
of entry) loses `N * d` at the stop, so the notional that puts `f` of NAV at
risk is `f * NAV / d`. An earlier version used `f * NAV` as the notional
itself, which made the stop distance drop out: a 1% stop and a 20% stop with
the same reward/risk ratio got the same position and twenty times different
dollar risk, and the vol scalar was then applied to something that was not a
risk number. Dividing by the stop distance can ask for more than NAV when the
stop is tight; `max_position_pct_nav` is applied last and is what bounds it.

**The hit rate is an estimate, and the size can say so.** With `resolved_n`
given, the size is taken from the Wilson 95% lower bound of the hit rate rather
than its point value: 0.60 from 30 resolved predictions has a lower bound near
0.42, which at even money is no edge at all. A lower bound at or below
breakeven sizes to zero, the same refusal a negative point edge gets.

The return value is a *quantity*, because that is what `TradeIntent` takes.
"""

from __future__ import annotations

from decimal import Decimal

DEFAULT_KELLY_FRACTION = Decimal("0.25")

# One tolerance for the module. Price distances are compared against it scaled
# by `entry`, so "too close to entry to be a barrier" means the same thing for
# a sub-cent token and a five-figure index; volatility is a standard deviation
# of returns and is already scale-free, so it is compared directly. A
# tolerance is used rather than an equality test because a barrier arrived at
# by arithmetic lands near a bound, not on it.
_TOL = Decimal("1e-12")


def _finite(value: object, name: str) -> Decimal:
    """Coerce to Decimal and refuse NaN and inf explicitly.

    Every comparison against a NaN is either False or, for Decimal, an
    InvalidOperation -- so a range check written as a comparison does not
    reject a NaN, it lets one through or blows up somewhere unrelated. The
    check has to be its own thing.
    """
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a number, got bool {value!r}")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(repr(value))
    else:
        raise TypeError(
            f"{name} must be Decimal, int or float, got {type(value).__name__}"
        )
    if number.is_nan() or number.is_infinite():
        raise ValueError(f"{name} is not finite: {value!r}")
    return number


def _money(value: object, name: str) -> Decimal:
    if isinstance(value, float):
        raise TypeError(
            f"{name} must be Decimal, not float ({value!r}); binary floats "
            f"accumulate error across a position's lifetime and it lands in the P&L"
        )
    return _finite(value, name)


def payoff_ratio(*, entry: Decimal, stop: Decimal, target: Decimal) -> Decimal:
    """Reward over risk implied by the barriers, direction read off them.

    Direction is inferred rather than passed in, so a stop and a target that
    disagree about which way the trade goes cannot be quietly accepted -- there
    is no direction argument left for them to contradict. A stop on the wrong
    side of entry is a take-profit wearing a stop's name; it never stops the
    loss it was written to stop, and it would size the position off a risk leg
    that cannot be lost.
    """
    entry_price = _money(entry, "entry")
    stop_price = _money(stop, "stop")
    target_price = _money(target, "target")

    for name, price in (
        ("entry", entry_price),
        ("stop", stop_price),
        ("target", target_price),
    ):
        if price <= 0:
            raise ValueError(f"{name} must be a positive price, got {price}")

    tolerance = entry_price * _TOL

    if target_price - entry_price > tolerance:
        risk = entry_price - stop_price
        if risk <= tolerance:
            raise ValueError(
                f"a long targeting {target_price} needs a stop below entry "
                f"{entry_price}, got {stop_price}"
            )
        return (target_price - entry_price) / risk

    if entry_price - target_price > tolerance:
        risk = stop_price - entry_price
        if risk <= tolerance:
            raise ValueError(
                f"a short targeting {target_price} needs a stop above entry "
                f"{entry_price}, got {stop_price}"
            )
        return (entry_price - target_price) / risk

    raise ValueError(
        f"target {target_price} is indistinguishable from entry {entry_price}; "
        f"there is no direction to size"
    )


def kelly_fraction(*, hit_rate: Decimal | None, payoff: Decimal) -> Decimal:
    """f* = (p*b - q) / b, floored at zero.

    A negative f* means the other side of the trade holds the edge. Returning
    it as a size would have the caller open a position the analysis never
    claimed, so the refusal is expressed as zero rather than as a sign flip.
    """
    if hit_rate is None:
        raise ValueError(
            "hit_rate is required and has no default: an uncalibrated method "
            "has no probability, and a substituted one sizes a position from nothing"
        )
    probability = _finite(hit_rate, "hit_rate")
    ratio = _finite(payoff, "payoff")

    if not Decimal(0) <= probability <= Decimal(1):
        raise ValueError(f"hit_rate is a probability and is out of range: {probability}")
    if ratio <= 0:
        raise ValueError(f"payoff must be positive, got {ratio}")

    edge = probability * ratio - (Decimal(1) - probability)
    if edge <= 0:
        return Decimal(0)
    return edge / ratio


# The two-sided 95% normal quantile, for the Wilson interval.
_Z95 = Decimal("1.959963984540054")


def wilson_lower_bound(*, hit_rate: Decimal, resolved_n: int) -> Decimal:
    """The Wilson 95% lower confidence bound on a hit rate from `resolved_n`.

    The standard haircut for sizing off an estimated probability: Kelly's
    penalty for an overstated `p` is severe and asymmetric, and a point
    estimate from a small sample overstates it half the time.
    """
    probability = _finite(hit_rate, "hit_rate")
    if not Decimal(0) <= probability <= Decimal(1):
        raise ValueError(f"hit_rate is a probability and is out of range: {probability}")
    if isinstance(resolved_n, bool) or not isinstance(resolved_n, int):
        raise TypeError(
            f"resolved_n must be an int count, got {type(resolved_n).__name__}"
        )
    if resolved_n <= 0:
        raise ValueError(
            f"resolved_n must be a positive count, got {resolved_n}; a hit rate "
            f"over no resolved predictions is not a hit rate"
        )
    n = Decimal(resolved_n)
    z2 = _Z95 * _Z95
    centre = probability + z2 / (2 * n)
    spread = _Z95 * (probability * (1 - probability) / n + z2 / (4 * n * n)).sqrt()
    return max(Decimal(0), (centre - spread) / (1 + z2 / n))


def stop_distance(*, entry: Decimal, stop: Decimal) -> Decimal:
    """The loss at the stop as a fraction of entry: what one unit of notional risks."""
    entry_price = _money(entry, "entry")
    stop_price = _money(stop, "stop")
    if entry_price <= 0:
        raise ValueError(f"entry must be a positive price, got {entry_price}")
    distance = abs(entry_price - stop_price) / entry_price
    if distance <= _TOL:
        raise ValueError(
            f"stop {stop_price} is indistinguishable from entry {entry_price}; "
            f"there is no risk to size against"
        )
    return distance


def size(
    *,
    nav: Decimal,
    hit_rate: Decimal | None,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    kelly_cap: Decimal = DEFAULT_KELLY_FRACTION,
    max_position_pct_nav: Decimal,
    volatility: Decimal | None = None,
    vol_target: Decimal | None = None,
    resolved_n: int | None = None,
) -> Decimal:
    """Quantity to trade: Kelly at risk, fractioned, over the stop distance,
    vol-scaled, clamped, divided by price.

    The order matters. The fractioned Kelly is NAV at risk; dividing by the
    stop distance turns it into the notional that risks exactly that. Vol
    targeting applies to that notional rather than replacing it, and the NAV
    clamp is applied last so it is a hard ceiling on the position and not one
    more term that a tight stop or a vol scalar can lift the size back above.

    `resolved_n`, when given, is the number of resolved predictions the hit
    rate was measured over, and the size is taken from the Wilson 95% lower
    bound of the hit rate instead of its point value. Omitted, the point
    value is used -- the caller is asserting the rate needs no haircut.
    """
    if hit_rate is None:
        raise ValueError(
            "hit_rate is required and has no default: an uncalibrated method "
            "has no probability, and a substituted one sizes a position from nothing"
        )

    equity = _money(nav, "nav")
    if equity <= 0:
        raise ValueError(f"nav must be positive to size against, got {equity}")

    cap = _finite(kelly_cap, "kelly_cap")
    if not Decimal(0) < cap <= Decimal(1):
        raise ValueError(
            f"kelly_cap is a fraction of full Kelly in (0, 1], got {cap}"
        )

    limit = _finite(max_position_pct_nav, "max_position_pct_nav")
    if not Decimal(0) < limit <= Decimal(1):
        raise ValueError(
            f"max_position_pct_nav is a fraction of NAV in (0, 1], got {limit}; "
            f"leverage is a venue and risk decision, not a sizing default"
        )

    if (volatility is None) != (vol_target is None):
        raise ValueError(
            "vol targeting needs both volatility and vol_target, or neither; "
            "one alone has no ratio to scale by"
        )

    entry_price = _money(entry, "entry")
    payoff = payoff_ratio(entry=entry_price, stop=stop, target=target)
    probability = (
        hit_rate
        if resolved_n is None
        else wilson_lower_bound(hit_rate=hit_rate, resolved_n=resolved_n)
    )
    at_risk = kelly_fraction(hit_rate=probability, payoff=payoff) * cap
    # NAV at risk -> NAV deployed: a unit of notional loses the stop distance.
    fraction = at_risk / stop_distance(entry=entry_price, stop=stop)

    if volatility is not None:
        realised = _finite(volatility, "volatility")
        wanted = _finite(vol_target, "vol_target")
        if realised <= _TOL:
            raise ValueError(
                f"volatility must be positive to scale against, got {realised}"
            )
        if wanted <= _TOL:
            raise ValueError(f"vol_target must be positive, got {wanted}")
        fraction *= wanted / realised

    fraction = min(fraction, limit)

    return fraction * equity / entry_price
