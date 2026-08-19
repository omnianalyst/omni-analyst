"""The Gerhard battery: do his published SMA parameters survive outside his fit?

Source of the claims (summarized by Grok from Gerhard - Bitcoin Strategy's
public videos/ledger, 2026-08): grid-searched SMA trend-following on BTC,
"76% annualized vs 21% buy-and-hold post-2018, with fees" -- flagship
2/116 dual crossover, long/short; single-MA ~109-125 as robust alternative;
SOL 14/31 dual and single 63; leverage 2.5x long / 4x short.

The one structural fact that matters: he optimized ON post-2018 data, so
2018->~2023 is his in-sample and 2024->now is his out-of-sample whether he
chose it or not. Every cell therefore reports BOTH eras and the verdict
reads the recent era -- the registry's discipline applied to somebody
else's fitted parameters. If the edge is real it holds where he didn't fit;
if it is a grid-search artifact it dies there.

Cells (predeclared, 5):
  gerhard.btc.sma_2_116.long_short   golden cross long, death cross short
  gerhard.btc.sma_2_116.long_flat    golden cross long, else cash (the
                                     shape our own tsmom.252 survivor uses)
  gerhard.btc.sma_116.long_flat      the single-MA robust variant
  gerhard.sol.sma_14_31.long_short   his published SOL dual
  gerhard.sol.sma_63.long_flat       his published SOL single

Daily closes from listing (Binance spot, cached), signals at close t,
entry t+1, t-stat on daily excess vs buy-and-hold of the SAME asset over
the same windows (so a rising market does not grade a long-only rule as
skill), 40 bps per position change charged against the mean. Verdict needs
recent-era |t| >= bar AND positive net-of-cost excess AND same-sign t in
both eras.
"""

from __future__ import annotations

import asyncio
import sys

import numpy as np

sys.path.insert(0, "ops")
from rotation_batch import SOURCE, Registry, _t_stat, load_panel

COST_BPS_PER_FLIP = 40.0
SPLIT = 0.70  # his era: 2018 -> ~2023.7 on the BTC panel; recent: after


def sma(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if w <= 1:
        out[:] = x
        return out
    c = np.convolve(x, np.ones(w) / w, "valid")
    out[w - 1:] = c
    return out


def run_rule(
    prices: np.ndarray,
    *,
    fast: int,
    slow: int,
    mode: str,
) -> dict:
    """One SMA rule over the full panel. Position series from crossovers
    (or price-vs-single-MA when fast == slow); daily returns of the rule
    against daily returns of holding the asset outright."""
    fast_ma = sma(prices, fast)
    slow_ma = sma(prices, slow)
    if fast == slow:
        raw = np.where(prices > slow_ma, 1.0, -1.0)
    else:
        raw = np.where(fast_ma > slow_ma, 1.0, -1.0)
    usable = ~np.isnan(fast_ma) & ~np.isnan(slow_ma)
    position = np.where(usable, raw, 0.0)

    ret = prices[1:] / prices[:-1] - 1.0
    pos = position[1:]  # signal known at close t applies to t -> t+1
    strat = pos * ret
    excess = strat - ret  # vs holding the same asset

    flips = int(np.count_nonzero(np.diff(np.concatenate([[0.0], pos]))))
    n = len(excess)
    gross = float(np.mean(excess)) * 10_000.0
    net = gross - (flips * COST_BPS_PER_FLIP / n)

    split = int(n * SPLIT)
    his_era = list(excess[:split])
    recent = list(excess[split:])
    recent_n = max(len(recent) // 3, 3)
    return {
        "days": n,
        "flips": flips,
        "his_era_t": round(_t_stat(his_era), 2),
        "recent_t_full": round(_t_stat(recent), 2),
        "recent_t_recent_third": round(_t_stat(recent[-recent_n:]), 2),
        "mean_excess_bps_day": round(gross, 2),
        "net_bps_day": round(net, 2),
        "his_era_mean_bps": round(float(np.mean(his_era)) * 10_000.0, 2),
        "recent_mean_bps": round(float(np.mean(recent)) * 10_000.0, 2),
    }


def self_check() -> None:
    up = np.cumprod(1.0 + np.full(400, 0.002))  # steady bull
    rule = run_rule(up, fast=2, slow=116, mode="long_flat")
    assert rule["flips"] <= 2, rule
    chop = np.cumprod(1.0 + np.sin(np.arange(400) * 0.3) * 0.01)
    assert run_rule(chop, fast=2, slow=116, mode="long_flat")["flips"] > 10
    assert _t_stat([0.01] * 30) > 4


async def main() -> int:
    self_check()
    panel = await load_panel()
    btc = panel["BTC"]
    sol = panel["SOL"]
    print(f"BTC {len(btc)} closes, SOL {len(sol)} closes from listing\n")

    rules = [
        ("gerhard.btc.sma_2_116.long_short",
         lambda: run_rule(btc, fast=2, slow=116, mode="long_short")),
        ("gerhard.btc.sma_2_116.long_flat",
         lambda: run_rule(btc, fast=2, slow=116, mode="long_flat")),
        ("gerhard.btc.sma_116.long_flat",
         lambda: run_rule(btc, fast=116, slow=116, mode="long_flat")),
        ("gerhard.sol.sma_14_31.long_short",
         lambda: run_rule(sol, fast=14, slow=31, mode="long_short")),
        ("gerhard.sol.sma_63.long_flat",
         lambda: run_rule(sol, fast=63, slow=63, mode="long_flat")),
    ]

    reg = Registry()
    print(f"{'cell':38} {'days':>6} {'flips':>6} {'t_his':>7} {'t_rec':>7} "
          f"{'t_rec3':>7} {'bps_his':>8} {'bps_rec':>8} {'net':>7} verdict")
    passes: list[str] = []
    for name, fn in rules:
        r = fn()
        bar = reg.bar(pending_cells=max(r["days"] // 30, 1))
        stat = r["recent_t_recent_third"]
        ok = (
            abs(stat) >= bar
            and r["net_bps_day"] > 0
            and r["his_era_t"] != 0
            and (r["his_era_t"] > 0) == (stat > 0)
        )
        verdict = "pass" if ok else "fail"
        if ok:
            passes.append(name)
        detail = dict(r)
        detail["bar"] = round(bar, 3)
        detail["best_recent_third_t"] = abs(stat)
        print(
            f"{name:38} {r['days']:>6} {r['flips']:>6} "
            f"{r['his_era_t']:>+7.2f} {r['recent_t_full']:>+7.2f} "
            f"{stat:>+7.2f} {r['his_era_mean_bps']:>8.1f} "
            f"{r['recent_mean_bps']:>8.1f} {r['net_bps_day']:>+7.2f} {verdict}"
        )
        reg.record(
            name=name, source=SOURCE, cells=max(r["days"] // 30, 1),
            verdict=verdict, detail=detail,
        )

    print(f"\nregistry now {len(reg.entries())} entries, "
          f"bar for the next test {reg.bar(pending_cells=1):.3f}")
    print(f"passes: {passes if passes else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
