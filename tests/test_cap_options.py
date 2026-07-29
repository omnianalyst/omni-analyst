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
        # Seeded MC must bracket the closed-form BSM price inside its 95% CI.
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
        assert mc["confidence_interval_lower"] <= bs + 1e-9
        assert bs - 1e-9 <= mc["confidence_interval_upper"]
        # And the point estimate should be close for a healthy sample count.
        assert mc["price"] == pytest.approx(bs, abs=0.15)

    def test_put_within_bs_confidence(self):
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
        assert mc["price"] == pytest.approx(bs, abs=0.15)

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
