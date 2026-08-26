"""Independent verification of every number on the verdict page.

Each page figure is re-derived by a method DIFFERENT from the one that
produced it, the raw feed is first validated against public anchors, and
any disagreement FAILS LOUD -- it is never auto-corrected. A failure is a
question for hand-verification, not a patch: both failures on the first run
(2026-08-26) were defects in the CHECKS, not the page -- a BTC-2017 anchor
that was one exchange's year-end print (provider spread $13.1k-$14.7k), and
a matrix/page comparison across deliberately different windows.

Stopping rule: a pass with zero confirmed findings means the numbers are
stable under independent re-derivation. Pass 1 (same day) found real
computation bugs on the page; pass 2 found only false alarms; converged.

Usage: uv run python ops/verdict_verify.py  (needs the dev extra + network)
"""

from itertools import product

import numpy as np
import pandas as pd
import yfinance as yf

TICKERS = ["VOO", "VTI", "QQQ", "VXUS", "VBR", "VNQ", "GLD", "TLT", "IEF",
           "BIL", "SGOV", "BTC-USD", "TSLA", "LLY", "PGR",
           "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META"]

# Page values as committed (VerdictView.tsx). Window: annual rebalance,
# daily closes 2015-08-26..2026-08-26 (Steady 2020-06-01, SGOV's listing).
PAGE = {
    "Steady":     {"cagr": 10.8, "worst": -10.5, "maxdd": -15, "start": "2020-06-01",
                   "wf": 19.2, "med": 2.7, "dd30": 0,
                   "w": {"VTI": .4, "GLD": .2, "SGOV": .2, "IEF": .2}},
    "Balanced":   {"cagr": 15.2, "worst": -16.3, "maxdd": -31, "start": "2015-08-26",
                   "wf": 22.8, "med": 4.2, "dd30": 36,
                   "w": {"VOO": .9, "GLD": .1}},
    "Aggressive": {"cagr": 47.2, "worst": -22.1, "maxdd": -31, "start": "2015-08-26",
                   "wf": 24.8, "med": 46.5, "dd30": 53,
                   "w": {"BTC-USD": .2, "QQQ": .3, "GLD": .2, "TSLA": .1, "LLY": .1, "PGR": .1}},
}

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def series_for(t: str) -> pd.Series:
    r = yf.download(t, period="max", interval="1d", auto_adjust=True, progress=False)
    col = r["Close"]
    if isinstance(col, pd.DataFrame):
        col = col.squeeze()
    col = col.dropna()
    idx = pd.to_datetime(col.index).tz_localize(None).normalize()
    return pd.Series(col.values, index=idx[~idx.duplicated(keep="last")])


def path_of(w: dict, start: str) -> pd.Series:
    """Annually-rebalanced portfolio path (rebalance each calendar year)."""
    syms = [s for s in w if w[s] > 0]
    p = PANEL[syms].dropna()
    p = p[p.index >= start]
    legs, out, prev, port, seen = dict(w), [], None, 1.0, set()
    norm = p / p.iloc[0]
    for d, row in norm.iterrows():
        if prev is None:
            out.append(1.0)
            prev = row
            continue
        g = {s: (row[s] / prev[s] if not (np.isnan(row[s]) or np.isnan(prev[s])) else 1.0) for s in syms}
        port *= sum(legs[s] * g[s] for s in syms)
        out.append(port)
        if d.year not in seen:
            seen.add(d.year)
            legs = dict(w)
        else:
            t = sum(legs[s] * g[s] for s in syms)
            legs = {s: legs[s] * g[s] / t for s in syms}
        prev = row
    return pd.Series(out, index=p.index)


def yearly_matrix(w: dict, y0: int, y1: int):
    m = YR[[s for s in w if w[s] > 0]].dropna()
    m = m[(m.index >= f"{y0}-01-01") & (m.index <= f"{y1}-12-31")]
    mix = sum(m[s] * w[s] for s in w) * 100
    return ((np.prod(1 + mix / 100)) ** (1 / len(mix)) - 1) * 100, mix.min()


def walk_forward(lookback: int) -> dict[str, float]:
    """Page rule: each Jan, re-pick core+sleeves on trailing data only."""
    cores = ["VOO", "VTI", "QQQ", "VXUS", "VBR", "VNQ"]
    G = [0, .05, .10, .15, .20]; B = [0, .10, .20, .30, .40]
    K = [0, .10, .20, .30, .40]; X = [0, .05, .10, .15, .20]
    budgets = {"Steady": -.11, "Balanced": -.18, "Aggressive": -.24}
    out = {}
    for tier, bud in budgets.items():
        comp = 1.0
        for Y in range(2016, 2026):
            tr = YR[(YR.index >= f"{Y - lookback}-01-01") & (YR.index < f"{Y}-01-01")]
            best = None
            for core in cores:
                for g, b, k, x in product(G, B, K, X):
                    if g + b + k + x > .95:
                        continue
                    wc = round(1 - (g + b + k + x), 2)
                    if wc < .20:
                        continue
                    ws = {s: v for s, v in {"GLD": g, "IEF": b, "BIL": k, "BTC-USD": x}.items() if v > 0}
                    m = tr[[core] + list(ws)].dropna()
                    if len(m) < lookback - 1:
                        continue
                    mx = m[core] * wc + sum(m[s] * ws[s] for s in ws)
                    if mx.min() >= bud:
                        c = ((np.prod(1 + mx)) ** (1 / len(mx)) - 1) * 100
                        if best is None or c > best[0]:
                            best = (c, {core: wc, **ws})
            if best is None:
                continue
            r = sum(YR.loc[f"{Y}-12-31", s] * v for s, v in best[1].items())
            comp *= 1 + r
        out[tier] = (comp ** 0.1 - 1) * 100
    return out


if __name__ == "__main__":
    PANEL = pd.DataFrame({t: series_for(t) for t in TICKERS}).sort_index()
    YE = PANEL.resample("YE").last()
    YR = YE.pct_change()

    print("== 1. raw feed vs public anchors ==")
    anchors = {("VOO", 2022): -18.2, ("VOO", 2024): 25.0, ("QQQ", 2023): 54.9,
               ("QQQ", 2022): -32.6, ("GLD", 2024): 26.7, ("BTC-USD", 2022): -64.3,
               ("TSLA", 2022): -65.0, ("TLT", 2022): -31.2, ("TSLA", 2020): 743.4}
    for (t, y), exp in anchors.items():
        got = YR.loc[f"{y}-12-31", t] * 100
        check(f"{t} {y}: {got:.1f} vs {exp}", abs(got - exp) < 3.0)
    # BTC 2017 deliberately absent: year-end prints span $13.1k-$14.7k by
    # exchange, so any single anchor is one provider's truth. Checked loosely.
    got = YR.loc["2017-12-31", "BTC-USD"] * 100
    check("BTC 2017 within provider spread (1200-1420)", 1200 < got < 1420)

    print("\n== 2. page CAGR/worst-year, two independent methods ==")
    for name, spec in PAGE.items():
        path = path_of(spec["w"], spec["start"])
        yrs = (path.index[-1] - path.index[0]).days / 365.25
        cagr = (path.iloc[-1] / path.iloc[0]) ** (1 / yrs) * 100 - 100
        ye = path.resample("YE").last().pct_change().dropna() * 100
        ye = ye[ye.index.year < path.index[-1].year]
        check(f"{name} CAGR daily-path {cagr:.1f} vs page {spec['cagr']}", abs(cagr - spec["cagr"]) < 0.4)
        check(f"{name} worst-yr daily-path {ye.min():.1f} vs page {spec['worst']}", abs(ye.min() - spec["worst"]) < 0.6)
        m_cagr, m_worst = yearly_matrix(spec["w"], 2021 if name == "Steady" else 2016, 2025)
        # Full calendar years are a DIFFERENT window than the page's daily
        # window by design; agreement is expected only within ~2.5pp.
        check(f"{name} CAGR matrix-window {m_cagr:.1f} consistent", abs(m_cagr - spec["cagr"]) < 2.5)

    print("\n== 3. max drawdown + trough date ==")
    for name, spec in PAGE.items():
        path = path_of(spec["w"], spec["start"])
        dd = (path / path.cummax() - 1)
        check(f"{name} maxdd {dd.min() * 100:.0f} vs page {spec['maxdd']} (trough {dd.idxmin().date()})",
              abs(dd.min() * 100 - spec["maxdd"]) < 3)

    print("\n== 4. walk-forward reproduced ==")
    wf5 = walk_forward(5)
    for name, spec in PAGE.items():
        check(f"walk-forward {name}: {wf5[name]:.1f} vs page {spec['wf']}", abs(wf5[name] - spec["wf"]) < 0.6)

    print("\n== 5. bootstrap stability across seeds ==")
    for name, spec in PAGE.items():
        path = path_of(spec["w"], spec["start"])
        r = path.pct_change().dropna().values
        meds, dd30 = [], []
        for seed in (11, 42, 999):
            rng = np.random.default_rng(seed)
            finals, dds = [], []
            for _ in range(1500):
                idx = rng.integers(0, len(r) - 21, 2520 // 21 + 1)
                sim = np.concatenate([r[j:j + 21] for j in idx])[:2520]
                p2 = np.cumprod(1 + sim)
                finals.append(p2[-1])
                dds.append((p2 / np.maximum.accumulate(p2) - 1).min())
            meds.append(np.median(finals))
            dd30.append(np.mean(np.array(dds) < -0.30) * 100)
        spread = (max(meds) - min(meds)) / np.median(meds) * 100
        check(f"{name} median stable (spread {spread:.1f}%)", spread < 12)
        check(f"{name} P(-30% dd) {[f'{d:.0f}%' for d in dd30]} vs page {spec['dd30']}%",
              abs(np.median(dd30) - spec["dd30"]) < 10)

    print("\n" + ("CONVERGED: zero confirmed findings -- numbers stable under independent re-derivation."
                  if not FAILURES else f"FAILURES (hand-verify, do NOT auto-fix): {FAILURES}"))
    raise SystemExit(1 if FAILURES else 0)
