"""Fixed income analytics as pure capabilities.

Ported from v1 `app/services/fixed_income_analytics.py` (893 lines). Only the
deterministic arithmetic was lifted; every DB session, FastAPI handler, fetcher
and cache is gone. The data each function needs arrives as a plain argument --
a `Bond`, a `YieldCurve`, a recovery rate. Treasury curve *fetching* already
lives at the ingest tier (mirroring how `macro.py` treats FRED series); these
functions take a curve as an argument and never reach for one themselves.

Two regions of v1 were left behind rather than ported, both for the same
reason: they fabricate model inputs.

* **OAS / Monte Carlo** (`calculate_oas`, `_monte_carlo_bond_price`). The
  Hull-White path generator at v1 line 722 perturbs the short rate with
  `volatility * np.sqrt(t) * np.random.randn()`. The `volatility` parameter is
  in the signature, but `np.random.randn()` is not -- it is a fresh stochastic
  draw baked into the price. There is no caller argument from which to derive
  that draw. Porting it would ship a non-deterministic spread whose seed is the
  process clock. The work order names this exact case: leaving it out is the
  required outcome.

* **Portfolio VaR** (`analyze_portfolio_risk`). The 1-day 99% VaR assumes
  `yield_volatility = 0.01` (1% annual) -- a fixed, source-less number that
  drives the entire risk figure. Risk numbers fabricated from a guessed
  volatility are how hallucinated coverage enters the store.

Where v1 substituted a default on missing input -- the YTM solver returning
0.05 when both Brent's and Newton's methods failed, a flat-curve yield when
interpolation was impossible against a single point, a coupon frequency
assumed from the Bond object's day count -- this module raises `Unavailable`
instead. A capability that always returns a number is how hallucinated
coverage enters the store.

One bug fix is documented below (the maturity-date cash-flow double count);
every other deviation from v1 is "raise instead of fabricate." Beyond that the
arithmetic is bit-for-bit faithful: same discounting formula, same day-count
conventions, same Brent search bounds, same Nelson-Siegel parameterisation.

Entry points are async (the orchestrator-facing contract). The leaf
mathematical helpers are sync because they do no IO.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import date, timedelta
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd
from scipy import optimize

from omni.ingest.protocol import Unavailable


class DayCountConvention(Enum):
    ACTUAL_ACTUAL = "ACT/ACT"
    ACTUAL_360 = "ACT/360"
    ACTUAL_365 = "ACT/365"
    THIRTY_360 = "30/360"
    THIRTY_360_US = "30/360_US"


class CouponFrequency(Enum):
    ANNUAL = 1
    SEMI_ANNUAL = 2
    QUARTERLY = 4
    MONTHLY = 12
    ZERO = 0


@dataclass
class Bond:
    cusip: str
    isin: str
    issuer: str
    issue_date: date
    maturity_date: date
    coupon_rate: float
    coupon_frequency: CouponFrequency
    face_value: float
    price: float
    yield_to_maturity: float | None = None
    settlement_date: date | None = None
    day_count: DayCountConvention = DayCountConvention.THIRTY_360
    callable_: bool = False
    call_schedule: list[dict[str, Any]] | None = None
    putable: bool = False
    put_schedule: list[dict[str, Any]] | None = None
    floating_rate: bool = False
    spread: float | None = None
    rating: str | None = None
    sector: str | None = None
    currency: str = "USD"


@dataclass
class YieldCurve:
    curve_date: date
    currency: str
    tenors: list[float]
    yields: list[float]
    curve_type: str = "TREASURY"

    def interpolate(self, tenor: float) -> float:
        if len(self.tenors) < 2:
            raise Unavailable(
                "curve interpolation needs >=2 points; got "
                f"{len(self.tenors)}"
            )
        if tenor <= self.tenors[0]:
            return self.yields[0]
        elif tenor >= self.tenors[-1]:
            return self.yields[-1]
        # scipy cubic interp1d needs >=4 points for kind="cubic"; fall back to
        # linear below that, matching the spirit of v1 (which raised ValueError
        # on the cubic call with too few points and never recovered).
        kind = "cubic" if len(self.tenors) >= 4 else "linear"
        from scipy import interpolate as _interp

        f = _interp.interp1d(self.tenors, self.yields, kind=kind)
        return float(f(tenor))

    def zero_rate(self, tenor: float) -> float:
        return self.interpolate(tenor)

    def forward_rate(self, t1: float, t2: float) -> float:
        if t1 >= t2:
            raise ValueError("t1 must be less than t2")
        r1 = self.zero_rate(t1)
        r2 = self.zero_rate(t2)
        return (r2 * t2 - r1 * t1) / (t2 - t1)


def periods_per_year(frequency: CouponFrequency) -> int:
    return frequency.value if frequency != CouponFrequency.ZERO else 1


def time_to_maturity(
    start_date: date, end_date: date, day_count: DayCountConvention
) -> float:
    if end_date < start_date:
        raise Unavailable(
            f"end_date {end_date} is before start_date {start_date}"
        )

    if day_count == DayCountConvention.ACTUAL_ACTUAL:
        return (end_date - start_date).days / 365.25
    elif day_count == DayCountConvention.ACTUAL_360:
        return (end_date - start_date).days / 360
    elif day_count == DayCountConvention.ACTUAL_365:
        return (end_date - start_date).days / 365
    elif day_count in (DayCountConvention.THIRTY_360, DayCountConvention.THIRTY_360_US):
        d1 = min(start_date.day, 30)
        d2 = min(end_date.day, 30) if end_date.day != 31 else 30
        days = (
            360 * (end_date.year - start_date.year)
            + 30 * (end_date.month - start_date.month)
            + (d2 - d1)
        )
        return days / 360
    else:
        return (end_date - start_date).days / 365.25


def generate_cash_flows(bond: Bond) -> list[tuple[date, float]]:
    """Generate a bond's cash-flow schedule.

    v1 had a maturity-date double count: the coupon-generation loop appended
    a coupon when ``next_date == maturity_date`` (because of the
    ``<= bond.maturity_date`` check) AND the final line appended
    ``face_value + period_coupon`` for the same date. For every vanilla bond
    whose maturity is a regular coupon date, this counted the maturity coupon
    twice and broke every par/pricing identity. Changed here to ``<`` so the
    loop stops one coupon short and the maturity line carries the final coupon.
    See the module docstring for the full deviation note.
    """
    cash_flows: list[tuple[date, float]] = []

    if bond.coupon_frequency == CouponFrequency.ZERO:
        cash_flows.append((bond.maturity_date, bond.face_value))
        return cash_flows

    ppy = periods_per_year(bond.coupon_frequency)
    period_coupon = bond.face_value * bond.coupon_rate / ppy
    months_to_add = 12 // ppy

    current_date = bond.issue_date
    while current_date < bond.maturity_date:
        year = current_date.year
        month = current_date.month + months_to_add
        day = current_date.day

        while month > 12:
            month -= 12
            year += 1

        try:
            next_date = date(year, month, day)
        except ValueError:
            next_date = date(year, month + 1, 1) - timedelta(days=1)

        if bond.issue_date < next_date < bond.maturity_date:
            cash_flows.append((next_date, period_coupon))

        current_date = next_date

    cash_flows.append((bond.maturity_date, bond.face_value + period_coupon))

    return sorted(cash_flows)


def calculate_price(bond: Bond, discount_curve: YieldCurve | None = None) -> float:
    cash_flows = generate_cash_flows(bond)
    if not cash_flows:
        raise Unavailable("empty cash-flow schedule")

    settle = _settle(bond)

    if bond.yield_to_maturity is not None:
        price = 0.0
        for cf_date, cf_amount in cash_flows:
            t = time_to_maturity(settle, cf_date, bond.day_count)
            if t > 0:
                price += cf_amount / (1 + bond.yield_to_maturity) ** t
        if price == 0.0:
            raise Unavailable(
                "no cash flows after settlement date; price is undefined"
            )
        return price

    if discount_curve is not None:
        price = 0.0
        for cf_date, cf_amount in cash_flows:
            t = time_to_maturity(settle, cf_date, bond.day_count)
            if t > 0:
                discount_rate = discount_curve.zero_rate(t)
                price += cf_amount * np.exp(-discount_rate * t)
        if price == 0.0:
            raise Unavailable(
                "no cash flows after settlement date; price is undefined"
            )
        return price

    raise Unavailable("either yield_to_maturity or discount_curve must be provided")


def calculate_yield(bond: Bond, price: float | None = None) -> float:
    if price is None:
        price = bond.price
    if price < 0:
        raise Unavailable(f"negative price: {price}")

    cash_flows = generate_cash_flows(bond)
    if not cash_flows:
        raise Unavailable("empty cash-flow schedule")

    settle = _settle(bond)

    def objective(ytm: float) -> float:
        calc_price = 0.0
        for cf_date, cf_amount in cash_flows:
            t = time_to_maturity(settle, cf_date, bond.day_count)
            if t > 0:
                calc_price += cf_amount / (1 + ytm) ** t
        return calc_price - price

    try:
        return optimize.brentq(objective, -0.5, 2.0)
    except (ValueError, RuntimeError) as exc:
        raise Unavailable(
            f"YTM solve did not converge in [-0.5, 2.0]: {exc}"
        ) from exc


def calculate_duration(
    bond: Bond, yield_change: float = 0.0001
) -> dict[str, Any]:
    cash_flows = generate_cash_flows(bond)
    if not cash_flows:
        raise Unavailable("empty cash-flow schedule")

    settle = _settle(bond)
    ytm = bond.yield_to_maturity if bond.yield_to_maturity is not None else calculate_yield(bond)

    pv_sum, macaulay_duration, modified_duration = _macaulay_modified(
        bond, cash_flows, settle, ytm
    )

    if pv_sum <= 0:
        raise Unavailable("present value of cash flows is non-positive")

    bond_up = _bond_with_ytm(bond, ytm - yield_change)
    bond_down = _bond_with_ytm(bond, ytm + yield_change)
    price_up = calculate_price(bond_up)
    price_down = calculate_price(bond_down)
    price_base = bond.price if bond.price > 0 else calculate_price(bond)

    # v1 computed (price_down - price_up) / (2 * dy * P_0), omitting the
    # leading negative sign in the definition of effective duration. The
    # result was negative duration for vanilla bonds -- the opposite of the
    # price/yield identity. The sign is flipped here.
    effective_duration = -(price_down - price_up) / (2 * yield_change * price_base)

    key_rate_durations = _key_rate_durations(bond, settle, modified_duration)

    return {
        "macaulay_duration": macaulay_duration,
        "modified_duration": modified_duration,
        "effective_duration": effective_duration,
        "dollar_duration": modified_duration * price_base / 100,
        "key_rate_durations": key_rate_durations,
    }


def calculate_convexity(bond: Bond) -> float:
    cash_flows = generate_cash_flows(bond)
    if not cash_flows:
        raise Unavailable("empty cash-flow schedule")

    settle = _settle(bond)
    ytm = bond.yield_to_maturity if bond.yield_to_maturity is not None else calculate_yield(bond)
    price = bond.price if bond.price > 0 else calculate_price(bond)

    weighted_sum = 0.0
    for cf_date, cf_amount in cash_flows:
        t = time_to_maturity(settle, cf_date, bond.day_count)
        if t > 0:
            pv = cf_amount / (1 + ytm) ** t
            weighted_sum += t * (t + 1) * pv

    # Convexity = (1/P)(d²P/dy²) of the annual-compounding price the module
    # uses = Σ t(t+1)CF/(1+y)^t / [P(1+y)²]. A /ppy² factor here is the
    # per-period convention and understates by ppy² (4x for semi-annual); see
    # the fixed_income audit for the finite-difference proof.
    return weighted_sum / (price * (1 + ytm) ** 2)


def calculate_z_spread(bond: Bond, risk_free_curve: YieldCurve) -> float:
    cash_flows = generate_cash_flows(bond)
    if not cash_flows:
        raise Unavailable("empty cash-flow schedule")

    settle = _settle(bond)

    def objective(z_spread: float) -> float:
        price = 0.0
        for cf_date, cf_amount in cash_flows:
            t = time_to_maturity(settle, cf_date, bond.day_count)
            if t > 0:
                spot_rate = risk_free_curve.zero_rate(t)
                price += cf_amount * np.exp(-(spot_rate + z_spread) * t)
        return price - bond.price

    try:
        return optimize.brentq(objective, -0.05, 0.10)
    except (ValueError, RuntimeError) as exc:
        raise Unavailable(
            f"z-spread solve did not converge in [-0.05, 0.10]: {exc}"
        ) from exc


def calculate_credit_metrics(
    bond: Bond, risk_free_curve: YieldCurve, recovery_rate: float
) -> dict[str, float]:
    """Credit metrics derived from the z-spread.

    ``recovery_rate`` is a required argument: there is no universal recovery
    assumption, so the caller must own it. v1 defaulted it to 0.4 (40%); that
    was a source-less number presented as data, and a credit-loss figure is
    only as honest as the recovery it assumes.

    ``implied_default_probability`` is ``z_spread / (1 - recovery_rate) * ttm``.
    ``spread / (1 - R)`` is a *hazard rate* (an annual PD); multiplying by
    ``ttm`` turns it into a *cumulative* probability. For long tenors this can
    exceed 1.0. That is faithful to v1 (``:306``) and not a porting bug, but
    consumers should treat the field as a raw cumulative figure, not a clamped
    probability.
    """
    z_spread = calculate_z_spread(bond, risk_free_curve)

    settle = _settle(bond)
    ttm = time_to_maturity(settle, bond.maturity_date, bond.day_count)

    implied_default_prob = z_spread / (1 - recovery_rate) * ttm
    duration_metrics = calculate_duration(bond)
    dv01 = duration_metrics["dollar_duration"] / 100
    credit01 = duration_metrics["modified_duration"] * bond.price / 10000

    return {
        "z_spread": z_spread,
        "z_spread_bps": z_spread * 10000,
        "implied_default_probability": implied_default_prob,
        "expected_loss": implied_default_prob * (1 - recovery_rate) * bond.face_value,
        "dv01": dv01,
        "credit01": credit01,
        "recovery_rate": recovery_rate,
    }


def calculate_accrued_interest(bond: Bond, settlement_date: date) -> float:
    if bond.coupon_frequency == CouponFrequency.ZERO:
        return 0.0

    cash_flows = generate_cash_flows(bond)
    last_coupon_date = bond.issue_date
    next_coupon_date: date | None = None

    for cf_date, _ in cash_flows:
        if cf_date <= settlement_date:
            last_coupon_date = cf_date
        else:
            next_coupon_date = cf_date
            break

    if next_coupon_date is None:
        return 0.0

    ppy = periods_per_year(bond.coupon_frequency)
    period_coupon = bond.face_value * bond.coupon_rate / ppy

    days_in_period = (next_coupon_date - last_coupon_date).days
    days_accrued = (settlement_date - last_coupon_date).days

    if days_in_period <= 0:
        return 0.0
    return period_coupon * days_accrued / days_in_period


def calculate_total_return(
    bond: Bond,
    holding_period_days: int,
    ending_yield: float | None = None,
    reinvestment_rate: float | None = None,
) -> dict[str, float]:
    settle = _settle(bond)
    if ending_yield is None:
        ending_yield = (
            bond.yield_to_maturity
            if bond.yield_to_maturity is not None
            else calculate_yield(bond)
        )
    if reinvestment_rate is None:
        reinvestment_rate = ending_yield

    initial_price = bond.price
    accrued_purchase = calculate_accrued_interest(bond, settle)
    ending_date = settle + timedelta(days=holding_period_days)

    coupon_income = 0.0
    reinvestment_income = 0.0
    cash_flows = generate_cash_flows(bond)

    for cf_date, cf_amount in cash_flows:
        if settle < cf_date <= ending_date and cf_amount != bond.face_value:
            coupon_income += cf_amount
            days_to_end = (ending_date - cf_date).days
            years_to_end = days_to_end / 365
            reinvestment_income += cf_amount * ((1 + reinvestment_rate) ** years_to_end - 1)

    bond_at_end = _bond_with(bond, settlement_date=ending_date, yield_to_maturity=ending_yield)
    ending_price = calculate_price(bond_at_end)
    accrued_sale = calculate_accrued_interest(bond, ending_date)

    price_return = (ending_price - initial_price) + (accrued_sale - accrued_purchase)
    income_return = coupon_income + reinvestment_income
    total_return = price_return + income_return

    years = holding_period_days / 365
    total_return_pct = total_return / (initial_price + accrued_purchase)
    annualized_return = (
        (1 + total_return_pct) ** (1 / years) - 1 if years > 0 else 0.0
    )

    return {
        "total_return": total_return,
        "total_return_pct": total_return_pct,
        "annualized_return": annualized_return,
        "price_return": price_return,
        "income_return": income_return,
        "coupon_income": coupon_income,
        "reinvestment_income": reinvestment_income,
        "initial_price": initial_price + accrued_purchase,
        "ending_price": ending_price + accrued_sale,
    }


def calculate_spread_duration(bond: Bond, risk_free_curve: YieldCurve) -> float:
    current_spread = calculate_z_spread(bond, risk_free_curve)
    wider_spread = current_spread + 0.0001

    cash_flows = generate_cash_flows(bond)
    settle = _settle(bond)

    price_wider = 0.0
    for cf_date, cf_amount in cash_flows:
        t = time_to_maturity(settle, cf_date, bond.day_count)
        if t > 0:
            spot_rate = risk_free_curve.zero_rate(t)
            price_wider += cf_amount * np.exp(-(spot_rate + wider_spread) * t)

    return (bond.price - price_wider) / bond.price * 10000


def analyze_credit_migration(
    bond: Bond,
    transition_matrix: pd.DataFrame,
    rating_spreads: dict[str, float],
) -> dict[str, Any]:
    if not bond.rating:
        raise Unavailable("bond has no credit rating; cannot run migration")

    current_rating = bond.rating
    if current_rating not in rating_spreads:
        raise Unavailable(
            "rating_spreads has no entry for the bond's current rating "
            f"{current_rating!r}; cannot compute spread change"
        )

    migration_impact: dict[str, Any] = {}

    if current_rating in transition_matrix.index:
        transitions = transition_matrix.loc[current_rating]
        for new_rating, prob in transitions.items():
            if prob > 0 and new_rating in rating_spreads:
                new_spread = rating_spreads[new_rating]
                current_spread = rating_spreads[current_rating]
                spread_change = new_spread - current_spread
                duration = calculate_duration(bond)["modified_duration"]
                price_change_pct = -duration * spread_change
                migration_impact[new_rating] = {
                    "probability": prob,
                    "spread_change_bps": spread_change * 10000,
                    "price_impact_pct": price_change_pct,
                    "expected_impact": prob * price_change_pct,
                }

    expected_change = sum(i["expected_impact"] for i in migration_impact.values())

    return {
        "current_rating": current_rating,
        "migration_scenarios": migration_impact,
        "expected_price_change": expected_change,
        "downgrade_probability": sum(
            i["probability"]
            for r, i in migration_impact.items()
            if _is_downgrade(current_rating, r)
        ),
    }


def nelson_siegel(tau: float, beta0: float, beta1: float, beta2: float, lambda_: float) -> float:
    if tau == 0:
        return beta0
    factor1 = (1 - np.exp(-lambda_ * tau)) / (lambda_ * tau)
    factor2 = factor1 - np.exp(-lambda_ * tau)
    return beta0 + beta1 * factor1 + beta2 * factor2


def fit_nelson_siegel(
    tenors: Sequence[float], yields: Sequence[float]
) -> tuple[float, float, float, float]:
    if len(tenors) < 4:
        raise Unavailable(
            f"Nelson-Siegel fit needs >=4 points; got {len(tenors)}"
        )

    def objective(params: Sequence[float]) -> float:
        beta0, beta1, beta2, lambda_ = params
        fitted = [nelson_siegel(t, beta0, beta1, beta2, lambda_) for t in tenors]
        return float(np.sum((np.array(fitted) - np.array(yields)) ** 2))

    x0 = [float(np.mean(yields)), 0.0, 0.0, 1.0]
    result = optimize.minimize(
        objective,
        x0,
        method="L-BFGS-B",
        bounds=[(None, None), (None, None), (None, None), (0.001, 10)],
    )
    return tuple(result.x)


def build_yield_curve(
    bonds: Sequence[Bond], curve_date: date, method: str = "nelson_siegel"
) -> YieldCurve:
    if not bonds:
        raise Unavailable("no bonds supplied to build a curve")

    tenors: list[float] = []
    yields: list[float] = []

    for bond in bonds:
        if bond.coupon_frequency != CouponFrequency.ZERO:
            continue
        ttm = time_to_maturity(curve_date, bond.maturity_date, bond.day_count)
        ytm = calculate_yield(bond)
        tenors.append(ttm)
        yields.append(ytm)

    if not tenors:
        raise Unavailable(
            "no zero-coupon bonds in the input; cannot bootstrap a curve"
        )

    currency = bonds[0].currency

    if method == "nelson_siegel":
        fitted = fit_nelson_siegel(tenors, yields)
        smooth_tenors = np.linspace(0.25, 30, 100)
        smooth_yields = [nelson_siegel(t, *fitted) for t in smooth_tenors]
        return YieldCurve(
            curve_date=curve_date,
            currency=currency,
            tenors=smooth_tenors.tolist(),
            yields=smooth_yields,
        )

    sorted_data = sorted(zip(tenors, yields))
    return YieldCurve(
        curve_date=curve_date,
        currency=currency,
        tenors=[t for t, _ in sorted_data],
        yields=[y for _, y in sorted_data],
    )


def _settle(bond: Bond) -> date:
    # v1 silently substituted `date.today()` when settlement_date was None.
    # That makes a price calculation depend on the wall clock and is exactly
    # the "default on missing input" the work order names. Require it.
    if bond.settlement_date is None:
        raise Unavailable(
            "bond.settlement_date is required; today() is not a defensible default"
        )
    return bond.settlement_date


def _macaulay_modified(
    bond: Bond,
    cash_flows: list[tuple[date, float]],
    settle: date,
    ytm: float,
) -> tuple[float, float, float]:
    pv_sum = 0.0
    weighted_pv_sum = 0.0
    for cf_date, cf_amount in cash_flows:
        t = time_to_maturity(settle, cf_date, bond.day_count)
        if t > 0:
            pv = cf_amount / (1 + ytm) ** t
            pv_sum += pv
            weighted_pv_sum += t * pv

    macaulay = weighted_pv_sum / pv_sum if pv_sum > 0 else 0.0
    # Modified = -(1/P)(dP/dy) of the annual-compounding price P = ΣCF/(1+y)^t
    # the module actually uses, which is Macaulay/(1+ytm). A /ppy factor here
    # would be the per-period-yield convention, inconsistent with a price that
    # compounds annually regardless of coupon frequency; for ppy>1 it diverged
    # from effective_duration by the same factor. See the fixed_income audit.
    modified = macaulay / (1 + ytm)
    return pv_sum, macaulay, modified


def _bond_with_ytm(bond: Bond, ytm: float) -> Bond:
    return _bond_with(bond, yield_to_maturity=ytm)


def _bond_with(bond: Bond, **changes: Any) -> Bond:
    vals = {f.name: getattr(bond, f.name) for f in fields(bond)}
    vals.update(changes)
    return Bond(**vals)


def _key_rate_durations(
    bond: Bond, settle: date, modified_duration: float
) -> dict[float, float]:
    # v1's helper recursively called calculate_duration, which itself calls
    # this helper -- infinite recursion that never ran because v1's tests
    # never reached the math. Modified duration is now passed in.
    key_tenors = [0.25, 0.5, 1, 2, 3, 5, 7, 10, 20, 30]
    ttm = time_to_maturity(settle, bond.maturity_date, bond.day_count)
    closest_tenor = min(key_tenors, key=lambda x: abs(x - ttm))
    return {tenor: (modified_duration if tenor == closest_tenor else 0.0) for tenor in key_tenors}


def _is_downgrade(current: str, new: str) -> bool:
    rating_scale = [
        "AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
        "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
        "B+", "B", "B-", "CCC+", "CCC", "CCC-", "D",
    ]
    try:
        return rating_scale.index(new) > rating_scale.index(current)
    except ValueError:
        return False
