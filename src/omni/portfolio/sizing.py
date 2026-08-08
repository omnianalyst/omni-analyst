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

The return value is a *quantity*, because that is what `TradeIntent` takes.
Everything before the final line is a fraction of NAV.
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
) -> Decimal:
    """Quantity to trade: Kelly, fractioned, vol-scaled, clamped, divided by price.

    The order matters. Vol targeting applies to the fractioned Kelly rather
    than replacing it, and the NAV clamp is applied last so it is a hard
    ceiling on the position and not one more term that a vol scalar can lift
    the size back above.
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
    fraction = kelly_fraction(hit_rate=hit_rate, payoff=payoff) * cap

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
