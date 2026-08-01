"""Options pricing and chain analytics as pure capabilities.

Two v1 modules were lifted into this file:

1. `app/services/options/pricing_engine.py` -- Black-Scholes-Merton, the
   Greeks, implied-volatility solving, the Monte Carlo pricer and
   `VolatilitySurface.build_surface`. The binomial tree (American options)
   was NOT lifted: the work order did not name it, and nothing in the
   required outcome touches American exercise. `OptionsFlowAnalyzer`'s
   three chain-level methods (put/call ratio, max pain, unusual activity)
   were lifted as plain functions taking a list of contract records.

2. `app/services/options_analytics.py` -- the higher-level analytics over a
   chain. Only the parity-violation scanner from `_find_arbitrage_opportunities`
   was lifted; it is the natural consumer of bid *and* ask and therefore the
   honest failure point for "chain missing bid or ask". The rest of that
   module (term-structure slope, strategy P&L, profit probability) was left
   behind -- it is out of scope for this work order.

Everything is sync: there is no IO. v1's pricing engine took a populated
`OptionContract` dataclass; the dataclass carried `risk_free_rate=0.05` and
`dividend_yield=0.0` as defaults, which is exactly the fabricated-input
pattern this codebase rejects. Those inputs are therefore required positional
arguments on every function -- a caller who omits the rate or the dividend
yield gets a TypeError, never a silent number.

Failure modes that v1 papered over with a number are now honest:

* `black_scholes` on `T < 0` raises `Unavailable` (v1 folded negative time
  into the "expired" branch and returned intrinsic). `T == 0` still returns
  intrinsic -- that is the correct limit, and the required outcome asserts
  it. `sigma <= 0` with `T > 0` raises `Unavailable`: the BSM formula is a
  0/0 limit at zero volatility and v1 returned NaN.
* `implied_volatility` returns `None` on non-convergence, below-intrinsic
  market prices, and `T <= 0`. v1 returned the last iterate (Newto) or the
  minimiser's `x` (Brent) unconditionally; both are "a number the caller did
  not earn". `None` is "no number", per the work order.
* `monte_carlo` takes `seed` as an explicit argument and never calls
  `np.random.seed` globally. v1 hardcoded `np.random.seed(42)` inside the
  pricer. Its docstring states that the output is model-derived, not
  measured.
* The chain analytics raise `Unavailable` on an empty chain and on a chain
  where no contract carries the fields the analysis needs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.stats import norm

from omni.ingest.protocol import Unavailable

_TOL = 1e-6
_IV_MAX_ITER = 100
_IV_BOUNDS = (0.001, 5.0)


def _validate_option_type(option_type: Any) -> None:
    if option_type not in ("call", "put"):
        raise Unavailable(f"unrecognised option_type: {option_type!r}")


def _check_inputs(T: float, sigma: float) -> None:
    if T < 0.0:
        raise Unavailable(f"negative time to expiry: T={T}")
    if T > 0.0 and sigma <= 0.0:
        raise Unavailable(f"volatility must be positive for live options; sigma={sigma}")


def black_scholes(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    q: float,
    option_type: str,
) -> dict[str, Any]:
    """Black-Scholes-Merton price and Greeks for a European option.

    All inputs are required -- the caller states the dividend yield `q`
    explicitly (0.0 means "no dividend", the Merton baseline, not a guess).
    `option_type` is ``"call"`` or ``"put"``. Greeks follow v1's conventions:
    theta is per calendar day (``/365``), vega and rho are per 1% change
    (``/100``).
    """
    _validate_option_type(option_type)
    _check_inputs(T, sigma)
    is_call = option_type == "call"

    if T == 0.0:
        intrinsic = (S - K) if is_call else (K - S)
        price = max(intrinsic, 0.0)
        return {
            "price": price,
            "delta": 1.0
            if (is_call and intrinsic > 0)
            else (-1.0 if (not is_call and intrinsic > 0) else 0.0),
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0,
            "rho": 0.0,
            "model": "Black-Scholes-Merton",
        }

    sqrt_T = np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    exp_qT = np.exp(-q * T)
    exp_rT = np.exp(-r * T)
    pdf_d1 = norm.pdf(d1)

    if is_call:
        price = S * exp_qT * norm.cdf(d1) - K * exp_rT * norm.cdf(d2)
    else:
        price = K * exp_rT * norm.cdf(-d2) - S * exp_qT * norm.cdf(-d1)

    delta = exp_qT * norm.cdf(d1) if is_call else -exp_qT * norm.cdf(-d1)
    gamma = exp_qT * pdf_d1 / (S * sigma * sqrt_T)

    term1 = -exp_qT * S * pdf_d1 * sigma / (2 * sqrt_T)
    if is_call:
        theta = (term1 + q * S * exp_qT * norm.cdf(d1) - r * K * exp_rT * norm.cdf(d2)) / 365
    else:
        theta = (term1 - q * S * exp_qT * norm.cdf(-d1) + r * K * exp_rT * norm.cdf(-d2)) / 365

    vega = S * exp_qT * pdf_d1 * sqrt_T / 100
    rho = (K * T * exp_rT * norm.cdf(d2) if is_call else -K * T * exp_rT * norm.cdf(-d2)) / 100

    return {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
        "model": "Black-Scholes-Merton",
    }


def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    option_type: str,
) -> float | None:
    """Implied volatility from a market price, or ``None``.

    Uses Newton-Raphson seeded at 0.2, falling back to a bounded Brent
    minimise when vega collapses. Returns ``None`` -- not the last iterate,
    not the input guess -- when:

    * ``T <= 0`` (IV is undefined at/after expiry),
    * the market price is below intrinsic (no positive vol can produce it),
    * neither solver reaches ``_TOL`` on the residual.
    """
    _validate_option_type(option_type)
    if T <= 0.0:
        return None
    is_call = option_type == "call"
    if is_call:
        intrinsic = max(S * np.exp(-q * T) - K * np.exp(-r * T), 0.0)
    else:
        intrinsic = max(K * np.exp(-r * T) - S * np.exp(-q * T), 0.0)
    if market_price < intrinsic - _TOL:
        return None

    def price_at(vol: float) -> tuple[float, float]:
        res = black_scholes(S, K, T, r, vol, q, option_type)
        return res["price"], res["vega"] * 100

    vol = 0.2
    for _ in range(_IV_MAX_ITER):
        model_price, vega_full = price_at(vol)
        diff = model_price - market_price
        if abs(diff) < _TOL:
            return vol
        if abs(vega_full) < 1e-10:
            break
        vol = vol - diff / vega_full
        vol = max(_IV_BOUNDS[0], min(vol, _IV_BOUNDS[1]))

    def objective(vol: float) -> float:
        return (black_scholes(S, K, T, r, vol, q, option_type)["price"] - market_price) ** 2

    result = minimize_scalar(objective, bounds=_IV_BOUNDS, method="bounded")
    # objective returns the SQUARED residual, so gate on _TOL**2: this makes the
    # Brent fallback accept on the same LINEAR residual (sqrt(_TOL**2) == _TOL)
    # that Newton uses at :184. The old `abs(result.fun) < _TOL` gate accepted a
    # linear residual of sqrt(_TOL) = 1e-3 -- 1000x looser -- and returned the
    # 5.0 bound for prices no in-bound vol can produce.
    if result.fun < _TOL**2:
        return float(result.x)
    return None


def monte_carlo(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    q: float,
    option_type: str,
    *,
    simulations: int = 10000,
    time_steps: int = 252,
    seed: int | None = None,
) -> dict[str, Any]:
    """Monte Carlo pricer under geometric Brownian motion.

    The output is **model-derived, not measured**: it is the discounted
    expectation of the terminal payoff under the risk-neutral GBM with the
    supplied `(r, sigma, q)`, estimated by Monte Carlo over `simulations`
    paths of `time_steps` steps. The estimate carries sampling noise; the
    returned ``std_error`` and 95% confidence interval quantify it.

    ``seed`` is explicit. v1 hardcoded ``np.random.seed(42)`` inside the
    pricer, polluting global RNG state; passing ``seed=None`` here leaves
    numpy's global state untouched, and passing an int makes the draw
    reproducible via an isolated generator.
    """
    _validate_option_type(option_type)
    _check_inputs(T, sigma)
    if T == 0.0:
        payoff = max((S - K) if option_type == "call" else (K - S), 0.0)
        return {
            "price": payoff,
            "std_error": 0.0,
            "confidence_interval_lower": payoff,
            "confidence_interval_upper": payoff,
            "model": "Monte Carlo",
            "simulations": simulations,
            "time_steps": time_steps,
        }

    dt = T / time_steps
    if seed is None:
        z = np.random.standard_normal((time_steps, simulations))
    else:
        rng = np.random.default_rng(seed)
        z = rng.standard_normal((time_steps, simulations))

    price_paths = np.zeros((time_steps + 1, simulations))
    price_paths[0] = S
    drift = (r - q - 0.5 * sigma**2) * dt
    diffusion = sigma * np.sqrt(dt)
    for t in range(1, time_steps + 1):
        price_paths[t] = price_paths[t - 1] * np.exp(drift + diffusion * z[t - 1])

    if option_type == "call":
        payoffs = np.maximum(price_paths[-1] - K, 0.0)
    else:
        payoffs = np.maximum(K - price_paths[-1], 0.0)

    discounted = payoffs * np.exp(-r * T)
    price = float(np.mean(discounted))
    std_error = float(np.std(discounted) / np.sqrt(simulations))
    return {
        "price": price,
        "std_error": std_error,
        "confidence_interval_lower": price - 1.96 * std_error,
        "confidence_interval_upper": price + 1.96 * std_error,
        "model": "Monte Carlo",
        "simulations": simulations,
        "time_steps": time_steps,
    }


def build_volatility_surface(
    spot: float,
    r: float,
    q: float,
    strikes: Sequence[float],
    expiries: Sequence[float],
    market_prices: np.ndarray,
    option_type: str = "call",
) -> np.ndarray:
    """Implied-vol grid for ``market_prices[i, j]`` at ``strikes[i]`` x
    ``expiries[j]``.

    Cells where the market price is non-positive, or where
    :func:`implied_volatility` cannot converge, are ``NaN``. v1 logged and
    swallowed the failure; NaN propagates honestly here.
    """
    _validate_option_type(option_type)
    market_prices = np.asarray(market_prices, dtype=float)
    implied_vols = np.full_like(market_prices, np.nan, dtype=float)
    for i, strike in enumerate(strikes):
        for j, expiry in enumerate(expiries):
            mp = market_prices[i, j]
            if mp <= 0.0:
                continue
            iv = implied_volatility(mp, spot, strike, expiry, r, q, option_type)
            if iv is not None:
                implied_vols[i, j] = iv
    return implied_vols


# ---------------------------------------------------------------------------
# Chain-level analytics (ported from OptionsFlowAnalyzer and the parity
# branch of OptionsAnalyticsEngine._find_arbitrage_opportunities). A contract
# is a plain dict shaped like one leg of v1's OptionContract:
#   {strike, option_type, bid, ask, volume, open_interest,
#    implied_volatility, expiry?}
# -------------------------------------------------------------------(parity)--


def _require(contracts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    if not contracts:
        raise Unavailable("empty options chain; no contracts to analyse")
    for c in contracts:
        _validate_option_type(c.get("option_type"))
    return list(contracts)


def put_call_ratio(contracts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Volume put/call ratio over a chain.

    Raises ``Unavailable`` on an empty chain or when no contract reports any
    volume. v1 returned ``put_volume / max(call_volume, 1)``, which silently
    reported 0 on a totally dry chain.
    """
    contracts = _require(contracts)
    call_volume = sum(c.get("volume", 0) for c in contracts if c.get("option_type") == "call")
    put_volume = sum(c.get("volume", 0) for c in contracts if c.get("option_type") == "put")
    total = call_volume + put_volume
    if total == 0:
        raise Unavailable("no volume on any contract; put/call ratio undefined")
    ratio = put_volume / call_volume if call_volume > 0 else float("inf")
    sentiment = _pcr_sentiment(ratio)
    return {
        "ratio": ratio,
        "put_volume": put_volume,
        "call_volume": call_volume,
        "sentiment": sentiment,
    }


def _pcr_sentiment(ratio: float) -> str:
    if ratio == float("inf"):
        return "EXTREMELY_BEARISH"
    if ratio < 0.5:
        return "VERY_BULLISH"
    if ratio < 0.7:
        return "BULLISH"
    if ratio < 0.9:
        return "NEUTRAL_BULLISH"
    if ratio < 1.1:
        return "NEUTRAL"
    if ratio < 1.3:
        return "NEUTRAL_BEARISH"
    if ratio < 1.5:
        return "BEARISH"
    return "VERY_BEARISH"


def max_pain(contracts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Max-pain strike -- the strike at which total writer payoff is least.

    Raises ``Unavailable`` on an empty chain or when no contract carries open
    interest (the pain sum is identically zero and every strike ties).
    """
    contracts = _require(contracts)
    strikes = sorted({c["strike"] for c in contracts})
    pain_by_strike: dict[float, float] = {}
    for candidate in strikes:
        pain = 0.0
        for c in contracts:
            k = c["strike"]
            oi = c.get("open_interest", 0)
            if c.get("option_type") == "call" and candidate > k:
                pain += (candidate - k) * oi
            elif c.get("option_type") == "put" and candidate < k:
                pain += (k - candidate) * oi
        pain_by_strike[candidate] = pain

    # Max pain is undefined only when there is no open interest anywhere. The
    # previous guard summed the PAIN values and raised when that was 0.0 -- but a
    # single-strike chain with real OI has zero cross-strike pain at its only
    # candidate, so the guard refused a well-defined answer (and the message
    # "no open interest" was false). Summing integer OI is also exact, avoiding
    # the float-noise-on-pain-sum defect (Q1 F6).
    total_oi = sum(c.get("open_interest", 0) for c in contracts)
    if total_oi == 0:
        raise Unavailable("no open interest on any contract; max pain undefined")

    max_pain_strike = min(pain_by_strike, key=pain_by_strike.get)
    return {
        "strike": max_pain_strike,
        "total_pain": pain_by_strike[max_pain_strike],
        "pain_by_strike": pain_by_strike,
    }


def detect_unusual_activity(contracts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag contracts whose volume is large relative to open interest.

    Raises ``Unavailable`` on an empty chain. A contract with no open
    interest is skipped (the ratio is undefined), matching v1's guard.
    """
    contracts = _require(contracts)
    alerts: list[dict[str, Any]] = []
    for c in contracts:
        oi = c.get("open_interest", 0)
        volume = c.get("volume", 0)
        if oi > 0:
            ratio = volume / oi
            if ratio > 2.0:
                alerts.append(
                    {
                        "strike": c["strike"],
                        "option_type": c.get("option_type"),
                        "type": "high_volume",
                        "volume": volume,
                        "open_interest": oi,
                        "vol_oi_ratio": ratio,
                        "implied_volatility": c.get("implied_volatility"),
                    }
                )
    return alerts


def put_call_parity_errors(
    contracts: Sequence[dict[str, Any]],
    underlying_price: float,
    risk_free_rate: float,
    *,
    time_to_expiry: float | None = None,
    threshold: float = 0.10,
) -> list[dict[str, Any]]:
    """Put-call parity violations across paired call/put contracts.

    For each ``(strike, expiry)`` with both a call and a put, compares
    ``C - P`` (from quoted mids) against ``S - K*exp(-rT)``. Pairs missing
    bid or ask on either side are skipped. Raises ``Unavailable`` when no
    pair carries a full quote set -- the chain is missing bid or ask for
    every paired strike, which is the work order's named failure path.
    """
    contracts = _require(contracts)

    if time_to_expiry is None:

        def ttf(c: dict[str, Any]) -> float | None:
            return c.get("time_to_expiry")
    else:

        def ttf(c: dict[str, Any]) -> float | None:
            return time_to_expiry

    pairs: dict[tuple[float, Any], dict[str, dict[str, Any]]] = {}
    for c in contracts:
        key = (c["strike"], c.get("expiry"))
        pairs.setdefault(key, {})
        side = c.get("option_type")
        if side in ("call", "put"):
            pairs[key][side] = c

    evaluated = 0
    errors: list[dict[str, Any]] = []
    for (strike, expiry), sides in pairs.items():
        call = sides.get("call")
        put = sides.get("put")
        if call is None or put is None:
            continue
        if not all(call.get(k) is not None for k in ("bid", "ask")):
            continue
        if not all(put.get(k) is not None for k in ("bid", "ask")):
            continue
        T = ttf(call)
        if T is None or T <= 0:
            continue
        evaluated += 1
        call_mid = (call["bid"] + call["ask"]) / 2
        put_mid = (put["bid"] + put["ask"]) / 2
        theoretical = underlying_price - strike * np.exp(-risk_free_rate * T)
        actual = call_mid - put_mid
        parity_error = abs(actual - theoretical)
        if parity_error > threshold:
            errors.append(
                {
                    "strike": strike,
                    "expiry": expiry,
                    "call_price": call_mid,
                    "put_price": put_mid,
                    "theoretical_diff": theoretical,
                    "actual_diff": actual,
                    "parity_error": parity_error,
                    "action": "buy_synthetic" if actual < theoretical else "sell_synthetic",
                }
            )

    if evaluated == 0:
        raise Unavailable(
            "no call/put pair with complete bid/ask and positive time to expiry; "
            "chain is missing bid or ask"
        )
    return errors


# ---------------------------------------------------------------------------
# Strategy scanner (ported from the inline screener at
# app/api/v1/endpoints/options.py:855-1005). The v1 handler fetched each chain
# via yfinance -- a prohibited provider per the credential catalog (W-19 /
# RECENSUS) -- so this port takes the chain as data and performs no fetch. The
# caller owns ingestion; the screener owns the strategy-selection arithmetic.
#
# A contract is the same plain dict the chain analytics above consume:
#   {strike, option_type, bid, ask, implied_volatility, expiry?, days_to_expiry?}
# Legs are grouped by `expiry` so a spread can pair a long and a short call
# struck in the same maturity. `days_to_expiry` (when present) gates the
# short-dated skip; an expiry without it is still screened.
#
# `probability_of_profit` is a moneyness heuristic, not a priced model: v1
# labelled it "simplified" / "estimate" and the census accepted it as a
# modelled estimate. The heuristics are ported bit-for-bit, including one
# defect -- see `_covered_call_candidates`.
# ---------------------------------------------------------------------------

_CC_MONEYNESS = (1.02, 1.10)      # covered call: 2-10% OTM calls
_CSP_MONEYNESS = (0.90, 0.98)     # cash-secured put: 2-10% OTM puts
_BCS_LONG_MONEYNESS = (0.98, 1.02)  # bull-call long leg: near ATM
_MIN_DAYS_TO_EXPIRY = 7           # v1 skips expirations closer than this


def _group_legs_by_expiry(
    contracts: Sequence[dict[str, Any]],
) -> dict[Any, dict[str, Any]]:
    """Group flat contracts into ``{expiry: {days_to_expiry, legs}}`` where
    ``legs`` maps ``strike -> {"call"|"put": contract}``."""
    groups: dict[Any, dict[str, Any]] = {}
    for c in contracts:
        key = c.get("expiry")
        group = groups.setdefault(
            key, {"days_to_expiry": c.get("days_to_expiry"), "legs": {}}
        )
        strike = c["strike"]
        legs = group["legs"].setdefault(strike, {})
        side = c.get("option_type")
        if side in ("call", "put") and side not in legs:
            legs[side] = c
    return groups


def _covered_call_candidates(
    legs: dict[float, dict[str, dict[str, Any]]],
    spot: float,
    expiry: Any,
    days_to_expiry: Any,
    min_probability: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for strike, sides in legs.items():
        call = sides.get("call")
        if call is None:
            continue
        moneyness = strike / spot
        if not (_CC_MONEYNESS[0] <= moneyness <= _CC_MONEYNESS[1]):
            continue
        premium = call.get("bid", 0) or 0
        if premium <= 0:
            continue
        return_if_called = ((strike - spot) + premium) / spot
        return_if_not_called = premium / spot
        # v1 heuristic, ported verbatim. NOTE: for an OTM covered call
        # (moneyness > 1) this evaluates to < 0.5, i.e. it reports a LOWER
        # probability of profit the FURTHER OTM the call -- the opposite of
        # the economic truth (a further-OTM call is less likely to be
        # assigned, so the writer is more likely to keep the premium). v1's
        # own comment ("Higher moneyness = higher prob of profit") contradicts
        # its formula. Ported bit-for-bit per PORTING.md; the defect is
        # recorded, not "fixed".
        prob_profit = 0.5 + 0.5 * (1 - moneyness)
        if prob_profit >= min_probability:
            out.append(
                {
                    "type": "covered_call",
                    "strike": strike,
                    "expiry": expiry,
                    "days_to_expiry": days_to_expiry,
                    "premium_collected": premium,
                    "return_if_called": return_if_called,
                    "return_if_not_called": return_if_not_called,
                    "probability_of_profit": prob_profit,
                    "max_risk": 0.0,
                    "implied_volatility": call.get("implied_volatility"),
                }
            )
    return out


def _cash_secured_put_candidates(
    legs: dict[float, dict[str, dict[str, Any]]],
    spot: float,
    expiry: Any,
    days_to_expiry: Any,
    min_probability: float,
    max_risk: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for strike, sides in legs.items():
        put = sides.get("put")
        if put is None:
            continue
        moneyness = strike / spot
        if not (_CSP_MONEYNESS[0] <= moneyness <= _CSP_MONEYNESS[1]):
            continue
        premium = put.get("bid", 0) or 0
        if premium <= 0:
            continue
        return_on_cash = premium / strike
        prob_profit = 0.5 + 0.5 * (1 - moneyness)
        if prob_profit >= min_probability and strike <= max_risk:
            out.append(
                {
                    "type": "cash_secured_put",
                    "strike": strike,
                    "expiry": expiry,
                    "days_to_expiry": days_to_expiry,
                    "premium_collected": premium,
                    "return_on_cash": return_on_cash,
                    "probability_of_profit": prob_profit,
                    "max_risk": float(strike),
                    "implied_volatility": put.get("implied_volatility"),
                }
            )
    return out


def _bull_call_spread_candidates(
    legs: dict[float, dict[str, dict[str, Any]]],
    spot: float,
    expiry: Any,
    days_to_expiry: Any,
    min_probability: float,
    max_risk: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    strikes = sorted(legs)
    for strike in strikes:
        long_call = legs[strike].get("call")
        if long_call is None:
            continue
        long_moneyness = strike / spot
        if not (_BCS_LONG_MONEYNESS[0] <= long_moneyness <= _BCS_LONG_MONEYNESS[1]):
            continue
        long_ask = long_call.get("ask", 0) or 0
        for otm_strike in strikes:
            if not (strike < otm_strike <= strike * 1.05):
                continue
            short_call = legs[otm_strike].get("call")
            if short_call is None:
                continue
            short_bid = short_call.get("bid", 0) or 0
            if not (long_ask > short_bid > 0):
                continue
            net_debit = long_ask - short_bid
            max_profit = (otm_strike - strike) - net_debit
            max_loss = net_debit
            if not (max_profit > 0 and max_loss <= max_risk):
                continue
            risk_reward = max_profit / max_loss if max_loss > 0 else 0.0
            prob_profit = min(0.85, 0.4 + 0.1 * risk_reward)
            if prob_profit >= min_probability:
                out.append(
                    {
                        "type": "bull_call_spread",
                        "long_strike": strike,
                        "short_strike": otm_strike,
                        "expiry": expiry,
                        "days_to_expiry": days_to_expiry,
                        "net_debit": net_debit,
                        "max_profit": max_profit,
                        "max_loss": max_loss,
                        "probability_of_profit": prob_profit,
                        "risk_reward_ratio": risk_reward,
                    }
                )
                break  # v1 takes the first qualifying short leg per long leg
    return out


def scan_option_strategies(
    contracts: Sequence[dict[str, Any]],
    spot: float,
    *,
    min_probability: float = 0.6,
    max_risk: float = 1000.0,
    strategy_types: Sequence[str] = ("covered_call", "cash_secured_put", "spread"),
) -> list[dict[str, Any]]:
    """Screen an options chain for income / spread strategy candidates.

    Ported from v1 ``GET /options/strategies/scanner``. The chain arrives as
    data (no yfinance); `spot` is required -- moneyness is undefined without
    it. Candidates are returned sorted by ``probability_of_profit`` descending.

    `probability_of_profit` is a moneyness heuristic ported verbatim from v1,
    not a priced probability; see the module notes above and the per-strategy
    docstrings. Each candidate carries the fields its v1 counterpart did.

    Raises ``Unavailable`` on an empty chain or a non-positive spot. An
    expiration whose ``days_to_expiry`` is present and below
    ``_MIN_DAYS_TO_EXPIRY`` is skipped, matching v1.
    """
    contracts = _require(contracts)
    if spot <= 0:
        raise Unavailable(f"non-positive spot {spot}; moneyness is undefined")

    types = set(strategy_types)
    groups = _group_legs_by_expiry(contracts)

    found: list[dict[str, Any]] = []
    for expiry, group in groups.items():
        days = group["days_to_expiry"]
        if days is not None and days < _MIN_DAYS_TO_EXPIRY:
            continue
        legs = group["legs"]
        if "covered_call" in types:
            found.extend(
                _covered_call_candidates(legs, spot, expiry, days, min_probability)
            )
        if "cash_secured_put" in types:
            found.extend(
                _cash_secured_put_candidates(
                    legs, spot, expiry, days, min_probability, max_risk
                )
            )
        if "spread" in types:
            found.extend(
                _bull_call_spread_candidates(
                    legs, spot, expiry, days, min_probability, max_risk
                )
            )

    found.sort(key=lambda x: x["probability_of_profit"], reverse=True)
    return found
