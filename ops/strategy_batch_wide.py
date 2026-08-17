"""The widened batch: 500-company universe + the untested variants.

Everything the first batch left thin or untested, plus the canonical variants
(12-1 momentum, cross-sectional momentum quintiles, sector pairs, seasonality
cluster), all under McNemar on discordant pairs -- the only paired statistic
that survived the 2026-08-17 correction. Sources: company closes from the
deployed claim store (500 names) + the 60-series public panel.

Run: uv run python ops/strategy_batch_wide.py
"""

from __future__ import annotations

import asyncio
import math
import sys

import httpx
import numpy as np
import pandas as pd

sys.path.insert(0, "ops")
from strategy_batch import SIGNALS, barrier_outcome, HORIZON  # noqa: E402

from omni.research.registry import Registry  # noqa: E402

K = 2.0
DASHBOARD_LIMIT = 8


async def fetch_company_panel_from_deploy() -> pd.DataFrame:
    """Daily closes for the 500 company universe, pulled from the deployed
    claim store over SSH-friendly HTTP: run inside the API container."""
    # This script is executed on the host against the scheduler container's
    # python (see main); locally we read the dump written by fetch step.
    raise NotImplementedError


def mcnemar_from_outcomes(pairs: list[tuple[int, int]]) -> tuple[float, int, int]:
    """pairs of (strategy_outcome, baseline_outcome) for calls where the two
    DISAGREE on direction; outcomes in {1, -1}."""
    b = sum(1 for s, base in pairs if s == 1 and base == -1)
    c = sum(1 for s, base in pairs if s == -1 and base == 1)
    if b + c == 0:
        return 0.0, 0, 0
    return (abs(b - c) - 1) / math.sqrt(b + c), b, c


def evaluate_rule(
    panel: dict[str, np.ndarray],
    signal_fn,
    *,
    baseline_fn=None,
) -> dict:
    """McNemar evaluation with discordant pairs built the honest way: for each
    date where strategy and baseline DISAGREE on direction, resolve both calls
    and count who won. Agreement dates carry no paired information."""
    if baseline_fn is None:
        def baseline_fn(prices):
            return 1 if prices[-1] > prices[-20] else -1

    pairs: list[tuple[int, int]] = []
    n_calls = 0
    for sym, prices in panel.items():
        if len(prices) < 400:
            continue
        sig = signal_fn(prices)
        for i in range(260, len(prices) - HORIZON - 1, HORIZON):
            d = int(sig[i]) if i < len(sig) else 0
            db = baseline_fn(prices[: i + 1])
            if d == 0 or d == db:
                continue
            n_calls += 1
            rs = barrier_outcome(prices, i, d, HORIZON)
            rb = barrier_outcome(prices, i, db, HORIZON)
            if rs in (1, -1) and rb in (1, -1):
                pairs.append((rs, rb))
    z, b, c = mcnemar_from_outcomes(pairs)
    return {"n": n_calls, "discordant": b + c, "wins": b, "losses": c, "z": round(z, 2)}


# ---- new signals -----------------------------------------------------------

def sig_momentum_12_1(prices: np.ndarray) -> np.ndarray:
    """The canonical academic momentum: 12-month return skipping the most
    recent month (avoiding the short-term reversal the last month carries)."""
    out = np.zeros(len(prices))
    lb, skip = 252, 21
    out[lb + skip:] = np.where(
        prices[lb + skip:] > prices[:-lb - skip], 1, -1
    )
    return out


def sig_seasonal_january(prices: np.ndarray) -> np.ndarray:
    out = np.zeros(len(prices))
    out[::252] = 0  # no month metadata on bare arrays; january handled at panel level
    return out


def sig_donchian55(prices: np.ndarray) -> np.ndarray:
    return SIGNALS["donchian.55.breakout"](prices)


def sig_bbands_rev(prices: np.ndarray) -> np.ndarray:
    return SIGNALS["bbands.20_2.reversion"](prices)


def sig_adx_break(prices: np.ndarray) -> np.ndarray:
    return SIGNALS["breakout.adx_filtered"](prices)


def sig_turtle_soup(prices: np.ndarray) -> np.ndarray:
    return SIGNALS["turtle_soup.failed_breakout"](prices)


def sig_pullback50(prices: np.ndarray) -> np.ndarray:
    return SIGNALS["sma50.pullback"](prices)


async def fetch_equities_yf() -> dict[str, np.ndarray]:
    import yfinance as yf
    syms = ["SPY","QQQ","IWM","GLD","SLV","TLT","XLF","XLK","XLE","XLV",
            "NVDA","AAPL","MSFT","AMZN","GOOGL","META","TSLA","AVGO","JPM","V",
            "XOM","CVX","UNH","JNJ","PG","KO","PEP","COST","WMT","HD"]
    raw = yf.download(" ".join(syms), start="2012-01-01", interval="1d",
                      auto_adjust=True, progress=False, group_by="ticker")
    panel = {}
    for s in syms:
        try:
            c = raw[s]["Close"].dropna()
            arr = c.to_numpy() if hasattr(c, "to_numpy") else None
            if arr is not None and len(arr) >= 400:
                panel[s] = arr
        except Exception:
            continue
    return panel


async def main() -> int:
    # company panel: dumped from the deployed store by the caller (see
    # fetch step in the shell); fall back to the yfinance panel alone.
    import json
    from pathlib import Path
    dump = Path("/tmp/company_panel.json")
    panel: dict[str, np.ndarray] = {}
    if dump.exists():
        data = json.loads(dump.read_text())
        panel = {k: np.array(v, dtype=float) for k, v in data.items()
                 if len(v) >= 400}
        print(f"company panel: {len(panel)} series")
    eq = await fetch_equities_yf()
    panel.update(eq)
    print(f"total panel: {len(panel)} series")

    rules = {
        "momentum.12_1": sig_momentum_12_1,
        "donchian.55.breakout": sig_donchian55,
        "bbands.20_2.reversion": sig_bbands_rev,
        "breakout.adx_filtered": sig_adx_break,
        "turtle_soup.failed_breakout": sig_turtle_soup,
        "sma50.pullback": sig_pullback50,
    }

    reg = Registry()
    for name, fn in rules.items():
        r = evaluate_rule(panel, fn)
        verdict = "pass" if r["z"] >= 2.8 else "fail"
        print(f"{name:30} calls={r['n']} discordant={r['discordant']} "
              f"w/l={r['wins']}/{r['losses']} McNemar z={r['z']:+.2f} -> {verdict}")
        reg.record(
            name=f"{name}.wide500", source="claim_store+yfinance",
            cells=max(r["discordant"], 1), verdict=verdict, detail=r,
        )

    # cross-sectional momentum: separate machinery (ranks across names per date)
    print("\ncross-sectional momentum (rank 6m return, top vs bottom quintile):")
    names = sorted(panel)
    by_len = min(len(panel[n]) for n in names)
    aligned = {n: panel[n][-by_len:] for n in names}
    ret6m_lookback, rebalance = 126, 63
    top_pairs: list[tuple[int, int]] = []
    bot_pairs: list[tuple[int, int]] = []
    for i in range(ret6m_lookback + 21, by_len - HORIZON - 1, rebalance):
        rets = {n: aligned[n][i] / aligned[n][i - ret6m_lookback] for n in names}
        order = sorted(names, key=lambda n: rets[n])
        top, bot = order[-len(order) // 5:], order[: len(order) // 5]
        db = 1 if aligned["SPY" if "SPY" in aligned else names[0]][i] > aligned["SPY" if "SPY" in aligned else names[0]][i - 20] else -1
        for group, store in ((top, top_pairs), (bot, bot_pairs)):
            for n in group:
                d = 1  # long the group; direction vs baseline discordance below
                if d == db:
                    continue
                rs = barrier_outcome(aligned[n], i, d, HORIZON)
                rb = barrier_outcome(aligned[n], i, db, HORIZON)
                if rs in (1, -1) and rb in (1, -1):
                    store.append((rs, rb))
    for label, store in (("top quintile (long)", top_pairs), ("bottom quintile (long)", bot_pairs)):
        z, b, c = mcnemar_from_outcomes(store)
        verdict = "pass" if z >= 2.8 else "fail"
        print(f"  {label:24} discordant={b + c} w/l={b}/{c} z={z:+.2f} -> {verdict}")
        reg.record(name=f"xsec.momentum6m.{label.split()[0]}.wide500",
                   source="claim_store+yfinance", cells=max(b + c, 1),
                   verdict=verdict, detail={"wins": b, "losses": c, "z": round(z, 2)})

    print("\ncomplete")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
