"""Tests for the basis exposure probe.

Every test here targets a decision that, made the other way, produces an
honest-looking wrong number rather than an error:

  - the basis sign, which decides whether a long-spot/short-perp book is being
    reported its gains or its losses
  - the session-liquidity floor priced off the PERP close, so a corrupt spot
    print cannot vouch for itself
  - the six-week change matched on the CALENDAR date rather than the row
    offset, so gaps do not silently widen the measured distribution
  - the max/sd guard, which divides by a standard deviation that np.std returns
    as ~1e-15 for a constant series rather than 0.0

Each is checked by constructing the case where the wrong implementation returns
a number and the right one does not.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.basis_exposure import (
    MIN_ALIGNED_DAYS,
    Unfillable,
    align_basis,
    bars_by_date,
    degenerate,
    describe,
    describe_changes,
    hold_changes,
    pair_economics,
    resolve_pair,
    vol_link,
)

DAY0 = date(2026, 1, 1)


def _legs(
    pairs: list[tuple[float, float]],
    *,
    spot_volume: float = 1000.0,
    perp_volume: float = 1000.0,
    start: date = DAY0,
) -> tuple[dict, dict]:
    """(spot, perp) date -> (close, volume) maps from a list of (spot, perp) closes."""
    spot, perp = {}, {}
    for i, (s, p) in enumerate(pairs):
        d = start + timedelta(days=i)
        spot[d] = (s, spot_volume)
        perp[d] = (p, perp_volume)
    return spot, perp


class TestAlignBasis:
    def test_basis_is_perp_over_spot_and_signed_toward_the_perp(self):
        spot, perp = _legs([(100.0, 101.0), (200.0, 198.0)])
        out = align_basis(spot, perp, min_session_notional=0.0)

        # (101-100)/100 = +100 bps. Flipping the sign gives -100; dividing by
        # the perp gives +99.01; both fail.
        assert out["basis_bps"][0] == pytest.approx(100.0)
        # (198-200)/200 = -100 bps. A perp BELOW spot is a negative basis, which
        # is the favourable direction for long spot / short perp.
        assert out["basis_bps"][1] == pytest.approx(-100.0)

    def test_only_dates_present_on_both_legs_are_measured(self):
        spot, perp = _legs([(100.0, 101.0), (100.0, 101.0), (100.0, 101.0)])
        del perp[DAY0 + timedelta(days=1)]
        out = align_basis(spot, perp, min_session_notional=0.0)

        assert out["dates"] == [DAY0, DAY0 + timedelta(days=2)]

    def test_zero_volume_listing_print_is_excluded_by_name_not_averaged_in(self):
        # Hyperliquid's BTC spot book (@142) opened with sessions closing at
        # 6,969,696 against a perp at 101,367 on zero volume. Admitting them
        # reports a -9,855 bps basis nobody could cross.
        spot, perp = _legs([(100.0, 101.0)] * 3)
        spot[DAY0 + timedelta(days=1)] = (6_969_696.0, 0.0)
        perp[DAY0 + timedelta(days=1)] = (101_367.0, 58_125.0)

        out = align_basis(spot, perp, min_session_notional=1000.0)

        assert out["dates"] == [DAY0, DAY0 + timedelta(days=2)]
        assert list(out["basis_bps"]) == pytest.approx([100.0, 100.0])

        excluded = out["excluded"]
        assert len(excluded) == 1
        assert excluded[0]["reason"] == "no_market"
        assert excluded[0]["date"] == DAY0 + timedelta(days=1)
        # The raw basis is carried out rather than discarded: the operator sees
        # exactly what the exclusion removed.
        assert excluded[0]["basis_bps"] == pytest.approx(-9854.6, abs=0.1)

    def test_liquidity_floor_prices_the_spot_leg_off_the_perp_close(self):
        # The spot leg traded 0.01 units. At the corrupt spot close of 1,000,000
        # that is $10,000 and passes a $1,000 floor; at the perp's 100 it is $1
        # and does not. Pricing the floor off the spot close lets a bad print
        # vouch for its own admissibility.
        spot = {DAY0: (1_000_000.0, 0.01)}
        perp = {DAY0: (100.0, 1000.0)}

        out = align_basis(spot, perp, min_session_notional=1000.0)

        assert out["dates"] == []
        assert out["excluded"][0]["reason"] == "no_market"
        assert out["excluded"][0]["spot_usd"] == pytest.approx(1.0)

    def test_liquid_session_survives_the_floor(self):
        spot = {DAY0: (100.0, 50.0)}
        perp = {DAY0: (101.0, 50.0)}

        out = align_basis(spot, perp, min_session_notional=1000.0)

        assert out["dates"] == [DAY0]
        assert out["excluded"] == []

    def test_thin_leg_excludes_the_session_whichever_side_is_thin(self):
        spot = {DAY0: (100.0, 1000.0)}
        perp = {DAY0: (101.0, 0.5)}

        out = align_basis(spot, perp, min_session_notional=1000.0)

        assert out["dates"] == []
        assert out["excluded"][0]["reason"] == "no_market"

    def test_nonpositive_spot_close_is_refused_rather_than_divided_by(self):
        spot = {DAY0: (0.0, 1000.0), DAY0 + timedelta(days=1): (-5.0, 1000.0)}
        perp = {DAY0: (101.0, 1000.0), DAY0 + timedelta(days=1): (101.0, 1000.0)}

        out = align_basis(spot, perp, min_session_notional=0.0)

        assert out["dates"] == []
        assert [e["reason"] for e in out["excluded"]] == ["not_a_price", "not_a_price"]
        assert all(e["basis_bps"] is None for e in out["excluded"])

    def test_nonfinite_close_is_refused(self):
        spot = {DAY0: (float("nan"), 1000.0), DAY0 + timedelta(days=1): (100.0, 1000.0)}
        perp = {DAY0: (101.0, 1000.0), DAY0 + timedelta(days=1): (float("inf"), 1000.0)}

        out = align_basis(spot, perp, min_session_notional=0.0)

        assert out["dates"] == []
        assert [e["reason"] for e in out["excluded"]] == ["not_a_price", "not_a_price"]


class TestBarsByDate:
    def test_close_and_volume_are_taken_from_the_ohlcv_positions(self):
        ts = 1_767_225_600_000  # 2026-01-01T00:00:00Z
        out = bars_by_date([[ts, 1.0, 9.0, 0.5, 7.0, 42.0]])

        # close is index 4 and volume index 5. Taking the high (9.0) or the open
        # (1.0) as the close is the mistake this pins down.
        assert out == {date(2026, 1, 1): (7.0, 42.0)}

    def test_repeated_timestamp_keeps_the_later_row(self):
        ts = 1_767_225_600_000
        out = bars_by_date([[ts, 1, 1, 1, 7.0, 1.0], [ts, 1, 1, 1, 8.0, 2.0]])

        assert out[date(2026, 1, 1)] == (8.0, 2.0)

    def test_truncated_or_null_rows_are_skipped(self):
        ts = 1_767_225_600_000
        out = bars_by_date([[ts, 1, 1, 1, 7.0], [ts + 86_400_000, 1, 1, 1, None, 1.0]])

        assert out == {}


class TestDescribe:
    def test_statistics_are_the_stated_ones(self):
        values = np.array([-30.0] + [0.0] * (MIN_ALIGNED_DAYS - 2) + [10.0])
        out = describe(values)

        assert out["n"] == MIN_ALIGNED_DAYS
        assert out["mean_bps"] == pytest.approx(values.mean())
        assert out["sd_bps"] == pytest.approx(float(np.std(values, ddof=1)))
        # max ABSOLUTE basis is 30, not the max signed value of 10.
        assert out["max_abs_bps"] == pytest.approx(30.0)
        assert out["min_bps"] == pytest.approx(-30.0)
        assert out["max_bps"] == pytest.approx(10.0)
        assert out["max_in_sd"] == pytest.approx(30.0 / float(np.std(values, ddof=1)))

    def test_too_few_days_refuses_instead_of_reporting_a_percentile(self):
        with pytest.raises(Unfillable) as exc:
            describe(np.zeros(MIN_ALIGNED_DAYS - 1))

        assert "aligned days" in str(exc.value)

    def test_constant_series_refuses_max_over_sd_with_a_reason(self):
        # np.std(ddof=1) of sixty copies of 3.7 is 1.34e-15, NOT 0.0, so an
        # `if sd == 0` guard never fires and max/sd returns ~2.7e15 -- a
        # confident number computed from rounding error. This is the case that
        # tells the two guards apart.
        values = np.full(60, 3.7)
        assert float(np.std(values, ddof=1)) != 0.0

        out = describe(values)

        assert out["max_in_sd"] is None
        assert out["max_in_sd_reason"] is not None
        assert out["sd_bps"] < 1e-9

    def test_a_genuinely_small_but_real_sd_is_still_reported(self):
        # The guard must not swallow a real distribution. An sd of 1e-6 bps is
        # tiny and real; refusing it would be the opposite failure.
        values = np.zeros(60)
        values[0] = 1e-5
        out = describe(values)

        assert out["max_in_sd"] is not None
        assert out["max_in_sd"] > 1.0


class TestHoldChanges:
    def test_change_is_exit_minus_entry_so_a_rising_basis_is_positive(self):
        dates = [DAY0 + timedelta(days=i) for i in range(11)]
        basis = np.array([0.0] * 5 + [50.0] * 6)

        changes, unmatched = hold_changes(dates, basis, 5)

        # Entry at day 0 (basis 0) exits at day 5 (basis 50): +50, adverse for
        # long spot / short perp. Reversing the subtraction reports -50 and
        # turns every loss into a gain.
        assert changes[0] == pytest.approx(50.0)
        assert changes.max() == pytest.approx(50.0)
        assert unmatched == 5

    def test_matching_is_by_calendar_date_not_row_offset(self):
        # Day 3 is missing from both legs. Offsetting by 5 ROWS pairs day 0 with
        # day 6; matching on the calendar pairs it with day 5. The two disagree
        # by design here: day 5 is +10 and day 6 is +999.
        dates = [DAY0 + timedelta(days=i) for i in (0, 1, 2, 4, 5, 6, 7)]
        basis = np.array([0.0, 1.0, 2.0, 4.0, 10.0, 999.0, 7.0])

        changes, unmatched = hold_changes(dates, basis, 5)

        assert changes[0] == pytest.approx(10.0)
        assert 999.0 not in [pytest.approx(c) for c in changes]
        # Entries on days 4, 5, 6, 7 have no exit at +5 days; day 3 is absent as
        # an entry too, so 4 of 7 entries go unmatched.
        assert unmatched == 4
        assert len(changes) == 3

    def test_no_matching_exit_produces_no_changes_rather_than_a_shorter_hold(self):
        dates = [DAY0 + timedelta(days=i) for i in range(3)]
        basis = np.array([0.0, 1.0, 2.0])

        changes, unmatched = hold_changes(dates, basis, 42)

        assert changes.size == 0
        assert unmatched == 3


class TestDescribeChanges:
    def test_worst_adverse_is_the_maximum_positive_change(self):
        changes = np.array([-500.0] * (MIN_ALIGNED_DAYS - 1) + [12.0])
        out = describe_changes(changes)

        # -500 is the largest magnitude and the most FAVOURABLE move. Reporting
        # it as the worst adverse case is the error this catches.
        assert out["worst_adverse_bps"] == pytest.approx(12.0)
        assert out["best_favourable_bps"] == pytest.approx(-500.0)
        assert out["max_abs_bps"] == pytest.approx(500.0)

    def test_too_few_holds_refuses(self):
        with pytest.raises(Unfillable) as exc:
            describe_changes(np.zeros(MIN_ALIGNED_DAYS - 1))

        assert "entry/exit pairs" in str(exc.value)


class TestPairEconomics:
    def test_earnings_and_cost_are_the_stated_arithmetic(self):
        out = pair_economics(
            notional=10_000.0, carry_pct_yr=11.50, hold_days=42, adverse_bps=180.0
        )

        # 10000 * 0.115 * 42/365
        assert out["earned_usd"] == pytest.approx(132.3287671, abs=1e-5)
        # 10000 * 180/10000
        assert out["adverse_cost_usd"] == pytest.approx(180.0)
        assert out["cost_over_earned"] == pytest.approx(180.0 / 132.3287671, abs=1e-5)
        # The carry over the hold, expressed in the same bps units as the basis:
        # 132.33 bps absorbed before the hold is flat.
        assert out["breakeven_bps"] == pytest.approx(132.3287671, abs=1e-5)

    def test_the_binance_rate_absorbs_less_basis_than_the_hyperliquid_rate(self):
        binance = pair_economics(
            notional=10_000.0, carry_pct_yr=7.80, hold_days=42, adverse_bps=180.0
        )
        hyperliquid = pair_economics(
            notional=10_000.0, carry_pct_yr=11.50, hold_days=42, adverse_bps=180.0
        )

        assert binance["breakeven_bps"] == pytest.approx(89.75342, abs=1e-4)
        assert binance["cost_over_earned"] > hyperliquid["cost_over_earned"]
        assert binance["cost_over_earned"] == pytest.approx(2.0055, abs=1e-3)

    def test_the_ratio_is_size_invariant_and_the_dollars_are_not(self):
        # $210 is the book's actual NAV. Both legs of the comparison scale with
        # notional, so the ratio is identical at $210 and at $10,000 -- which is
        # exactly why "it is only $3.78" is not an argument that the exposure is
        # bounded. It is the same 1.36x either way.
        small = pair_economics(
            notional=210.0, carry_pct_yr=11.50, hold_days=42, adverse_bps=180.0
        )
        large = pair_economics(
            notional=10_000.0, carry_pct_yr=11.50, hold_days=42, adverse_bps=180.0
        )

        assert small["adverse_cost_usd"] == pytest.approx(3.78)
        assert large["adverse_cost_usd"] == pytest.approx(180.0)
        assert small["cost_over_earned"] == pytest.approx(large["cost_over_earned"])
        assert small["cost_over_earned"] == pytest.approx(1.3602484, abs=1e-6)

    def test_a_longer_hold_absorbs_more_basis(self):
        six_weeks = pair_economics(
            notional=10_000.0, carry_pct_yr=11.50, hold_days=42, adverse_bps=180.0
        )
        twelve_weeks = pair_economics(
            notional=10_000.0, carry_pct_yr=11.50, hold_days=84, adverse_bps=180.0
        )

        assert twelve_weeks["breakeven_bps"] == pytest.approx(
            2 * six_weeks["breakeven_bps"], abs=1e-6
        )
        assert twelve_weeks["cost_over_earned"] == pytest.approx(
            six_weeks["cost_over_earned"] / 2, abs=1e-6
        )

    def test_zero_notional_refuses_rather_than_dividing(self):
        with pytest.raises(Unfillable):
            pair_economics(
                notional=0.0, carry_pct_yr=11.50, hold_days=42, adverse_bps=180.0
            )

    def test_zero_hold_refuses(self):
        with pytest.raises(Unfillable):
            pair_economics(
                notional=10_000.0, carry_pct_yr=11.50, hold_days=0, adverse_bps=180.0
            )


class TestDegenerate:
    def test_constant_series_is_degenerate_at_every_magnitude(self):
        # The tolerance is relative, so it has to answer the same way for a
        # basis of 3.7 bps and for a return of 0.000037. An ABSOLUTE tolerance
        # calls one of these constant and the other not.
        for value in (3.7, 0.05, 1.76, 3.7e-5, 3.7e5):
            assert degenerate(np.full(60, value)), value

        # And the reason the guard cannot be an equality: for the values that
        # are not exactly representable in binary64, np.std of sixty identical
        # copies is not 0.0.
        for value in (3.7, 0.05, 1.76, 3.7e-5):
            assert float(np.std(np.full(60, value), ddof=1)) != 0.0, value

    def test_a_real_distribution_is_not_degenerate_at_every_magnitude(self):
        # 1e-12 is the case that separates a relative tolerance from an absolute
        # one. This series is a clean ramp with sixty distinct values -- as
        # non-degenerate as a series gets -- but its standard deviation is
        # 1.7e-11, so a fixed `sd <= 1e-9` calls it constant and refuses a
        # measurement that is perfectly well posed. Relative to the series' own
        # magnitude the spread is enormous.
        for scale in (1e-12, 1e-6, 1.0, 1e6):
            series = np.arange(60, dtype=float) * scale
            assert not degenerate(series), scale

    def test_all_zero_nonfinite_and_single_point_are_degenerate(self):
        assert degenerate(np.zeros(60))
        assert degenerate(np.array([1.0, 2.0, np.nan, 4.0] * 15))
        assert degenerate(np.array([1.0, 2.0, np.inf]))
        assert degenerate(np.array([1.0]))


class TestVolLink:
    @staticmethod
    def _perp(returns: list[float], start: date = DAY0) -> dict:
        closes, price = {}, 100.0
        closes[start - timedelta(days=1)] = (price, 1000.0)
        for i, r in enumerate(returns):
            price *= 1.0 + r
            closes[start + timedelta(days=i)] = (price, 1000.0)
        return closes

    def test_basis_that_widens_on_big_moves_is_reported_as_linked(self):
        n = 80
        returns = [0.001] * n
        basis = [2.0] * n
        for i in range(0, n, 10):
            returns[i] = 0.15
            basis[i] = 150.0
        dates = [DAY0 + timedelta(days=i) for i in range(n)]

        out = vol_link(dates, np.array(basis), self._perp(returns))

        assert out["pearson"] > 0.9
        assert out["volatile_mean_abs_bps"] == pytest.approx(150.0)
        assert out["quiet_mean_abs_bps"] == pytest.approx(2.0)
        assert out["volatile_max_abs_bps"] > out["quiet_max_abs_bps"]

    def test_basis_unrelated_to_the_move_is_reported_as_unlinked(self):
        # The wide-basis days and the big-move days are deliberately disjoint.
        n = 80
        returns = [0.001] * n
        basis = [2.0] * n
        for i in range(0, n, 10):
            returns[i] = 0.15
        for i in range(5, n, 10):
            basis[i] = 150.0
        dates = [DAY0 + timedelta(days=i) for i in range(n)]

        out = vol_link(dates, np.array(basis), self._perp(returns))

        assert out["pearson"] < 0.0
        assert out["volatile_mean_abs_bps"] == pytest.approx(2.0)
        assert out["quiet_mean_abs_bps"] > out["volatile_mean_abs_bps"]

    def test_correlation_uses_absolute_basis_so_a_wide_discount_counts(self):
        # A basis of -150 bps on a volatile day is just as much exposure as
        # +150. Correlating the SIGNED basis reports these as unlinked.
        n = 80
        returns = [0.001] * n
        basis = [2.0] * n
        for i in range(0, n, 10):
            returns[i] = 0.15
            basis[i] = -150.0
        dates = [DAY0 + timedelta(days=i) for i in range(n)]

        out = vol_link(dates, np.array(basis), self._perp(returns))

        assert out["pearson"] > 0.9
        assert out["volatile_mean_abs_bps"] == pytest.approx(150.0)

    def test_constant_basis_refuses_rather_than_correlating_rounding_error(self):
        n = 80
        dates = [DAY0 + timedelta(days=i) for i in range(n)]
        returns = [0.01 * (i % 7) for i in range(n)]

        with pytest.raises(Unfillable) as exc:
            vol_link(dates, np.full(n, 3.7), self._perp(returns))

        assert "usable spread" in str(exc.value)

    def test_too_few_sessions_with_a_prior_close_refuses(self):
        dates = [DAY0 + timedelta(days=i) for i in range(10)]
        returns = [0.01 * (i % 7) for i in range(10)]

        with pytest.raises(Unfillable) as exc:
            vol_link(dates, np.arange(10, dtype=float), self._perp(returns))

        assert "prior close" in str(exc.value)

    def test_a_session_without_the_previous_day_is_dropped_not_backfilled(self):
        n = 80
        dates = [DAY0 + timedelta(days=i) for i in range(n)]
        returns = [0.01 * (i % 7) + 0.001 for i in range(n)]
        perp = self._perp(returns)
        # Remove the prior closes for the first 40 sessions. Those days cannot
        # produce a return and must be dropped, not carried forward from an
        # older close, which would invent a multi-day move as a daily one.
        for i in range(-1, 39):
            perp.pop(DAY0 + timedelta(days=i), None)

        out = vol_link(dates, np.arange(n, dtype=float), perp)

        assert out["n"] == 40


class TestResolvePair:
    MARKETS: ClassVar[dict] = {
        "BTC/USDC": {"base": "BTC", "quote": "USDC", "spot": True, "swap": False},
        "BTC/USDH": {"base": "BTC", "quote": "USDH", "spot": True, "swap": False},
        "BTC/USDC:USDC": {"base": "BTC", "quote": "USDC", "spot": False, "swap": True},
        "HYPE/USDC": {"base": "HYPE", "quote": "USDC", "spot": True, "swap": False},
        "HYPE/USDT": {"base": "HYPE", "quote": "USDT", "spot": True, "swap": False},
        "HYPE/USDC:USDC": {"base": "HYPE", "quote": "USDC", "spot": False, "swap": True},
        "FOO/USDC": {"base": "FOO", "quote": "USDC", "spot": True, "swap": False},
        "BAR/USDC:USDC": {"base": "BAR", "quote": "USDC", "spot": False, "swap": True},
    }

    def test_both_legs_resolve_against_the_same_quote(self):
        assert resolve_pair(self.MARKETS, "BTC") == ("BTC/USDC", "BTC/USDC:USDC")

    def test_alternate_quote_listings_do_not_create_ambiguity(self):
        # HYPE lists against USDC, USDT, USDH and USDE. Matching on the base
        # alone would raise ambiguity and drop a held name entirely.
        assert resolve_pair(self.MARKETS, "HYPE") == ("HYPE/USDC", "HYPE/USDC:USDC")

    def test_missing_perp_leg_is_unfillable_by_name(self):
        with pytest.raises(Unfillable) as exc:
            resolve_pair(self.MARKETS, "FOO")

        assert "perpetual" in str(exc.value)

    def test_missing_spot_leg_is_unfillable_by_name(self):
        with pytest.raises(Unfillable) as exc:
            resolve_pair(self.MARKETS, "BAR")

        assert "spot" in str(exc.value)

    def test_unknown_base_is_unfillable(self):
        with pytest.raises(Unfillable):
            resolve_pair(self.MARKETS, "NOPE")
