"""Measure present spot/perp basis exposure on the live carry book.

WHY THIS EXISTS (Finding 15):
    The carry loop prices both legs off ONE series. That is what guarantees the
    two legs are equal-sized and delta-neutral, and it is also why the loop
    structurally cannot see the spot/perp basis. Finding 15 measured the gap on
    BTC/Binance over 750 aligned days -- mean +1.76 bps, sd 9.70, max |180| bps
    -- and MONEY_PLAN calls that "bounded and acceptable at M7 micro size with
    the risk stated; not acceptable at size".

    This probe answers the question that gates sizing up: what is the basis
    doing on the six names actually held or holdable, on the venue that holds
    them, and how bad does it get against six weeks of carry.

WHAT IT MEASURES
    For BTC, ETH, SOL, HYPE, PENGU, PURR on Hyperliquid:
      1. Daily spot and perp closes, fetched at an EXPLICIT depth.
      2. basis = (perp - spot) / spot, in bps, aligned by UTC date.
      3. Per asset: n, window, mean, sd, max |basis|, p99 |basis|, p1/p99 signed.
      4. The live basis right now, from order-book mids on both legs.
      5. The distribution of the SIX-WEEK CHANGE in basis, which is what a
         42-day hold is actually exposed to. The level is not the exposure: a
         pair opened at +50 bps and closed at +50 bps paid nothing for it. The
         change is the exposure.
      6. What an adverse basis move costs a $10k pair against what that pair
         earns over the same six weeks.

    The book holds ETH and SOL. Those are reported first.

THE DEPTH TRAP (Finding 5, and again in the Hyperliquid listing study):
    ccxt's default `limit` returns the most recent 500 bars. BTC perp returns
    2,186 at limit=5000. A measurement taken at the default is a fact about an
    argument, not about the market. Every fetch here passes limit explicitly and
    the realised bar count is printed so a silent truncation is visible.

SIGN CONVENTION
    Long spot, short perp. P&L over a hold is
        -Q * (b1*S1 - b0*S0) / 10000
    so a RISE in basis (the perp richening against spot) is the adverse
    direction. "Adverse" below always means a positive change in basis.

WHAT IT DOES NOT DO
    It does not implement the M10 instrument dimension. It measures. It also
    does not claim the mark-to-market is a realised loss: a perpetual never
    expires, so an adverse basis is only crystallised by an exit -- voluntary,
    or forced by margin. That is stated in the output rather than assumed away.

Run:
    python ops/basis_exposure.py
    python ops/basis_exposure.py --notional 10000 --hold-days 42
    python ops/basis_exposure.py --adverse-bps 180 --limit 5000
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import numpy as np

CARRY_UNIVERSE = ["BTC", "ETH", "SOL", "HYPE", "PENGU", "PURR"]
HELD = ["ETH", "SOL"]

QUOTE = "USDC"

# Both are measured figures carried in from this project's own record, not
# assumptions invented here. They are printed with their provenance and are
# overridable, because a number quoted for a configuration nobody measured is
# the failure mode this codebase keeps rediscovering.
BINANCE_NET_PCT_YR = 7.80  # MONEY_PLAN: capped basket, net, in sample 2024-26
HYPERLIQUID_NET_PCT_YR = 11.50  # OMNI_ANALYST section 3: six-name book, net, t +19.4

# One degeneracy idiom for the whole module. The tolerance is on the standard
# deviation, not the variance -- a variance tolerance is scale-squared and does
# not mean the same thing across series -- and it is expressed RELATIVE to the
# series' own magnitude, so it means the same thing for a basis in bps and for a
# return expressed as a fraction. np.std of a constant series returns ~1e-15
# rather than 0.0, so an equality test never fires and the caller divides noise
# by noise.
DEGENERATE_REL_TOL = 1e-9

MIN_ALIGNED_DAYS = 30

# The top decile of sessions by the perp's own absolute daily move. Finding 15
# asserts the basis "lands exactly when volatility does"; this is the split that
# checks it.
VOLATILE_QUANTILE = 90.0


class Unfillable(Exception):
    """A named reason a measurement could not be taken. Never a substituted value."""


def degenerate(series: np.ndarray) -> bool:
    """True when a series has no usable spread and nothing may be divided by it.

    Fewer than two points, any non-finite element, every element exactly zero,
    or a standard deviation at or below `DEGENERATE_REL_TOL` of the series' own
    largest magnitude. The comparison is sd against magnitude -- same units,
    same scale -- so one constant answers for basis in bps and for a return as a
    fraction, which is what stops two functions in this file disagreeing about
    what counts as constant.
    """
    if series.size < 2:
        return True
    if not np.all(np.isfinite(series)):
        return True
    scale = float(np.max(np.abs(series)))
    if scale <= 0.0:
        return True
    return float(np.std(series, ddof=1)) <= DEGENERATE_REL_TOL * scale


def resolve_pair(markets: dict, base: str, quote: str = QUOTE) -> tuple[str, str]:
    """Find the spot and perpetual symbol for one base asset.

    Both must exist against the same quote. A base with only one leg cannot be
    a carry pair and is refused by name rather than half-measured.
    """
    spot = [
        s
        for s, m in markets.items()
        if m.get("base") == base and m.get("quote") == quote and m.get("spot")
    ]
    perp = [
        s
        for s, m in markets.items()
        if m.get("base") == base and m.get("quote") == quote and m.get("swap")
    ]
    if not spot:
        raise Unfillable(f"no {base}/{quote} spot market on this venue")
    if not perp:
        raise Unfillable(f"no {base}/{quote} perpetual market on this venue")
    if len(spot) > 1:
        raise Unfillable(f"{len(spot)} {base}/{quote} spot markets, ambiguous: {sorted(spot)}")
    if len(perp) > 1:
        raise Unfillable(f"{len(perp)} {base}/{quote} perp markets, ambiguous: {sorted(perp)}")
    return spot[0], perp[0]


def bars_by_date(bars: Sequence[Sequence[float]]) -> dict[date, tuple[float, float]]:
    """Map UTC date -> (close, base volume) from a ccxt OHLCV page.

    ccxt stamps a bar with its OPEN (Finding 6), so the date is the session the
    bar covers and the close is that session's last print. A repeated timestamp
    keeps the later row.
    """
    out: dict[date, tuple[float, float]] = {}
    for bar in bars:
        if len(bar) < 6:
            continue
        ts, close, volume = bar[0], bar[4], bar[5]
        if ts is None or close is None or volume is None:
            continue
        out[datetime.fromtimestamp(ts / 1000, tz=UTC).date()] = (float(close), float(volume))
    return out


def align_basis(
    spot: dict[date, tuple[float, float]],
    perp: dict[date, tuple[float, float]],
    *,
    min_session_notional: float,
) -> dict:
    """Basis in bps on every date both legs priced, and what was excluded and why.

    Two exclusions, both named and both reported rather than applied silently:

    `not_a_price` -- a non-finite close, or a spot close at or below zero. There
    is no defensible basis for a day whose denominator is not a price.

    `no_market` -- a session in which one leg traded less than
    `min_session_notional`. Hyperliquid's BTC spot book (`@142`) opened with
    eleven sessions closing at 6,969,696 and 7,979,573 on volumes between zero
    and five hundredths of a cent. Those bars are a resting joke quote on an
    empty book, and taking them at face value reports a 9,880 bps basis that no
    one could have crossed in either direction. The floor is a statement about
    liquidity, not about price: it is applied on volume x the PERP close, so a
    corrupt spot print cannot decide its own admissibility.

    Excluded sessions are returned in full with their raw basis so the operator
    sees exactly what was removed and can re-run at another floor.
    """
    dates: list[date] = []
    values: list[float] = []
    excluded: list[dict] = []

    for d in sorted(set(spot) & set(perp)):
        s, s_vol = spot[d]
        p, p_vol = perp[d]

        if not np.isfinite(s) or not np.isfinite(p) or s <= 0.0:
            excluded.append({"date": d, "reason": "not_a_price", "spot": s, "perp": p, "basis_bps": None})
            continue

        b = (p - s) / s * 10000.0
        if not np.isfinite(b):
            excluded.append({"date": d, "reason": "not_a_price", "spot": s, "perp": p, "basis_bps": None})
            continue

        spot_usd, perp_usd = s_vol * p, p_vol * p
        if min(spot_usd, perp_usd) < min_session_notional:
            excluded.append(
                {
                    "date": d,
                    "reason": "no_market",
                    "spot": s,
                    "perp": p,
                    "basis_bps": b,
                    "spot_usd": spot_usd,
                    "perp_usd": perp_usd,
                }
            )
            continue

        dates.append(d)
        values.append(b)

    return {
        "dates": dates,
        "basis_bps": np.asarray(values, dtype=float),
        "excluded": excluded,
    }


def describe(basis_bps: np.ndarray) -> dict:
    """Level statistics. Refuses rather than reporting a percentile off a handful of days."""
    n = int(basis_bps.size)
    if n < MIN_ALIGNED_DAYS:
        raise Unfillable(
            f"{n} aligned days, need >= {MIN_ALIGNED_DAYS}; a percentile off this "
            f"many points is a number without a distribution behind it"
        )

    abs_basis = np.abs(basis_bps)
    sd = float(np.std(basis_bps, ddof=1))
    max_abs = float(abs_basis.max())

    if degenerate(basis_bps):
        max_in_sd = None
        sd_reason = "basis has no usable spread; max/sd would divide noise by noise"
    else:
        max_in_sd = max_abs / sd
        sd_reason = None

    return {
        "n": n,
        "mean_bps": float(basis_bps.mean()),
        "sd_bps": sd,
        "max_abs_bps": max_abs,
        "p99_abs_bps": float(np.percentile(abs_basis, 99)),
        "p99_bps": float(np.percentile(basis_bps, 99)),
        "p1_bps": float(np.percentile(basis_bps, 1)),
        "min_bps": float(basis_bps.min()),
        "max_bps": float(basis_bps.max()),
        "max_in_sd": max_in_sd,
        "max_in_sd_reason": sd_reason,
    }


def hold_changes(
    dates: Sequence[date], basis_bps: np.ndarray, hold_days: int
) -> tuple[np.ndarray, int]:
    """Change in basis over a hold, matched on the CALENDAR date, not the row offset.

    Offsetting by `hold_days` rows assumes the series has no gaps. It does have
    gaps -- a missing session on either leg drops the day -- and an offset walk
    would silently compare pairs of dates further apart than the hold, reporting
    a wider distribution as if it were the six-week one.

    Returns the changes and the count of entry dates whose exit date was absent.
    """
    lookup = {d: float(v) for d, v in zip(dates, basis_bps, strict=True)}
    changes: list[float] = []
    unmatched = 0
    for d in dates:
        exit_date = d + timedelta(days=hold_days)
        exit_basis = lookup.get(exit_date)
        if exit_basis is None:
            unmatched += 1
            continue
        changes.append(exit_basis - lookup[d])
    return np.asarray(changes, dtype=float), unmatched


def describe_changes(changes: np.ndarray) -> dict:
    """Statistics on the six-week basis change. Adverse is a POSITIVE change."""
    n = int(changes.size)
    if n < MIN_ALIGNED_DAYS:
        raise Unfillable(
            f"{n} matched entry/exit pairs, need >= {MIN_ALIGNED_DAYS}; the "
            f"history does not cover enough non-degenerate holds"
        )
    abs_changes = np.abs(changes)
    return {
        "n": n,
        "mean_bps": float(changes.mean()),
        "sd_bps": float(np.std(changes, ddof=1)),
        "worst_adverse_bps": float(changes.max()),
        "best_favourable_bps": float(changes.min()),
        "p99_adverse_bps": float(np.percentile(changes, 99)),
        "p99_abs_bps": float(np.percentile(abs_changes, 99)),
        "max_abs_bps": float(abs_changes.max()),
    }


def vol_link(
    dates: Sequence[date],
    basis_bps: np.ndarray,
    perp: dict[date, tuple[float, float]],
) -> dict:
    """Does the basis widen on the days the perp moves?

    Finding 15 states the 180 bps tail "lands exactly when volatility does".
    That is the load-bearing half of the risk claim -- a basis that blows out
    independently of price is a nuisance, one that blows out on the same days
    the perp gaps is the thing that liquidates a leg -- and it is checked here
    rather than repeated.

    Two readings, because a correlation alone is easy to over-read: the Pearson
    correlation of |basis| against the perp's |daily return|, and a straight
    comparison of mean and max |basis| on the top-decile volatility sessions
    against the rest.
    """
    prev = {d: perp[d - timedelta(days=1)][0] for d in dates if d - timedelta(days=1) in perp}

    paired = [
        (abs(float(b)), abs(perp[d][0] / prev[d] - 1.0))
        for d, b in zip(dates, basis_bps, strict=True)
        if d in prev and prev[d] > 0.0 and np.isfinite(perp[d][0])
    ]
    if len(paired) < MIN_ALIGNED_DAYS:
        raise Unfillable(
            f"{len(paired)} sessions with a prior close, need >= {MIN_ALIGNED_DAYS}"
        )

    abs_basis = np.array([p[0] for p in paired], dtype=float)
    abs_return = np.array([p[1] for p in paired], dtype=float)

    if degenerate(abs_basis) or degenerate(abs_return):
        raise Unfillable(
            "|basis| or |return| has no usable spread; a correlation over it "
            "would be a ratio of rounding error"
        )

    cut = float(np.percentile(abs_return, VOLATILE_QUANTILE))
    volatile = abs_basis[abs_return >= cut]
    quiet = abs_basis[abs_return < cut]
    if volatile.size == 0 or quiet.size == 0:
        raise Unfillable("volatility split leaves one side empty")

    return {
        "n": len(paired),
        "pearson": float(np.corrcoef(abs_basis, abs_return)[0, 1]),
        "cut_return_pct": cut * 100.0,
        "volatile_n": int(volatile.size),
        "volatile_mean_abs_bps": float(volatile.mean()),
        "volatile_max_abs_bps": float(volatile.max()),
        "quiet_mean_abs_bps": float(quiet.mean()),
        "quiet_max_abs_bps": float(quiet.max()),
    }


def pair_economics(
    *, notional: float, carry_pct_yr: float, hold_days: int, adverse_bps: float
) -> dict:
    """What a pair earns over the hold against what an adverse basis move costs it.

    `notional` is the size of ONE leg -- a $10k pair is $10k long spot against
    $10k short perp. Carry accrues on the perp notional; the basis move applies
    to the same notional, so the two are directly comparable in dollars.
    """
    if notional <= 0.0:
        raise Unfillable("notional must be positive; a zero-size pair earns and risks nothing")
    if hold_days <= 0:
        raise Unfillable("hold_days must be positive")

    earned = notional * (carry_pct_yr / 100.0) * (hold_days / 365.0)
    cost = notional * (adverse_bps / 10000.0)
    breakeven_bps = (carry_pct_yr / 100.0) * (hold_days / 365.0) * 10000.0
    return {
        "notional": notional,
        "carry_pct_yr": carry_pct_yr,
        "hold_days": hold_days,
        "adverse_bps": adverse_bps,
        "earned_usd": earned,
        "adverse_cost_usd": cost,
        "cost_over_earned": cost / earned if earned > 0 else None,
        "breakeven_bps": breakeven_bps,
    }


def _fmt(value: float | None, spec: str) -> str:
    return "n/a" if value is None else format(value, spec)


def _live_basis(exchange, spot_symbol: str, perp_symbol: str) -> dict:
    """Basis right now, from the mid of each leg's order book.

    The spot ticker on Hyperliquid carries no bid/ask, so the book is the only
    contemporaneous quote. A missing side is unfillable, not a fallback to last
    trade: last trade on a thin spot book can be hours stale and would print a
    basis that is a fact about staleness.
    """
    mids = {}
    for label, symbol in (("spot", spot_symbol), ("perp", perp_symbol)):
        book = exchange.fetch_order_book(symbol, limit=5)
        bids, asks = book.get("bids") or [], book.get("asks") or []
        if not bids or not asks:
            raise Unfillable(f"{symbol} order book has no {'bid' if not bids else 'ask'}")
        bid, ask = float(bids[0][0]), float(asks[0][0])
        if not np.isfinite(bid) or not np.isfinite(ask) or bid <= 0.0 or ask <= 0.0:
            raise Unfillable(f"{symbol} top of book is not a price: bid={bid} ask={ask}")
        mids[label] = (bid + ask) / 2.0
        mids[f"{label}_spread_bps"] = (ask - bid) / ((bid + ask) / 2.0) * 10000.0

    return {
        "spot_mid": mids["spot"],
        "perp_mid": mids["perp"],
        "basis_bps": (mids["perp"] - mids["spot"]) / mids["spot"] * 10000.0,
        "spot_spread_bps": mids["spot_spread_bps"],
        "perp_spread_bps": mids["perp_spread_bps"],
    }


def measure_asset(
    exchange,
    markets: dict,
    base: str,
    *,
    limit: int,
    hold_days: int,
    min_session_notional: float,
    recent_days: int,
) -> dict:
    result: dict = {"asset": base}
    spot_symbol, perp_symbol = resolve_pair(markets, base)
    result["spot_symbol"] = spot_symbol
    result["perp_symbol"] = perp_symbol

    spot_bars = exchange.fetch_ohlcv(spot_symbol, "1d", limit=limit)
    perp_bars = exchange.fetch_ohlcv(perp_symbol, "1d", limit=limit)
    result["spot_bars"] = len(spot_bars)
    result["perp_bars"] = len(perp_bars)

    spot_by_date = bars_by_date(spot_bars)
    perp_by_date = bars_by_date(perp_bars)

    # Both floors are computed. The traded series is the headline and the raw
    # series is printed beside it, so the filter is visible as a difference
    # rather than trusted as a cleanup.
    raw = align_basis(spot_by_date, perp_by_date, min_session_notional=0.0)
    aligned = align_basis(
        spot_by_date, perp_by_date, min_session_notional=min_session_notional
    )

    dates, basis = aligned["dates"], aligned["basis_bps"]
    result["excluded"] = aligned["excluded"]
    if dates:
        result["window"] = (dates[0], dates[-1])

    result["levels"] = describe(basis)
    result["levels_raw"] = describe(raw["basis_bps"])
    changes, unmatched = hold_changes(dates, basis, hold_days)
    result["unmatched_exits"] = unmatched
    result["changes"] = describe_changes(changes)
    raw_changes, _ = hold_changes(raw["dates"], raw["basis_bps"], hold_days)
    result["changes_raw"] = describe_changes(raw_changes)

    # The recent window, on the same discipline the research harness applies to
    # every strategy here: full-sample is reported, recent decides. A basis tail
    # set during a spot listing week is a fact about that week, and the question
    # asked is what the exposure is now.
    cutoff = dates[-1] - timedelta(days=recent_days)
    recent_idx = [i for i, d in enumerate(dates) if d >= cutoff]
    recent_dates = [dates[i] for i in recent_idx]
    recent_basis = basis[recent_idx]
    try:
        result["recent_levels"] = describe(recent_basis)
        recent_changes, _ = hold_changes(recent_dates, recent_basis, hold_days)
        result["recent_changes"] = describe_changes(recent_changes)
    except Unfillable as exc:
        result["recent_levels"] = None
        result["recent_reason"] = str(exc)

    try:
        result["vol_link"] = vol_link(dates, basis, perp_by_date)
    except Unfillable as exc:
        result["vol_link"] = None
        result["vol_link_reason"] = str(exc)

    result["live"] = _live_basis(exchange, spot_symbol, perp_symbol)
    return result


def _print_asset(
    r: dict, *, hold_days: int, notional: float, carry_pct_yr: float, recent_days: int
) -> None:
    print(f"  {r['asset']}   {r['spot_symbol']}  vs  {r['perp_symbol']}")

    if "error" in r:
        print(f"    UNFILLABLE: {r['error']}")
        print()
        return

    w = r.get("window")
    print(
        f"    bars fetched   spot {r['spot_bars']}  perp {r['perp_bars']}"
        f"   traded sessions {r['levels']['n']}"
        + (f"  ({w[0]} to {w[1]})" if w else "")
    )

    excluded = r["excluded"]
    if excluded:
        counts: dict[str, int] = {}
        for e in excluded:
            counts[e["reason"]] = counts.get(e["reason"], 0) + 1
        print(f"    excluded       {counts}")
        for e in excluded:
            b = "not a price" if e["basis_bps"] is None else f"{e['basis_bps']:+.1f} bps"
            usd = (
                ""
                if "spot_usd" not in e
                else f"  traded spot ${e['spot_usd']:,.2f} / perp ${e['perp_usd']:,.2f}"
            )
            print(
                f"                   {e['date']}  {e['reason']:12s} spot {e['spot']:.6g}"
                f"  perp {e['perp']:.6g}  raw {b}{usd}"
            )

    lv = r["levels"]
    print(
        f"    level bps      mean {lv['mean_bps']:+7.2f}   sd {lv['sd_bps']:6.2f}"
        f"   max|b| {lv['max_abs_bps']:7.2f}   p99|b| {lv['p99_abs_bps']:6.2f}"
    )
    print(
        f"                   p1 {lv['p1_bps']:+7.2f}   p99 {lv['p99_bps']:+7.2f}"
        f"   range [{lv['min_bps']:+.2f}, {lv['max_bps']:+.2f}]"
        f"   max/sd {_fmt(lv['max_in_sd'], '.1f')}"
    )
    if lv["max_in_sd_reason"]:
        print(f"                   {lv['max_in_sd_reason']}")

    raw = r["levels_raw"]
    if raw["n"] != lv["n"]:
        print(
            f"    unfiltered     n {raw['n']}   mean {raw['mean_bps']:+.2f}"
            f"   sd {raw['sd_bps']:.2f}   max|b| {raw['max_abs_bps']:.2f}"
            f"   <- what the excluded sessions do to the tail"
        )

    ch = r["changes"]
    print(
        f"    {hold_days}d change    n {ch['n']:4d}   mean {ch['mean_bps']:+7.2f}"
        f"   sd {ch['sd_bps']:6.2f}   worst adverse {ch['worst_adverse_bps']:+7.2f}"
        f"   p99 adverse {ch['p99_adverse_bps']:+6.2f}"
    )

    rl, rc = r.get("recent_levels"), r.get("recent_changes")
    if rl is None:
        print(f"    last {recent_days}d      UNFILLABLE: {r.get('recent_reason')}")
    else:
        print(
            f"    last {recent_days}d      n {rl['n']:4d}   mean {rl['mean_bps']:+7.2f}"
            f"   sd {rl['sd_bps']:6.2f}   max|b| {rl['max_abs_bps']:7.2f}"
            f"   p99|b| {rl['p99_abs_bps']:6.2f}"
            f"   | {hold_days}d worst adverse {rc['worst_adverse_bps']:+7.2f}"
        )

    vl = r.get("vol_link")
    if vl is None:
        print(f"    vol link       UNFILLABLE: {r.get('vol_link_reason')}")
    else:
        print(
            f"    vol link       corr(|b|,|ret|) {vl['pearson']:+.3f}"
            f"   top-decile days (|ret| >= {vl['cut_return_pct']:.1f}%):"
            f" mean|b| {vl['volatile_mean_abs_bps']:6.2f} max {vl['volatile_max_abs_bps']:7.2f}"
            f"   vs quiet: mean {vl['quiet_mean_abs_bps']:6.2f} max {vl['quiet_max_abs_bps']:7.2f}"
        )

    live = r["live"]
    print(
        f"    live now       spot {live['spot_mid']:.6g}   perp {live['perp_mid']:.6g}"
        f"   basis {live['basis_bps']:+7.2f} bps"
        f"   (spreads {live['spot_spread_bps']:.1f} / {live['perp_spread_bps']:.1f} bps)"
    )

    for label, worst in (
        ("full sample", ch["worst_adverse_bps"]),
        (f"last {recent_days}d", rc["worst_adverse_bps"] if rc else None),
    ):
        if worst is None:
            continue
        econ = pair_economics(
            notional=notional,
            carry_pct_yr=carry_pct_yr,
            hold_days=hold_days,
            adverse_bps=worst,
        )
        verdict = "EXCEEDS" if econ["adverse_cost_usd"] > econ["earned_usd"] else "under"
        print(
            f"    on ${notional:,.0f}     earns ${econ['earned_usd']:,.2f} over {hold_days}d"
            f"   worst {hold_days}d move ({label}) costs ${econ['adverse_cost_usd']:,.2f}"
            f"   -> {verdict} the carry ({econ['cost_over_earned']:.2f}x)"
        )
    print()


def main(argv: Sequence[str] | None = None) -> int:
    import ccxt

    parser = argparse.ArgumentParser(
        description="Measure present spot/perp basis exposure on the carry book.",
    )
    parser.add_argument("--venue", default="hyperliquid")
    parser.add_argument("--universe", nargs="*", default=CARRY_UNIVERSE)
    parser.add_argument("--held", nargs="*", default=HELD)
    parser.add_argument(
        "--limit",
        type=int,
        default=5000,
        help="explicit OHLCV depth; the default ccxt page of 500 hides history (Finding 5)",
    )
    parser.add_argument("--hold-days", type=int, default=42)
    parser.add_argument(
        "--min-session-notional",
        type=float,
        default=1000.0,
        help="USD traded on each leg below which a session is 'no market' and excluded by name",
    )
    parser.add_argument(
        "--recent-days",
        type=int,
        default=180,
        help="trailing window reported beside the full sample; recent decides here",
    )
    parser.add_argument("--notional", type=float, default=10000.0)
    parser.add_argument("--adverse-bps", type=float, default=180.0)
    parser.add_argument("--carry-pct-yr", type=float, default=HYPERLIQUID_NET_PCT_YR)
    args = parser.parse_args(argv)

    exchange = getattr(ccxt, args.venue)({"enableRateLimit": True})
    markets = exchange.load_markets()

    print("=" * 78)
    print("PRESENT BASIS EXPOSURE -- basis = (perp - spot) / spot, bps")
    print(f"  venue {args.venue}   {len(markets)} markets   quote {QUOTE}")
    print(f"  ohlcv limit {args.limit} (explicit)   hold {args.hold_days}d   pair ${args.notional:,.0f}/leg")
    print(f"  session floor ${args.min_session_notional:,.0f} traded per leg; excluded sessions listed by date")
    print("  long spot / short perp: a RISE in basis is the adverse direction")
    print("=" * 78)
    print()

    held = [a for a in args.universe if a in set(args.held)]
    rest = [a for a in args.universe if a not in set(args.held)]

    results: dict[str, dict] = {}
    for group_label, group in (("HELD BY THE BOOK", held), ("HOLDABLE, NOT HELD", rest)):
        if not group:
            continue
        print(f"-- {group_label} " + "-" * (76 - len(group_label)))
        for base in group:
            try:
                r = measure_asset(
                    exchange,
                    markets,
                    base,
                    limit=args.limit,
                    hold_days=args.hold_days,
                    min_session_notional=args.min_session_notional,
                    recent_days=args.recent_days,
                )
            except Unfillable as exc:
                r = {"asset": base, "error": str(exc)}
                for key, default in (("spot_symbol", "?"), ("perp_symbol", "?")):
                    r.setdefault(key, default)
            except Exception as exc:  # noqa: BLE001 - a probe must report any failure shape
                r = {
                    "asset": base,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "spot_symbol": "?",
                    "perp_symbol": "?",
                }
            results[base] = r
            _print_asset(
                r,
                hold_days=args.hold_days,
                notional=args.notional,
                carry_pct_yr=args.carry_pct_yr,
                recent_days=args.recent_days,
            )

    print("-- THE $10K QUESTION " + "-" * 57)
    for label, rate in (
        (f"hyperliquid six-name book, net ({HYPERLIQUID_NET_PCT_YR}%/yr, OMNI_ANALYST s3)", args.carry_pct_yr),
        (f"binance capped basket, net ({BINANCE_NET_PCT_YR}%/yr, MONEY_PLAN)", BINANCE_NET_PCT_YR),
    ):
        econ = pair_economics(
            notional=args.notional,
            carry_pct_yr=rate,
            hold_days=args.hold_days,
            adverse_bps=args.adverse_bps,
        )
        print(f"  {label}")
        print(
            f"    earns ${econ['earned_usd']:,.2f} over {args.hold_days}d"
            f"   = {econ['breakeven_bps']:.1f} bps of basis absorbed before the hold is flat"
        )
        print(
            f"    a {args.adverse_bps:.0f} bps adverse move costs ${econ['adverse_cost_usd']:,.2f}"
            f"   = {econ['cost_over_earned']:.2f}x the hold's carry"
        )
    print()

    measured = [r for r in results.values() if "error" not in r]
    if measured:
        for label, key in (("full sample", "changes"), (f"last {args.recent_days}d", "recent_changes")):
            pool = [r for r in measured if r.get(key)]
            if not pool:
                continue
            worst = max(pool, key=lambda r: r[key]["worst_adverse_bps"])
            bps = worst[key]["worst_adverse_bps"]
            print(
                f"  worst {args.hold_days}d adverse move in the universe, {label}:"
                f" {worst['asset']} at {bps:+.1f} bps"
                f" (${args.notional * bps / 10000.0:,.2f} on a ${args.notional:,.0f} pair)"
            )
        held_measured = [r for r in measured if r["asset"] in set(args.held) and r.get("recent_changes")]
        if held_measured:
            worst_held = max(held_measured, key=lambda r: r["recent_changes"]["worst_adverse_bps"])
            bps = worst_held["recent_changes"]["worst_adverse_bps"]
            print(
                f"  worst among HELD names, last {args.recent_days}d:"
                f" {worst_held['asset']} at {bps:+.1f} bps"
                f" (${args.notional * bps / 10000.0:,.2f})"
            )
    failed = [r["asset"] for r in results.values() if "error" in r]
    if failed:
        print(f"  unfillable: {failed}")
    print("=" * 78)
    print(
        "  A perpetual does not expire, so an adverse basis is a mark-to-market\n"
        "  on the perp leg's margin, not a realised loss, until an exit -- taken\n"
        "  or forced. Sizing decisions belong to the margin, not to the notional."
    )
    print("=" * 78)
    return 0 if measured else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
