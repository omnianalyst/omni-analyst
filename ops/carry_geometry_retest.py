"""Carry as directional calls with fixed geometry -- the re-test.

The first carry test (2026-08-09, `improve.carry.*`) measured blend-level
t-statistics on quintile returns: inverse-vol reached t=4.45 and still failed
the multiple-testing bar. This re-test asks the narrower question the payoff
accounting taught us to ask: does a funding-rate-sorted book, expressed as
calls with barrier-fixed outcomes, realize a payoff ratio that pays for its
risk? Same source data (Binance OHLCV + funding), different statistic -- and
recorded as a NEW hypothesis so the bar rises for everything after it.

Geometry (fixed before looking, per the invariant): entry at signal, target
= k_target * 14d realized vol, stop = k_stop * 14d vol, horizon 14d. The
asymmetry under test is carry: high-funding names should drift up (shorts
pay longs) more often than the barrier geometry of a driftless walk implies.

Run from the repo root: uv run python ops/carry_geometry_retest.py
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime

import httpx

from omni.research.registry import Registry

SOURCE = "binance_ohlcv+funding"
NAME_EQUAL = "carry.calls.barrier_equal"
NAME_HEAD = "carry.calls.barrier_head"

# geometry, fixed a priori: 14d horizon, symmetric 2-vol barriers. The test
# is whether realized hit-rate on the carry-sorted side beats the driftless
# first-passage probability -- an edge measured against geometry, not a blend.
K = 2.0
HORIZON_DAYS = 14
TOP_N = 8


async def fetch_panel() -> tuple[dict[str, list[float]], dict[str, list[float]], list[str]]:
    """Daily closes + mean daily funding for the perpetual majors, 2y."""
    async with httpx.AsyncClient(timeout=60) as client:
        ex = (await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")).json()
        symbols = [
            s["symbol"] for s in ex["symbols"]
            if s["contractType"] == "PERPETUAL" and s["status"] == "TRADING"
            and s["quoteAsset"] == "USDT"
        ][:40]
        closes: dict[str, list[float]] = {}
        funding: dict[str, list[float]] = {}
        dates: set[str] = set()
        for sym in symbols:
            resp = await client.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": sym, "interval": "1d", "limit": 730},
            )
            k = resp.json()
            resp = await client.get(
                "https://fapi.binance.com/fapi/v1/fundingRate",
                params={"symbol": sym, "limit": 1000},
            )
            f = resp.json()
            if not isinstance(k, list) or len(k) < 400:
                continue
            closes[sym] = [float(row[4]) for row in k]
            dates.update(str(row[0]) for row in k)
            # mean funding per day over the window
            if isinstance(f, list) and f:
                daily: dict[str, float] = {}
                for row in f:
                    day = datetime.fromtimestamp(row["fundingTime"] / 1000, UTC).strftime("%Y-%m-%d")
                    daily[day] = daily.get(day, 0.0) + float(row["fundingRate"])
                funding[sym] = list(daily.values())
        common = sorted(dates)
        return closes, funding, common


def barrier_outcomes(
    closes: list[float], start: int, direction: int
) -> tuple[str, float, float]:
    """Resolve one 14d call: does target or stop hit first (or neither)?"""
    window = closes[start : start + HORIZON_DAYS + 1]
    if len(window) < HORIZON_DAYS + 1:
        return "expiry", 0.0, 0.0
    entry = window[0]
    rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window)) if window[i - 1] > 0]
    vol = (sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / len(rets)) ** 0.5 if rets else 0.0
    if vol <= 0:
        return "expiry", 0.0, 0.0
    target = entry * (1 + direction * K * vol * math.sqrt(HORIZON_DAYS))
    stop = entry * (1 - direction * K * vol * math.sqrt(HORIZON_DAYS))
    hit = miss = None
    for price in window[1:]:
        if direction > 0:
            if price >= target: hit = price; break
            if price <= stop: miss = price; break
        else:
            if price <= target: hit = price; break
            if price >= stop: miss = price; break
    if hit is not None: return "hit", K, 1.0
    if miss is not None: return "miss", -1.0, 1.0
    return "expiry", (window[-1] / entry - 1) / (K * vol * math.sqrt(HORIZON_DAYS) or 1), 0.0


async def main() -> int:
    closes, funding, _ = await fetch_panel()
    # signal: trailing 3d mean funding, top-N by funding = shorts pay most ->
    # longs collect; barrier-test whether the drift is real against geometry.
    outcomes_high: list[tuple[str, float]] = []
    outcomes_all: list[tuple[str, float]] = []
    for sym, c in closes.items():
        f = funding.get(sym)
        if not f or len(f) < 30 or len(c) < 400:
            continue
        for start in range(60, len(c) - HORIZON_DAYS - 1, HORIZON_DAYS):  # non-overlapping, dense
            outcome, ratio, _ = barrier_outcomes(c, start, 1)
            outcomes_all.append((outcome, ratio))
            trailing = sum(f[-3:]) / 3 if len(f) >= 3 else 0
            if trailing > 0:  # longs collect funding
                outcomes_high.append((outcome, ratio))

    def summarize(outs: list[tuple[str, float]]) -> dict:
        hits = sum(1 for o, _ in outs if o == "hit")
        misses = sum(1 for o, _ in outs if o == "miss")
        n = hits + misses
        rate = hits / n if n else 0.0
        z = (rate - 0.5) * math.sqrt(n) / 0.5 if n else 0.0
        return {"n": n, "hit_rate": round(rate, 4), "z_vs_symmetric": round(z, 2)}

    def paired_z(a: dict, b: dict) -> float:
        """Two-proportion z of carry-side vs all-calls baseline. The raw
        symmetric-null z is invalid in a drifting regime: measured 2026-08-17,
        the BASELINE hit 0.79 against the 0.5 null in the 2024-26 bull tape --
        that is beta, not edge. Only the excess over baseline can be carry."""
        if not a["n"] or not b["n"]:
            return 0.0
        p1, n1, p2, n2 = a["hit_rate"], a["n"], b["hit_rate"], b["n"]
        p = (p1 * n1 + p2 * n2) / (n1 + n2)
        se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
        return (p1 - p2) / se if se > 0 else 0.0

    print("carry side (longs collect):", summarize(outcomes_high))
    print("all calls baseline:        ", summarize(outcomes_all))
    s = summarize(outcomes_high)
    base = summarize(outcomes_all)
    z = round(paired_z(s, base), 2)
    print(f"paired carry-vs-baseline z: {z}")

    reg = Registry()
    entry = reg.record(
        name="carry.calls.barrier_paired",
        source=SOURCE,
        cells=s["n"],
        verdict="fail" if abs(z) < 2.8 else "pass",
        detail={
            "hit_rate_carry": s["hit_rate"], "hit_rate_baseline": base["hit_rate"],
            "paired_z": z, "geometry": f"{K}x vol, {HORIZON_DAYS}d",
            "supersedes": "carry.calls.barrier_head (its 0.5-null was invalid: "
                          "baseline hit 0.79 in the 2024-26 bull tape -- beta, not edge)",
        },
    )
    print(f"recorded {entry.name}: {entry.verdict} (paired z={z})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
