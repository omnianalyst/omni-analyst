"""Behaviour tests for the options capability module.

Two kinds of assertion only:

1. Closed-form BSM identities (put-call parity, K=0 call, delta limits, vega
   sign, expiry = intrinsic, IV round-trip) -- the work order's required
   outcome. These are arithmetic truths, not shape checks.
2. Honest failure -- every named failure path raises ``Unavailable`` or
   returns ``None`` rather than producing a number.

No fixture here is copied from an imagined provider payload. Every chain is
built from stated arithmetic in the test body.
"""

import math

import numpy as np
import pytest

from omni.capabilities.options import (
    black_scholes,
    build_volatility_surface,
    detect_unusual_activity,
    implied_volatility,
    max_pain,
    monte_carlo,
    put_call_parity_errors,
    put_call_ratio,
    scan_option_strategies,
)
from omni.ingest.protocol import Unavailable

S = 100.0
K = 100.0
T = 0.5
R = 0.05
SIGMA = 0.20
Q = 0.0


# ---------------------------------------------------------------------------
# Closed-form BSM identities
# ---------------------------------------------------------------------------


class TestPutCallParity:
    def test_parity_no_dividend(self):
        call = black_scholes(S, K, T, R, SIGMA, 0.0, "call")["price"]
        put = black_scholes(S, K, T, R, SIGMA, 0.0, "put")["price"]
        # C - P = S - K*exp(-rT)
        assert call - put == pytest.approx(S - K * math.exp(-R * T), abs=1e-9)

    def test_parity_with_dividend(self):
        q = 0.03
        call = black_scholes(S, K, T, R, SIGMA, q, "call")["price"]
        put = black_scholes(S, K, T, R, SIGMA, q, "put")["price"]
        # C - P = S*exp(-qT) - K*exp(-rT)
        assert call - put == pytest.approx(S * math.exp(-q * T) - K * math.exp(-R * T), abs=1e-9)

    def test_parity_off_strike(self):
        # Parity holds at every strike, not just ATM.
        for strike in (70.0, 85.0, 100.0, 115.0, 140.0):
            call = black_scholes(S, strike, T, R, SIGMA, 0.0, "call")["price"]
            put = black_scholes(S, strike, T, R, SIGMA, 0.0, "put")["price"]
            assert call - put == pytest.approx(S - strike * math.exp(-R * T), abs=1e-9)


class TestStrikeZeroCall:
    def test_call_struck_at_zero_equals_discounted_spot_minus_strike(self):
        # K -> 0: the call pays S at expiry for certain, so C = S*exp(-qT) -
        # 0*exp(-rT) = S*exp(-qT). The work order phrases this as "spot minus
        # the discounted strike"; with K=0 the discounted strike is 0.
        q = 0.02
        price = black_scholes(S, 1e-12, T, R, SIGMA, q, "call")["price"]
        # K must stay positive for log(S/K); use a negligible strike.
        assert price == pytest.approx(S * math.exp(-q * T), rel=1e-6)


class TestDeltaLimits:
    def test_deep_itm_call_delta_approaches_one(self):
        # Spot far above strike: delta -> exp(-qT).
        q = 0.0
        res = black_scholes(1000.0, K, T, R, SIGMA, q, "call")
        assert res["delta"] == pytest.approx(math.exp(-q * T), abs=1e-3)

    def test_deep_otm_call_delta_approaches_zero(self):
        res = black_scholes(1.0, K, T, R, SIGMA, 0.0, "call")
        assert res["delta"] == pytest.approx(0.0, abs=1e-3)

    def test_deep_itm_put_delta_approaches_negative_exp_qT(self):
        q = 0.0
        res = black_scholes(1.0, K, T, R, SIGMA, q, "put")
        assert res["delta"] == pytest.approx(-math.exp(-q * T), abs=1e-3)


class TestVegaPositive:
    def test_vega_positive_call(self):
        for strike in (80.0, 95.0, 100.0, 105.0, 130.0):
            assert black_scholes(S, strike, T, R, SIGMA, 0.0, "call")["vega"] > 0.0

    def test_vega_positive_put(self):
        for strike in (80.0, 95.0, 100.0, 105.0, 130.0):
            assert black_scholes(S, strike, T, R, SIGMA, 0.0, "put")["vega"] > 0.0

    def test_vega_identical_for_call_and_put(self):
        # Gamma and vega are type-symmetric in BSM; sanity check.
        vc = black_scholes(S, K, T, R, SIGMA, 0.0, "call")["vega"]
        vp = black_scholes(S, K, T, R, SIGMA, 0.0, "put")["vega"]
        assert vc == pytest.approx(vp, abs=1e-12)


class TestVegaValue:
    # Q1 F3: the sign/symmetry assertions above pass for `vega = sigma` (a
    # constant with no relationship to the BSM vega). These pin the value at
    # two strikes, computed independently from S*exp(-qT)*pdf(d1)*sqrt(T)/100.
    # `vega = sigma = 0.20` is off by 0.074 (ATM) and 0.123 (OTM) and fails.

    def test_atm_vega_value(self):
        v = black_scholes(S, K, T, R, SIGMA, 0.0, "call")["vega"]
        assert v == pytest.approx(0.2736, abs=1e-3)

    def test_otm_vega_value(self):
        v = black_scholes(S, 130.0, T, R, SIGMA, 0.0, "call")["vega"]
        assert v == pytest.approx(0.0775, abs=1e-3)

    def test_vega_peaks_near_atm(self):
        # A constant fake gives equal vega everywhere; real BSM vega peaks near
        # the money and falls off on both sides.
        v_itm = black_scholes(S, 80.0, T, R, SIGMA, 0.0, "call")["vega"]
        v_atm = black_scholes(S, K, T, R, SIGMA, 0.0, "call")["vega"]
        v_otm = black_scholes(S, 130.0, T, R, SIGMA, 0.0, "call")["vega"]
        assert v_atm > v_itm
        assert v_atm > v_otm


class TestExpiryIntrinsic:
    def test_call_at_expiry_is_intrinsic(self):
        itm = black_scholes(120.0, K, 0.0, R, SIGMA, 0.0, "call")
        assert itm["price"] == 20.0
        assert itm["delta"] == 1.0

    def test_call_at_expiry_otm_is_zero(self):
        otm = black_scholes(80.0, K, 0.0, R, SIGMA, 0.0, "call")
        assert otm["price"] == 0.0
        assert otm["delta"] == 0.0

    def test_put_at_expiry_is_intrinsic(self):
        itm = black_scholes(80.0, K, 0.0, R, SIGMA, 0.0, "put")
        assert itm["price"] == 20.0
        assert itm["delta"] == -1.0

    def test_put_at_expiry_otm_is_zero(self):
        otm = black_scholes(120.0, K, 0.0, R, SIGMA, 0.0, "put")
        assert otm["price"] == 0.0
        assert otm["delta"] == 0.0

    def test_gamma_theta_vega_rho_zero_at_expiry(self):
        res = black_scholes(110.0, K, 0.0, R, SIGMA, 0.0, "call")
        assert res["gamma"] == 0.0
        assert res["theta"] == 0.0
        assert res["vega"] == 0.0
        assert res["rho"] == 0.0


# ---------------------------------------------------------------------------
# Implied volatility round-trip
# ---------------------------------------------------------------------------


class TestImpliedVolatility:
    def test_round_trip_call(self):
        for sigma in (0.10, 0.20, 0.35, 0.50, 0.80):
            price = black_scholes(S, K, T, R, sigma, 0.0, "call")["price"]
            iv = implied_volatility(price, S, K, T, R, 0.0, "call")
            assert iv is not None
            assert iv == pytest.approx(sigma, abs=1e-6)

    def test_round_trip_put(self):
        for sigma in (0.15, 0.30, 0.60):
            price = black_scholes(S, K, T, R, sigma, 0.02, "put")["price"]
            iv = implied_volatility(price, S, K, T, R, 0.02, "put")
            assert iv is not None
            assert iv == pytest.approx(sigma, abs=1e-6)

    def test_round_trip_otm(self):
        # Deep OTM call: small price, still recovers sigma.
        sigma = 0.25
        price = black_scholes(S, 140.0, T, R, sigma, 0.0, "call")["price"]
        iv = implied_volatility(price, S, 140.0, T, R, 0.0, "call")
        assert iv is not None
        assert iv == pytest.approx(sigma, abs=1e-5)

    def test_non_convergent_returns_none(self):
        # A price no positive volatility can produce (above the asymptotic
        # ceiling). The solver cannot converge; must return None, not a guess.
        ceiling = black_scholes(S, K, T, R, _IV_CEILING, 0.0, "call")["price"]
        iv = implied_volatility(ceiling * 5.0, S, K, T, R, 0.0, "call")
        assert iv is None

    def test_unreachable_far_otm_returns_none(self):
        # Q1 F1: a far-OTM short-dated call whose maximum in-bound price (at
        # vol=_IV_BOUNDS[1]=5.0) is sub-penny. 5x that ceiling is a price no
        # in-bound vol can produce, but the squared residual (ceiling*4)**2
        # sits under the old `abs(result.fun) < _TOL` gate and the solver
        # returned the 5.0 bound instead of None. The effective linear residual
        # there is ~2.5e-4 -- far above _TOL.
        ceiling = black_scholes(100.0, 1000.0, 0.01, R, 5.0, 0.0, "call")["price"]
        iv = implied_volatility(ceiling * 5.0, 100.0, 1000.0, 0.01, R, 0.0, "call")
        assert iv is None

    def test_below_intrinsic_returns_none(self):
        intrinsic = max(S - K, 0.0)
        iv = implied_volatility(intrinsic - 1.0, S, K, T, R, 0.0, "call")
        assert iv is None

    def test_expired_returns_none(self):
        iv = implied_volatility(5.0, S, K, 0.0, R, 0.0, "call")
        assert iv is None


_IV_CEILING = 5.0


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------


class TestMonteCarlo:
    def test_seeded_reproducible(self):
        a = monte_carlo(S, K, T, R, SIGMA, 0.0, "call", seed=7)
        b = monte_carlo(S, K, T, R, SIGMA, 0.0, "call", seed=7)
        assert a["price"] == b["price"]

    def test_seeded_different_seed_differs(self):
        a = monte_carlo(S, K, T, R, SIGMA, 0.0, "call", seed=1)
        b = monte_carlo(S, K, T, R, SIGMA, 0.0, "call", seed=2)
        assert a["price"] != b["price"]

    def test_within_bs_confidence(self):
        # Q1 F4/F7: the previous form (a flat abs=0.15 plus a hard CI bracket
        # `ci_lower <= bs <= ci_upper`) passed for a no-simulation fake that
        # echoed the BSM price and fabricated std_error=1.0. A real Monte Carlo
        # must (a) carry a positive, CI-consistent standard error that shrinks
        # as 1/sqrt(simulations) -- proving a draw actually happened -- and
        # (b) land its point estimate within a few standard errors of the
        # closed-form price (a law-of-large-numbers bound), not within a flat
        # absolute band. The hard CI bracket is dropped: by construction a 95%
        # CI excludes the truth ~5% of the time, so it is seed-fragile and adds
        # no discriminating power over a wrong-but-wide-CI implementation.
        bs = black_scholes(S, K, T, R, SIGMA, 0.0, "call")["price"]
        mc = monte_carlo(
            S,
            K,
            T,
            R,
            SIGMA,
            0.0,
            "call",
            simulations=20000,
            time_steps=100,
            seed=42,
        )
        se = mc["std_error"]
        assert se > 0.0
        assert mc["confidence_interval_upper"] - mc["confidence_interval_lower"] == pytest.approx(
            2 * 1.96 * se, abs=1e-12
        )
        mc_quad = monte_carlo(
            S,
            K,
            T,
            R,
            SIGMA,
            0.0,
            "call",
            simulations=80000,
            time_steps=100,
            seed=42,
        )
        assert mc["std_error"] / mc_quad["std_error"] == pytest.approx(2.0, rel=0.05)
        assert abs(mc["price"] - bs) < 5.0 * se

    def test_put_within_bs_confidence(self):
        # Q1 F4: the previous `abs=0.15` point check passed for the no-simulation
        # fake on the put side too. Apply the same structural checks as the call.
        bs = black_scholes(S, K, T, R, SIGMA, 0.02, "put")["price"]
        mc = monte_carlo(
            S,
            K,
            T,
            R,
            SIGMA,
            0.02,
            "put",
            simulations=20000,
            time_steps=100,
            seed=42,
        )
        se = mc["std_error"]
        assert se > 0.0
        assert mc["confidence_interval_upper"] - mc["confidence_interval_lower"] == pytest.approx(
            2 * 1.96 * se, abs=1e-12
        )
        assert abs(mc["price"] - bs) < 5.0 * se

    def test_expiry_returns_intrinsic(self):
        mc = monte_carlo(120.0, K, 0.0, R, SIGMA, 0.0, "call", seed=1)
        assert mc["price"] == 20.0


# ---------------------------------------------------------------------------
# Volatility surface
# ---------------------------------------------------------------------------


class TestVolatilitySurface:
    def test_round_trips_a_grid(self):
        strikes = np.array([90.0, 100.0, 110.0])
        expiries = np.array([0.25, 0.5, 1.0])
        grid = np.array(
            [
                [black_scholes(S, k, t, R, 0.25, 0.0, "call")["price"] for t in expiries]
                for k in strikes
            ]
        )
        surface = build_volatility_surface(S, R, 0.0, strikes, expiries, grid, "call")
        # Every cell was produced from sigma=0.25, so the surface recovers it.
        assert np.allclose(surface, 0.25, atol=1e-6)

    def test_zero_price_cell_is_nan(self):
        strikes = np.array([100.0])
        expiries = np.array([0.5])
        grid = np.array([[0.0]])
        surface = build_volatility_surface(S, R, 0.0, strikes, expiries, grid, "call")
        assert math.isnan(surface[0, 0])


# ---------------------------------------------------------------------------
# Chain analytics
# ---------------------------------------------------------------------------


def _contract(strike, otype, *, bid=1.0, ask=2.0, volume=10, oi=100, iv=0.3, expiry=None, ttf=T):
    return {
        "strike": strike,
        "option_type": otype,
        "bid": bid,
        "ask": ask,
        "volume": volume,
        "open_interest": oi,
        "implied_volatility": iv,
        "expiry": expiry,
        "time_to_expiry": ttf,
    }


class TestPutCallRatio:
    def test_balanced_volumes(self):
        chain = [
            _contract(100, "call", volume=100),
            _contract(100, "put", volume=100),
        ]
        out = put_call_ratio(chain)
        assert out["ratio"] == 1.0
        assert out["call_volume"] == 100
        assert out["put_volume"] == 100
        assert out["sentiment"] == "NEUTRAL"

    def test_no_calls_is_inf(self):
        chain = [_contract(100, "put", volume=50)]
        out = put_call_ratio(chain)
        assert out["ratio"] == float("inf")
        assert out["sentiment"] == "EXTREMELY_BEARISH"

    def test_empty_chain_raises(self):
        with pytest.raises(Unavailable):
            put_call_ratio([])

    def test_zero_volume_raises(self):
        chain = [
            _contract(100, "call", volume=0),
            _contract(100, "put", volume=0),
        ]
        with pytest.raises(Unavailable):
            put_call_ratio(chain)


class TestMaxPain:
    def test_max_pain_at_zero_oi_raises(self):
        chain = [
            _contract(100, "call", oi=0),
            _contract(100, "put", oi=0),
        ]
        with pytest.raises(Unavailable):
            max_pain(chain)

    def test_known_pain(self):
        # Two strikes 95 and 105. ITM calls below the candidate add pain; ITM
        # puts above it add pain. With all OI equal, the strike minimising
        # total writer payoff is the median strike.
        chain = [
            _contract(95, "call", oi=50),
            _contract(95, "put", oi=50),
            _contract(105, "call", oi=50),
            _contract(105, "put", oi=50),
        ]
        out = max_pain(chain)
        assert out["strike"] in (95, 105)
        # At 95: calls below none; puts above 95 -> (105-95)*50 = 500.
        # At 105: calls below -> (105-95)*50 = 500; puts above none.
        # Both tie at 500; min() returns the first.
        assert out["total_pain"] == 500.0

    def test_single_strike_with_oi_is_well_defined(self):
        # Q1 F2: a single-strike chain with real open interest has zero
        # cross-strike pain at its only candidate (no option is ever ITM against
        # itself), so the sum of pain is 0.0. Max pain is still well-defined --
        # that strike, where writers pay nothing -- but the old guard summed the
        # pain, saw 0.0, and refused with a false "no open interest" message.
        chain = [
            _contract(100.0, "call", oi=50),
            _contract(100.0, "put", oi=50),
        ]
        out = max_pain(chain)
        assert out["strike"] == 100.0
        assert out["total_pain"] == 0.0

    def test_empty_chain_raises(self):
        with pytest.raises(Unavailable):
            max_pain([])


class TestUnusualActivity:
    def test_flags_high_volume_to_oi(self):
        chain = [_contract(100, "call", volume=300, oi=100)]
        alerts = detect_unusual_activity(chain)
        assert len(alerts) == 1
        assert alerts[0]["vol_oi_ratio"] == 3.0

    def test_no_flag_below_threshold(self):
        chain = [_contract(100, "call", volume=100, oi=100)]
        assert detect_unusual_activity(chain) == []

    def test_zero_oi_skipped(self):
        chain = [_contract(100, "call", volume=10000, oi=0)]
        assert detect_unusual_activity(chain) == []

    def test_empty_chain_raises(self):
        with pytest.raises(Unavailable):
            detect_unusual_activity([])


class TestParityErrors:
    def test_clean_chain_no_errors(self):
        # Build a chain whose mids exactly satisfy parity: mid = BSM price.
        call_price = black_scholes(S, K, T, R, SIGMA, 0.0, "call")["price"]
        put_price = black_scholes(S, K, T, R, SIGMA, 0.0, "put")["price"]
        chain = [
            _contract(K, "call", bid=call_price - 0.01, ask=call_price + 0.01),
            _contract(K, "put", bid=put_price - 0.01, ask=put_price + 0.01),
        ]
        errors = put_call_parity_errors(chain, S, R)
        assert errors == []

    def test_broken_parity_flagged(self):
        # Shift the call mid up by $2 so C - P violates parity by > threshold.
        call_price = black_scholes(S, K, T, R, SIGMA, 0.0, "call")["price"]
        put_price = black_scholes(S, K, T, R, SIGMA, 0.0, "put")["price"]
        chain = [
            _contract(K, "call", bid=call_price + 1.99, ask=call_price + 2.01),
            _contract(K, "put", bid=put_price - 0.01, ask=put_price + 0.01),
        ]
        errors = put_call_parity_errors(chain, S, R)
        assert len(errors) == 1
        assert errors[0]["parity_error"] == pytest.approx(2.0, abs=1e-9)
        assert errors[0]["action"] == "sell_synthetic"

    def test_missing_bid_ask_raises(self):
        # Every pair is missing a quote -> the chain is missing bid or ask.
        chain = [
            {
                "strike": K,
                "option_type": "call",
                "ask": 1.5,
                "volume": 10,
                "open_interest": 100,
                "implied_volatility": 0.3,
                "expiry": None,
                "time_to_expiry": T,
            },
            {
                "strike": K,
                "option_type": "put",
                "bid": 1.5,
                "volume": 10,
                "open_interest": 100,
                "implied_volatility": 0.3,
                "expiry": None,
                "time_to_expiry": T,
            },
        ]
        with pytest.raises(Unavailable):
            put_call_parity_errors(chain, S, R)

    def test_empty_chain_raises(self):
        with pytest.raises(Unavailable):
            put_call_parity_errors([], S, R)


# ---------------------------------------------------------------------------
# Required failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_negative_time_to_expiry_raises(self):
        with pytest.raises(Unavailable):
            black_scholes(S, K, -0.1, R, SIGMA, 0.0, "call")

    def test_zero_volatility_raises(self):
        with pytest.raises(Unavailable):
            black_scholes(S, K, T, R, 0.0, 0.0, "call")

    def test_negative_volatility_raises(self):
        with pytest.raises(Unavailable):
            black_scholes(S, K, T, R, -0.1, 0.0, "call")

    def test_monte_carlo_negative_time_raises(self):
        with pytest.raises(Unavailable):
            monte_carlo(S, K, -0.1, R, SIGMA, 0.0, "call", seed=1)

    def test_monte_carlo_zero_vol_raises(self):
        with pytest.raises(Unavailable):
            monte_carlo(S, K, T, R, 0.0, 0.0, "call", seed=1)


# ---------------------------------------------------------------------------
# Option-type validation -- an unrecognised side must raise, not silently
# price the other side. v1 typed the side as an OptionType enum; the port
# took a bare str and branched on `option_type == "call"`, so every value
# that was not exactly "call" priced a put: "Call", "CALL", "c", "put ",
# "", None, a typo. No exception, no warning, a plausible number.
# ---------------------------------------------------------------------------


_BAD_SIDES = ["Call", "CALL", "c", "put ", "", None, "typo"]


class TestOptionTypeValidation:
    def test_black_scholes_rejects_unknown_side(self):
        for bad in _BAD_SIDES:
            with pytest.raises(Unavailable):
                black_scholes(S, K, T, R, SIGMA, 0.0, bad)

    def test_black_scholes_rejects_unknown_side_even_at_expiry(self):
        # The expired branch returns intrinsic; a bad side must still raise
        # before it is reached, otherwise an expired "Call" silently prices a
        # put payoff.
        for bad in _BAD_SIDES:
            with pytest.raises(Unavailable):
                black_scholes(S, K, 0.0, R, SIGMA, 0.0, bad)

    def test_implied_volatility_rejects_unknown_side(self):
        for bad in _BAD_SIDES:
            with pytest.raises(Unavailable):
                implied_volatility(5.0, S, K, T, R, 0.0, bad)

    def test_implied_volatility_rejects_unknown_side_even_when_expired(self):
        # T <= 0 would otherwise return None before the side is read; the side
        # must be validated first so an expired option with a bad side raises
        # rather than silently yielding "no IV".
        for bad in _BAD_SIDES:
            with pytest.raises(Unavailable):
                implied_volatility(5.0, S, K, 0.0, R, 0.0, bad)

    def test_monte_carlo_rejects_unknown_side(self):
        for bad in _BAD_SIDES:
            with pytest.raises(Unavailable):
                monte_carlo(S, K, T, R, SIGMA, 0.0, bad, seed=1)

    def test_build_volatility_surface_rejects_unknown_side(self):
        strikes = np.array([100.0])
        expiries = np.array([0.5])
        grid = np.array([[5.0]])
        for bad in _BAD_SIDES:
            with pytest.raises(Unavailable):
                build_volatility_surface(S, R, 0.0, strikes, expiries, grid, bad)

    def test_put_call_ratio_rejects_chain_with_unknown_side(self):
        # A "Call" among the calls must not be counted as a put; the whole
        # chain is unanalysable.
        chain = [
            _contract(100, "call", volume=100),
            _contract(100, "Call", volume=100),
        ]
        with pytest.raises(Unavailable):
            put_call_ratio(chain)

    def test_put_call_ratio_rejects_chain_with_missing_side(self):
        chain = [
            {"strike": 100, "volume": 100},
            _contract(100, "put", volume=100),
        ]
        with pytest.raises(Unavailable):
            put_call_ratio(chain)

    def test_max_pain_rejects_chain_with_unknown_side(self):
        chain = [
            _contract(95, "call", oi=50),
            _contract(105, "Put", oi=50),
        ]
        with pytest.raises(Unavailable):
            max_pain(chain)

    def test_detect_unusual_activity_rejects_chain_with_unknown_side(self):
        chain = [
            _contract(100, "call", volume=300, oi=100),
            _contract(100, "", volume=300, oi=100),
        ]
        with pytest.raises(Unavailable):
            detect_unusual_activity(chain)

    def test_parity_errors_rejects_chain_with_unknown_side(self):
        # Previously the malformed contract was silently dropped from pairing
        # and the chain was analysed one leg short.
        call_price = black_scholes(S, K, T, R, SIGMA, 0.0, "call")["price"]
        chain = [
            _contract(K, "call", bid=call_price - 0.01, ask=call_price + 0.01),
            _contract(K, "Call", bid=call_price - 0.01, ask=call_price + 0.01),
        ]
        with pytest.raises(Unavailable):
            put_call_parity_errors(chain, S, R)

    def test_message_names_the_offending_value(self):
        # The work order requires the exception to name the value it received.
        with pytest.raises(Unavailable, match="Call"):
            black_scholes(S, K, T, R, SIGMA, 0.0, "Call")


# ---------------------------------------------------------------------------
# Strategy scanner (ported from the inline screener; chain arrives as data)
# ---------------------------------------------------------------------------


def _leg(strike, otype, *, bid=1.0, ask=2.0, iv=0.3, expiry="2025-12-19", dte=30):
    return {
        "strike": strike,
        "option_type": otype,
        "bid": bid,
        "ask": ask,
        "implied_volatility": iv,
        "expiry": expiry,
        "days_to_expiry": dte,
    }


class TestScanOptionStrategies:
    def test_empty_chain_raises(self):
        with pytest.raises(Unavailable):
            scan_option_strategies([], 100.0)

    def test_nonpositive_spot_raises(self):
        with pytest.raises(Unavailable):
            scan_option_strategies([_leg(100, "call")], 0.0)
        with pytest.raises(Unavailable):
            scan_option_strategies([_leg(100, "call")], -5.0)

    def test_covered_call_uses_the_moneyness_heuristic_verbatim(self):
        # strike 105, spot 100 -> moneyness 1.05 -> prob = 0.5 + 0.5*(1-1.05) = 0.475
        chain = [_leg(105, "call", bid=2.0)]
        out = scan_option_strategies(
            chain, 100.0, min_probability=0.0, strategy_types=("covered_call",)
        )
        assert len(out) == 1
        cc = out[0]
        assert cc["type"] == "covered_call"
        assert cc["strike"] == 105
        assert cc["premium_collected"] == 2.0
        assert cc["probability_of_profit"] == pytest.approx(0.475)
        assert cc["return_if_called"] == pytest.approx(((105 - 100) + 2.0) / 100)
        assert cc["return_if_not_called"] == pytest.approx(2.0 / 100)
        assert cc["max_risk"] == 0.0

    def test_covered_call_probability_is_inverted_relative_to_assignment_risk(self):
        # Documents the v1 defect: a further-OTM call (more likely to expire
        # worthless, so safer to write) gets a LOWER prob_of_profit under v1's
        # formula. 110-strike must score below 105-strike.
        chain = [_leg(105, "call", bid=2.0), _leg(110, "call", bid=1.0)]
        out = scan_option_strategies(
            chain, 100.0, min_probability=0.0, strategy_types=("covered_call",)
        )
        by_strike = {c["strike"]: c["probability_of_profit"] for c in out}
        assert by_strike[110] < by_strike[105]
        assert by_strike[105] == pytest.approx(0.475)
        assert by_strike[110] == pytest.approx(0.45)

    def test_covered_call_skips_in_the_money_and_non_otm_strikes(self):
        # Only the 2-10% OTM band qualifies; 101 (1%) and 112 (12%) are out.
        chain = [
            _leg(101, "call", bid=1.5),
            _leg(105, "call", bid=2.0),
            _leg(112, "call", bid=0.5),
        ]
        out = scan_option_strategies(
            chain, 100.0, min_probability=0.0, strategy_types=("covered_call",)
        )
        assert [c["strike"] for c in out] == [105]

    def test_cash_secured_put_candidate(self):
        # strike 95, spot 100 -> moneyness 0.95 -> prob = 0.5 + 0.5*0.05 = 0.525
        chain = [_leg(95, "put", bid=1.5)]
        out = scan_option_strategies(
            chain, 100.0, min_probability=0.0, strategy_types=("cash_secured_put",)
        )
        assert len(out) == 1
        put = out[0]
        assert put["type"] == "cash_secured_put"
        assert put["probability_of_profit"] == pytest.approx(0.525)
        assert put["return_on_cash"] == pytest.approx(1.5 / 95)
        assert put["max_risk"] == 95.0

    def test_cash_secured_put_filtered_by_max_risk(self):
        # strike 95 exceeds max_risk 90 -> excluded though it qualifies otherwise.
        chain = [_leg(95, "put", bid=1.5), _leg(90, "put", bid=1.0)]
        out = scan_option_strategies(
            chain,
            100.0,
            min_probability=0.0,
            max_risk=90.0,
            strategy_types=("cash_secured_put",),
        )
        assert [c["strike"] for c in out] == [90]

    def test_bull_call_spread_candidate_pnl_and_probability(self):
        # long 100 @ ask 5, short 104 @ bid 2 -> debit 3, max_profit 1, max_loss 3
        chain = [_leg(100, "call", ask=5.0), _leg(104, "call", bid=2.0)]
        out = scan_option_strategies(
            chain, 100.0, min_probability=0.0, strategy_types=("spread",)
        )
        assert len(out) == 1
        sp = out[0]
        assert sp["type"] == "bull_call_spread"
        assert sp["long_strike"] == 100
        assert sp["short_strike"] == 104
        assert sp["net_debit"] == pytest.approx(3.0)
        assert sp["max_profit"] == pytest.approx(1.0)
        assert sp["max_loss"] == pytest.approx(3.0)
        assert sp["risk_reward_ratio"] == pytest.approx(1.0 / 3.0)
        assert sp["probability_of_profit"] == pytest.approx(min(0.85, 0.4 + 0.1 * (1.0 / 3.0)))

    def test_bull_call_spread_takes_first_qualifying_short_leg(self):
        # Two short legs qualify (104 and 104.5); v1 breaks after the first.
        chain = [
            _leg(100, "call", ask=5.0),
            _leg(104, "call", bid=2.0),
            _leg(104.5, "call", bid=1.5),
        ]
        out = scan_option_strategies(
            chain, 100.0, min_probability=0.0, strategy_types=("spread",)
        )
        assert len(out) == 1
        assert out[0]["short_strike"] == 104

    def test_bull_call_spread_rejects_non_positive_max_profit(self):
        # short 103 @ bid 2: debit 3, max_profit = 0 -> rejected (needs > 0).
        chain = [_leg(100, "call", ask=5.0), _leg(103, "call", bid=2.0)]
        out = scan_option_strategies(
            chain, 100.0, min_probability=0.0, strategy_types=("spread",)
        )
        assert out == []

    def test_results_sorted_by_probability_descending(self):
        chain = [
            _leg(105, "call", bid=2.0),  # covered call, prob 0.475
            _leg(95, "put", bid=1.5),    # CSP, prob 0.525
            _leg(100, "call", ask=5.0),
            _leg(104, "call", bid=2.0),  # spread, prob 0.4 + 0.1/3
        ]
        out = scan_option_strategies(chain, 100.0, min_probability=0.0)
        probs = [c["probability_of_profit"] for c in out]
        assert probs == sorted(probs, reverse=True)
        # highest is the CSP at 0.525
        assert out[0]["type"] == "cash_secured_put"
        assert out[0]["probability_of_profit"] == pytest.approx(0.525)

    def test_short_dated_expiration_is_skipped(self):
        chain = [_leg(105, "call", bid=2.0, dte=5)]
        out = scan_option_strategies(
            chain, 100.0, min_probability=0.0, strategy_types=("covered_call",)
        )
        assert out == []

    def test_min_probability_filter_excludes_low_scoring_candidates(self):
        chain = [_leg(95, "put", bid=1.5)]  # prob 0.525
        out = scan_option_strategies(
            chain,
            100.0,
            min_probability=0.6,
            strategy_types=("cash_secured_put",),
        )
        assert out == []
