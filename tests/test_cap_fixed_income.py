"""Behaviour tests for the fixed-income capabilities.

Every assertion is on a hand-computed value for a known input, not on shape.
Every failure path is exercised -- past maturity, no future cash flows, a
single-point curve that cannot be interpolated, a negative price, a YTM solve
with no root in the search bracket -- and each raises `Unavailable` rather than
returning a plausible number. v1 silently substituted defaults on every one of
these paths (notably a 0.05 YTM when both solvers failed); a capability that
always returns a number is how hallucinated coverage enters the store.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from omni.capabilities.fixed_income import (
    Bond,
    CouponFrequency,
    DayCountConvention,
    YieldCurve,
    analyze_credit_migration,
    build_yield_curve,
    calculate_accrued_interest,
    calculate_convexity,
    calculate_credit_metrics,
    calculate_duration,
    calculate_price,
    calculate_spread_duration,
    calculate_total_return,
    calculate_yield,
    calculate_z_spread,
    fit_nelson_siegel,
    generate_cash_flows,
    nelson_siegel,
    periods_per_year,
    time_to_maturity,
)
from omni.ingest.protocol import Unavailable

# ---------------------------------------------------------------------------
# Fixtures: a vanilla 5y annual par bond and a 5y zero, both settling on the
# issue date so day-count arithmetic is exact.
# ---------------------------------------------------------------------------

ISSUE = date(2020, 1, 15)
MATURITY = date(2025, 1, 15)


def _par_coupon_bond(
    *,
    coupon_rate: float = 0.05,
    ytm: float | None = 0.05,
    price: float = 100.0,
    settle: date = ISSUE,
) -> Bond:
    # 30/360 makes each annual coupon period exactly 1.0 years, so the
    # par / YTM-equals-coupon identity holds bit-for-bit rather than only
    # within day-count noise.
    return Bond(
        cusip="000AAA00",
        isin="US000AAA0000",
        issuer="Test",
        issue_date=ISSUE,
        maturity_date=MATURITY,
        coupon_rate=coupon_rate,
        coupon_frequency=CouponFrequency.ANNUAL,
        face_value=100.0,
        price=price,
        yield_to_maturity=ytm,
        settlement_date=settle,
        day_count=DayCountConvention.THIRTY_360,
    )


def _zero_bond(
    *,
    ytm: float = 0.05,
    price: float = 100.0,
    maturity: date = MATURITY,
    settle: date = ISSUE,
) -> Bond:
    return Bond(
        cusip="000ZERO0",
        isin="US000ZERO00",
        issuer="Test",
        issue_date=ISSUE,
        maturity_date=maturity,
        coupon_rate=0.0,
        coupon_frequency=CouponFrequency.ZERO,
        face_value=100.0,
        price=price,
        yield_to_maturity=ytm,
        settlement_date=settle,
        day_count=DayCountConvention.THIRTY_360,
    )


# ---------------------------------------------------------------------------
# Cash-flow generation
# ---------------------------------------------------------------------------


class TestGenerateCashFlows:
    def test_vanilla_annual_coupon_schedule(self):
        bond = _par_coupon_bond()
        cfs = generate_cash_flows(bond)
        # Five annual coupons + principal at the final coupon date. v1
        # double-counted the maturity coupon (loop appended it via a `<=`
        # check AND the final line added face + period_coupon); the fix
        # changes the loop check to `<` so the maturity line carries it.
        assert cfs == [
            (date(2021, 1, 15), 5.0),
            (date(2022, 1, 15), 5.0),
            (date(2023, 1, 15), 5.0),
            (date(2024, 1, 15), 5.0),
            (date(2025, 1, 15), 105.0),
        ]

    def test_zero_coupon_has_only_principal_at_maturity(self):
        cfs = generate_cash_flows(_zero_bond())
        assert cfs == [(MATURITY, 100.0)]

    def test_semi_annual_coupons_split_period_coupon(self):
        bond = Bond(
            cusip="S", isin="S", issuer="T",
            issue_date=ISSUE, maturity_date=MATURITY,
            coupon_rate=0.06, coupon_frequency=CouponFrequency.SEMI_ANNUAL,
            face_value=100.0, price=100.0, yield_to_maturity=0.06,
            settlement_date=ISSUE,
            day_count=DayCountConvention.ACTUAL_ACTUAL,
        )
        cfs = generate_cash_flows(bond)
        # Semi-annual: 10 coupons of 3.0, last date carries principal + 3.0.
        coupon_amounts = [amt for _, amt in cfs]
        assert coupon_amounts[:-1] == [3.0] * 9
        assert coupon_amounts[-1] == pytest.approx(103.0)
        assert cfs[-1][0] == MATURITY


# ---------------------------------------------------------------------------
# Day count
# ---------------------------------------------------------------------------


class TestTimeToMaturity:
    def test_actual_actual_uses_365_25(self):
        t = time_to_maturity(date(2020, 1, 1), date(2021, 1, 1), DayCountConvention.ACTUAL_ACTUAL)
        assert t == pytest.approx(366 / 365.25)

    def test_actual_360(self):
        t = time_to_maturity(date(2020, 1, 1), date(2020, 7, 1), DayCountConvention.ACTUAL_360)
        assert t == pytest.approx(182 / 360)

    def test_thirty_360_handles_day_31(self):
        # Jan 31 -> Feb 28: under 30/360, d1=min(31,30)=30, d2=28 (not 31) -> 30.
        # Days = 30*(2-1) + (28-30) = 28; t = 28/360.
        t = time_to_maturity(date(2020, 1, 31), date(2020, 2, 28), DayCountConvention.THIRTY_360)
        assert t == pytest.approx(28 / 360)

    def test_past_maturity_raises(self):
        with pytest.raises(Unavailable, match="before start_date"):
            time_to_maturity(
                date(2025, 1, 15), date(2020, 1, 15), DayCountConvention.ACTUAL_ACTUAL
            )

    def test_periods_per_year_zero_returns_one(self):
        assert periods_per_year(CouponFrequency.ZERO) == 1
        assert periods_per_year(CouponFrequency.SEMI_ANNUAL) == 2
        assert periods_per_year(CouponFrequency.MONTHLY) == 12


# ---------------------------------------------------------------------------
# Pricing identities
# ---------------------------------------------------------------------------


class TestCalculatePrice:
    def test_par_bond_priced_at_100(self):
        # 5% coupon discounted at 5% YTM = par.
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        assert calculate_price(bond) == pytest.approx(100.0, abs=1e-9)

    def test_single_cash_flow_discounted_at_own_yield_is_exact(self):
        # Zero: price = face / (1+ytm)^t. calculate_price must produce that
        # bit-for-bit, no extra discount factors or skipped flows.
        ytm = 0.05
        t = time_to_maturity(ISSUE, MATURITY, DayCountConvention.THIRTY_360)
        expected = 100.0 / (1 + ytm) ** t
        bond = _zero_bond(ytm=ytm, price=expected)
        assert calculate_price(bond) == pytest.approx(expected, abs=1e-12)

    def test_price_and_yield_move_in_opposite_directions(self):
        at_par = calculate_price(_par_coupon_bond(ytm=0.05))
        above_par = calculate_price(_par_coupon_bond(ytm=0.04))
        below_par = calculate_price(_par_coupon_bond(ytm=0.06))
        assert above_par > at_par > below_par

    def test_price_via_discount_curve_uses_continuous_compounding(self):
        # Discount curve path: price = sum(cf * exp(-r * t)).
        curve = YieldCurve(
            curve_date=ISSUE, currency="USD",
            tenors=[1.0, 2.0, 3.0, 4.0, 5.0],
            yields=[0.05, 0.05, 0.05, 0.05, 0.05],
        )
        # For our 5y annual par bond at flat 5% continuous, the price is below
        # 100 because continuous compounding discounts harder than annual.
        price = calculate_price(_par_coupon_bond(ytm=None), discount_curve=curve)
        # Hand-checked: sum of 5 * exp(-0.05*t) for t in {1..4} + 105*exp(-0.05*5).
        ts = [time_to_maturity(ISSUE, date(2021 + i, 1, 15), DayCountConvention.THIRTY_360) for i in range(5)]
        expected = sum(5.0 * np.exp(-0.05 * t) for t in ts[:4]) + 105.0 * np.exp(-0.05 * ts[4])
        assert price == pytest.approx(expected, abs=1e-9)
        assert price < 100.0

    def test_neither_yield_nor_curve_raises(self):
        bond = _par_coupon_bond(ytm=None)
        with pytest.raises(Unavailable, match="either yield_to_maturity or discount_curve"):
            calculate_price(bond)

    def test_no_future_cash_flows_raises(self):
        # Settle on the single cash-flow date of a zero -> t==0 for the only
        # flow, nothing to discount -> raise. (Using a coupon bond here would
        # trigger the past-maturity check on an earlier coupon first.)
        bond = _zero_bond(ytm=0.05, settle=MATURITY)
        with pytest.raises(Unavailable, match="no cash flows after settlement"):
            calculate_price(bond)

    def test_maturity_in_the_past_raises(self):
        bond = Bond(
            cusip="P", isin="P", issuer="T",
            issue_date=date(2010, 1, 1), maturity_date=date(2011, 1, 1),
            coupon_rate=0.05, coupon_frequency=CouponFrequency.ANNUAL,
            face_value=100.0, price=100.0, yield_to_maturity=0.05,
            settlement_date=date(2025, 1, 1),
            day_count=DayCountConvention.ACTUAL_ACTUAL,
        )
        with pytest.raises(Unavailable, match="before start_date"):
            calculate_price(bond)

    def test_missing_settlement_date_raises(self):
        # v1 silently substituted date.today(); that makes the price depend on
        # the wall clock and is exactly the "default on missing input" the
        # work order names. settlement_date is required.
        bond = _par_coupon_bond(ytm=0.05)
        bond.settlement_date = None
        with pytest.raises(Unavailable, match="settlement_date is required"):
            calculate_price(bond)


# ---------------------------------------------------------------------------
# Yield-to-maturity
# ---------------------------------------------------------------------------


class TestCalculateYield:
    def test_par_bond_ytm_equals_coupon(self):
        # Price a 5% bond at par; the implied YTM must be 5%.
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        price = calculate_price(bond)
        ytm = calculate_yield(_par_coupon_bond(ytm=None), price=price)
        assert ytm == pytest.approx(0.05, abs=1e-9)

    def test_round_trip_price_ytm_price(self):
        # price -> ytm -> price must reproduce the original price.
        original_ytm = 0.07
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=original_ytm)
        price_from_ytm = calculate_price(bond)
        recovered_ytm = calculate_yield(_par_coupon_bond(ytm=None), price=price_from_ytm)
        assert recovered_ytm == pytest.approx(original_ytm, abs=1e-9)
        bond_recovered = _par_coupon_bond(coupon_rate=0.05, ytm=recovered_ytm)
        price_again = calculate_price(bond_recovered)
        assert price_again == pytest.approx(price_from_ytm, abs=1e-9)

    def test_zero_bond_ytm_recovered(self):
        # For a zero, ytm = (face/price)^(1/t) - 1; check the solver finds it.
        t = time_to_maturity(ISSUE, MATURITY, DayCountConvention.THIRTY_360)
        ytm = 0.043
        price = 100.0 / (1 + ytm) ** t
        bond = _zero_bond(ytm=None, price=price)
        assert calculate_yield(bond, price=price) == pytest.approx(ytm, abs=1e-9)

    def test_negative_price_raises(self):
        bond = _par_coupon_bond(ytm=None)
        with pytest.raises(Unavailable, match="negative price"):
            calculate_yield(bond, price=-10.0)

    def test_non_convergent_ytm_raises(self):
        # For a 5y zero, the achievable price in [-0.5, 2.0] YTM brackets is
        # bounded above by 100/(0.5)^5 = 3200. A price of 100000 lies outside
        # the bracket -> brentq fails to bracket a root -> raise.
        bond = _zero_bond(ytm=None, price=100000.0)
        with pytest.raises(Unavailable, match="did not converge"):
            calculate_yield(bond, price=100000.0)

    def test_empty_schedule_raises(self):
        # Force an empty schedule: generate_cash_flows always emits at least
        # the maturity flow, so this guards the contract by checking the
        # no-future-flows path instead.
        bond = _par_coupon_bond(settle=date(2030, 1, 1), ytm=None)
        with pytest.raises(Unavailable):
            calculate_yield(bond, price=100.0)


# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------


class TestCalculateDuration:
    def test_macaulay_of_zero_equals_its_maturity(self):
        bond = _zero_bond(ytm=0.05)
        d = calculate_duration(bond)
        t = time_to_maturity(ISSUE, MATURITY, DayCountConvention.THIRTY_360)
        assert d["macaulay_duration"] == pytest.approx(t, abs=1e-9)

    def test_modified_below_macaulay_for_coupon_bond(self):
        # Modified = -(1/P)(dP/dy) of the annual-compounding price the module
        # uses, so Macaulay/(1+ytm); always strictly less for ytm>0.
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        d = calculate_duration(bond)
        assert d["modified_duration"] < d["macaulay_duration"]
        expected = d["macaulay_duration"] / (1 + 0.05)
        assert d["modified_duration"] == pytest.approx(expected, abs=1e-12)

    def test_modified_matches_effective_for_a_semi_annual_bond(self):
        # For an option-free bond modified ≈ effective (both are -(1/P)dP/dy of
        # the same price function). The /(1+y/ppy) bug made them disagree for
        # ppy>1 (ratio ~1.022); with /(1+y) they agree. This is the case the
        # ppy=1 fixtures cannot reach.
        from dataclasses import fields as dataclass_fields

        base = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        vals = {f.name: getattr(base, f.name) for f in dataclass_fields(base)}
        vals["coupon_frequency"] = CouponFrequency.SEMI_ANNUAL
        # Price base must be the actual annual-compounding price so the duration
        # math and the effective-duration finite difference share one P.
        vals["price"] = calculate_price(Bond(**{**vals, "yield_to_maturity": 0.05}))
        d = calculate_duration(Bond(**vals), yield_change=0.0001)
        assert d["modified_duration"] == pytest.approx(
            d["effective_duration"], rel=0.01
        )

    def test_effective_duration_signs(self):
        # Effective duration = (P_down - P_up) / (2 * dy * P_base). For a
        # vanilla bond it must be positive and close to modified duration.
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        d = calculate_duration(bond, yield_change=0.001)
        assert d["effective_duration"] > 0
        assert d["effective_duration"] == pytest.approx(d["modified_duration"], rel=0.01)

    def test_key_rate_durations_assign_to_nearest_tenor(self):
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        d = calculate_duration(bond)
        krd = d["key_rate_durations"]
        # Maturity is 5y; the closest key tenor is 5.
        nonzero = {t: v for t, v in krd.items() if v != 0.0}
        assert set(nonzero.keys()) == {5}
        assert nonzero[5] == pytest.approx(d["modified_duration"], abs=1e-9)

    def test_empty_schedule_raises(self):
        bond = _par_coupon_bond(settle=date(2030, 1, 1), ytm=None)
        with pytest.raises(Unavailable):
            calculate_duration(bond)


# ---------------------------------------------------------------------------
# Convexity
# ---------------------------------------------------------------------------


class TestCalculateConvexity:
    def test_convexity_positive_for_option_free_bond(self):
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        assert calculate_convexity(bond) > 0

    def test_longer_bond_has_higher_convexity(self):
        # Convexity scales roughly with maturity for a zero; the 10y zero
        # must be more convex than the 5y zero at the same yield.
        zero_5y = _zero_bond(ytm=0.05, maturity=date(2025, 1, 15))
        zero_10y = _zero_bond(ytm=0.05, maturity=date(2030, 1, 15))
        assert calculate_convexity(zero_10y) > calculate_convexity(zero_5y)

    def test_convexity_matches_hand_derived_closed_form(self):
        # Magnitude test for the 5y annual par bond, 30/360 (times exactly
        # 1..5), ytm 5%, price 100, ppy 1. The value below is hand-derived
        # from the closed form the audit names --
        #   sum(t*(t+1)*cf/(1+y)^t) / (price*(1+y)^2)
        # -- NOT obtained by calling calculate_convexity (ppy=1 so the former
        # /ppy^2 factor was a no-op here; it is exercised by the semi-annual
        # test below). An independent finite-difference check on the price
        # function (P(+dy) + P(-dy) - 2P) / (P * dy^2) gives 23.935988508583247
        # at dy=1e-4, agreeing to ~1e-6 (the expected truncation error), which
        # is why the constant is trustworthy. A wrong implementation that
        # returns time-to-maturity (5.0) fails this by ~19; see the R4 report
        # for the stub-and-restore proof.
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        assert calculate_convexity(bond) == pytest.approx(
            23.935987497907238, abs=1e-9
        )

    def test_semi_annual_convexity_matches_the_finite_difference_definition(self):
        # The closed form must equal the convexity DEFINITION
        # (P(+dy)+P(-dy)-2P)/(P*dy^2) for a semi-annual bond too. The /ppy^2
        # factor (now removed) understated this by ~4x (5.91 vs 23.59); for
        # ppy=1 it was a no-op, which is why the hand-derived annual test above
        # never caught it. The finite difference IS convexity -- a closed form
        # that disagrees with it is wrong by definition.
        from dataclasses import fields as dataclass_fields

        base = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        vals = {f.name: getattr(base, f.name) for f in dataclass_fields(base)}
        vals["coupon_frequency"] = CouponFrequency.SEMI_ANNUAL
        vals["price"] = calculate_price(Bond(**{**vals, "yield_to_maturity": 0.05}))
        bond = Bond(**vals)
        closed = calculate_convexity(bond)

        def with_ytm(y):
            v = dict(vals)
            v["yield_to_maturity"] = y
            return Bond(**v)

        dy = 1e-5
        p = calculate_price(with_ytm(0.05))
        p_up = calculate_price(with_ytm(0.05 + dy))
        p_dn = calculate_price(with_ytm(0.05 - dy))
        finite_diff = (p_up + p_dn - 2 * p) / (p * dy**2)

        assert closed == pytest.approx(finite_diff, rel=1e-3)
        # Discriminates the old /ppy^2 bug, which returned ~5.9 here.
        assert closed > 15.0


# ---------------------------------------------------------------------------
# Z-spread
# ---------------------------------------------------------------------------


class TestCalculateZSpread:
    def test_zero_spread_when_priced_off_the_curve(self):
        # Price the bond off a flat 5% continuous curve; z-spread must be ~0.
        curve = YieldCurve(
            curve_date=ISSUE, currency="USD",
            tenors=[1.0, 2.0, 3.0, 4.0, 5.0],
            yields=[0.05, 0.05, 0.05, 0.05, 0.05],
        )
        bond = _par_coupon_bond(ytm=None)
        # Reprice the bond off the flat curve and use that as the price input.
        curve_price = sum(
            5.0 * np.exp(-0.05 * time_to_maturity(ISSUE, date(2021 + i, 1, 15), DayCountConvention.THIRTY_360))
            for i in range(4)
        )
        curve_price += 105.0 * np.exp(-0.05 * time_to_maturity(ISSUE, MATURITY, DayCountConvention.THIRTY_360))
        bond.price = curve_price
        zs = calculate_z_spread(bond, curve)
        assert zs == pytest.approx(0.0, abs=1e-9)

    def test_positive_spread_when_priced_below_curve(self):
        curve = YieldCurve(
            curve_date=ISSUE, currency="USD",
            tenors=[1.0, 2.0, 3.0, 4.0, 5.0],
            yields=[0.03, 0.03, 0.03, 0.03, 0.03],
        )
        # Price the bond at par with a 5% coupon; the curve is 3% so the
        # bond trades rich and the z-spread is positive.
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=None, price=100.0)
        zs = calculate_z_spread(bond, curve)
        assert zs > 0

    def test_single_point_curve_propagates_unavailable(self):
        curve = YieldCurve(
            curve_date=ISSUE, currency="USD",
            tenors=[5.0], yields=[0.05],
        )
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=None, price=100.0)
        with pytest.raises(Unavailable, match="curve interpolation"):
            calculate_z_spread(bond, curve)


# ---------------------------------------------------------------------------
# Credit metrics
# ---------------------------------------------------------------------------


class TestCalculateCreditMetrics:
    def test_known_components(self):
        curve = YieldCurve(
            curve_date=ISSUE, currency="USD",
            tenors=[1.0, 2.0, 3.0, 4.0, 5.0],
            yields=[0.03, 0.03, 0.03, 0.03, 0.03],
        )
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=None, price=100.0)
        m = calculate_credit_metrics(bond, curve, recovery_rate=0.4)
        zs = calculate_z_spread(bond, curve)
        assert m["z_spread"] == pytest.approx(zs)
        assert m["z_spread_bps"] == pytest.approx(zs * 10000)
        ttm = time_to_maturity(ISSUE, MATURITY, DayCountConvention.THIRTY_360)
        expected_pd = zs / (1 - 0.4) * ttm
        assert m["implied_default_probability"] == pytest.approx(expected_pd)
        assert m["expected_loss"] == pytest.approx(expected_pd * 0.6 * 100.0)
        assert m["recovery_rate"] == 0.4


# ---------------------------------------------------------------------------
# Spread duration
# ---------------------------------------------------------------------------


class TestCalculateSpreadDuration:
    def test_spread_duration_positive_for_rich_bond(self):
        curve = YieldCurve(
            curve_date=ISSUE, currency="USD",
            tenors=[1.0, 2.0, 3.0, 4.0, 5.0],
            yields=[0.03, 0.03, 0.03, 0.03, 0.03],
        )
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=None, price=100.0)
        sd = calculate_spread_duration(bond, curve)
        # Wider spread -> lower price -> (price - price_wider) > 0.
        assert sd > 0


# ---------------------------------------------------------------------------
# Yield curve interpolation, forward rates, Nelson-Siegel
# ---------------------------------------------------------------------------


class TestYieldCurveInterpolation:
    def test_interpolate_inside_range(self):
        curve = YieldCurve(
            curve_date=ISSUE, currency="USD",
            tenors=[1.0, 2.0, 3.0, 4.0, 5.0],
            yields=[0.03, 0.035, 0.04, 0.042, 0.045],
        )
        # Endpoints are returned exactly.
        assert curve.interpolate(1.0) == pytest.approx(0.03)
        assert curve.interpolate(5.0) == pytest.approx(0.045)
        # Interior monotone between endpoints.
        mid = curve.interpolate(3.5)
        assert 0.04 < mid < 0.042

    def test_extrapolation_is_flat_outside_range(self):
        curve = YieldCurve(
            curve_date=ISSUE, currency="USD",
            tenors=[1.0, 2.0, 3.0, 4.0, 5.0],
            yields=[0.03, 0.035, 0.04, 0.042, 0.045],
        )
        assert curve.interpolate(0.5) == pytest.approx(0.03)
        assert curve.interpolate(10.0) == pytest.approx(0.045)

    def test_single_point_curve_raises(self):
        curve = YieldCurve(
            curve_date=ISSUE, currency="USD",
            tenors=[5.0], yields=[0.05],
        )
        with pytest.raises(Unavailable, match="curve interpolation needs >=2"):
            curve.interpolate(5.0)

    def test_forward_rate_consistency(self):
        curve = YieldCurve(
            curve_date=ISSUE, currency="USD",
            tenors=[1.0, 2.0, 3.0, 4.0, 5.0],
            yields=[0.03, 0.035, 0.04, 0.042, 0.045],
        )
        r1, r2 = curve.zero_rate(2.0), curve.zero_rate(5.0)
        expected_fwd = (r2 * 5.0 - r1 * 2.0) / (5.0 - 2.0)
        assert curve.forward_rate(2.0, 5.0) == pytest.approx(expected_fwd)

    def test_forward_rate_with_t1_ge_t2_raises(self):
        curve = YieldCurve(
            curve_date=ISSUE, currency="USD",
            tenors=[1.0, 2.0, 3.0, 4.0, 5.0],
            yields=[0.03, 0.035, 0.04, 0.042, 0.045],
        )
        with pytest.raises(ValueError):
            curve.forward_rate(5.0, 2.0)


class TestNelsonSiegel:
    def test_flat_at_long_end(self):
        # As tau -> infinity, NS -> beta0 (with beta1, beta2 contributing ~0).
        out = nelson_siegel(50.0, beta0=0.04, beta1=-0.01, beta2=0.005, lambda_=1.0)
        assert out == pytest.approx(0.04, abs=1e-3)

    def test_tau_zero_returns_beta0(self):
        assert nelson_siegel(0.0, 0.04, -0.01, 0.005, 1.0) == pytest.approx(0.04)

    def test_fit_recovers_input_yields(self):
        # NS parameter recovery is ill-conditioned (lambda in particular is
        # hard to pin down from L-BFGS-B starting at x0[3]=1). What the
        # optimiser actually minimises is yield reproduction at the input
        # tenors -- that is the behaviour worth pinning.
        true = (0.05, -0.01, 0.02, 1.5)
        tenors = [0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 20.0, 30.0]
        yields = [nelson_siegel(t, *true) for t in tenors]
        fitted = fit_nelson_siegel(tenors, yields)
        # L-BFGS-B converges to a near-optimal solution from x0=[mean,0,0,1];
        # 5e-4 is the realistic reproduction tolerance for this optimiser on
        # this ill-conditioned problem. v1 has no test for this math so there
        # is no oracle to be faithful to; we pin what the algorithm achieves.
        for t, y in zip(tenors, yields):
            assert nelson_siegel(t, *fitted) == pytest.approx(y, abs=5e-4)

    def test_fit_too_few_points_raises(self):
        with pytest.raises(Unavailable, match="Nelson-Siegel fit needs"):
            fit_nelson_siegel([1.0, 2.0], [0.03, 0.04])


class TestBuildYieldCurve:
    def test_build_from_zero_coupon_bonds(self):
        # Construct zero-coupon bonds at known yields and rebuild the curve.
        zeros = []
        tenors_yields = [(1.0, 0.03), (2.0, 0.035), (3.0, 0.04), (5.0, 0.045), (10.0, 0.05)]
        for years, ytm in tenors_yields:
            maturity = date(2020, 1, 1)
            from datetime import timedelta as _td

            maturity = date(2020, 1, 1)
            for _ in range(int(years * 365)):
                maturity = maturity + _td(days=1)
            zeros.append(
                Bond(
                    cusip=f"Z{years}", isin=f"Z{years}", issuer="T",
                    issue_date=date(2020, 1, 1), maturity_date=maturity,
                    coupon_rate=0.0, coupon_frequency=CouponFrequency.ZERO,
                    face_value=100.0,
                    price=100.0 / (1 + ytm) ** years,
                    yield_to_maturity=ytm,
                    settlement_date=date(2020, 1, 1),
                    day_count=DayCountConvention.ACTUAL_ACTUAL,
                )
            )
        curve = build_yield_curve(zeros, date(2020, 1, 1), method="nelson_siegel")
        # The fitted curve must price the 5y zero close to its input yield.
        y5 = curve.interpolate(5.0)
        assert y5 == pytest.approx(0.045, abs=0.005)

    def test_build_no_bonds_raises(self):
        with pytest.raises(Unavailable, match="no bonds"):
            build_yield_curve([], date(2020, 1, 1))

    def test_build_no_zero_coupons_raises(self):
        # All coupon bonds -> cannot bootstrap.
        coupon = _par_coupon_bond()
        with pytest.raises(Unavailable, match="no zero-coupon bonds"):
            build_yield_curve([coupon], ISSUE)


# ---------------------------------------------------------------------------
# Accrued interest and total return
# ---------------------------------------------------------------------------


class TestAccruedInterest:
    def test_zero_on_settlement_date(self):
        bond = _par_coupon_bond()
        # Settle on a coupon date -> no days accrued.
        assert calculate_accrued_interest(bond, date(2021, 1, 15)) == pytest.approx(0.0)

    def test_accrues_linearly_into_next_period(self):
        bond = _par_coupon_bond()
        # Settle 3 months into the 2021->2022 coupon period.
        days_in_period = (date(2022, 1, 15) - date(2021, 1, 15)).days
        days_accrued = (date(2021, 4, 15) - date(2021, 1, 15)).days
        expected = 5.0 * days_accrued / days_in_period
        assert calculate_accrued_interest(bond, date(2021, 4, 15)) == pytest.approx(expected)

    def test_zero_coupon_returns_zero(self):
        bond = _zero_bond()
        assert calculate_accrued_interest(bond, date(2022, 6, 15)) == 0.0


class TestCalculateTotalReturn:
    def test_known_total_return_components(self):
        # Hold the par bond for exactly one coupon period; ending yield
        # unchanged. 2020 is a leap year, so the 366-day holding period lands
        # on 2021-01-15 -- the first coupon date.
        bond = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        out = calculate_total_return(
            bond,
            holding_period_days=366,
            ending_yield=0.05,
            reinvestment_rate=0.05,
        )
        # At unchanged yield over a clean coupon period, the ending bond is
        # still a par bond (4 years to maturity, 5% coupon, 5% yield) -> price
        # return is ~0. The only income is the 2021 coupon received (with
        # zero reinvestment because the holding period ends on its date).
        assert out["coupon_income"] == pytest.approx(5.0, abs=1e-9)
        assert out["reinvestment_income"] == pytest.approx(0.0, abs=1e-9)
        assert out["price_return"] == pytest.approx(0.0, abs=1e-9)
        assert out["total_return"] == pytest.approx(5.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Credit migration
# ---------------------------------------------------------------------------


class TestAnalyzeCreditMigration:
    def test_no_rating_raises(self):
        bond = _par_coupon_bond()
        bond.rating = None
        import pandas as pd

        with pytest.raises(Unavailable, match="no credit rating"):
            analyze_credit_migration(bond, pd.DataFrame(), {})

    def test_known_downgrade_impact(self):
        import pandas as pd

        bond = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        bond.rating = "AAA"
        # Single scenario: AAA -> AA with probability 0.1 and +50bp spread.
        tm = pd.DataFrame(
            {"AA": [0.1], "AAA": [0.9]}, index=["AAA"]
        )
        spreads = {"AAA": 0.0, "AA": 0.005}
        out = analyze_credit_migration(bond, tm, spreads)
        assert "AA" in out["migration_scenarios"]
        aa = out["migration_scenarios"]["AA"]
        assert aa["probability"] == pytest.approx(0.1)
        assert aa["spread_change_bps"] == pytest.approx(50.0)
        # Downgrade probability must include the AA scenario.
        assert out["downgrade_probability"] == pytest.approx(0.1)

    def test_missing_current_rating_in_spreads_raises(self):
        # The bond's current rating is absent from rating_spreads. v1 (and the
        # original port) silently treated the current spread as 0, producing a
        # wrong migration number for a legal input -- the auditor showed 50bp
        # where the correct answer is 40bp. A missing current spread is
        # unanalysable, so the call must refuse rather than fabricate.
        import pandas as pd

        bond = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        bond.rating = "AAA"
        tm = pd.DataFrame({"AA": [0.1], "AAA": [0.9]}, index=["AAA"])
        spreads = {"AA": 0.005}  # AAA omitted -- easy caller mistake
        with pytest.raises(Unavailable, match="AAA"):
            analyze_credit_migration(bond, tm, spreads)

    def test_upgrade_does_not_count_as_downgrade(self):
        import pandas as pd

        bond = _par_coupon_bond(coupon_rate=0.05, ytm=0.05)
        bond.rating = "AA"
        tm = pd.DataFrame({"AAA": [0.05], "AA": [0.95]}, index=["AA"])
        spreads = {"AAA": 0.0, "AA": 0.005}
        out = analyze_credit_migration(bond, tm, spreads)
        assert out["downgrade_probability"] == pytest.approx(0.0)
