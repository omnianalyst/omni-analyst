"""Insider-following event study (calendar-time portfolio).

Pre-registered test (stated before any result): at each month-end, go LONG
stocks with net insider BUY value > 0 over the trailing 30 days (measured by
FILING date -- the disclosure anchor, never transaction date), equal-weight,
hold one month, benchmark against the S&P 500 (SPY). Long-only carries market
beta, so the benchmark is the index, not zero. Headline statistics are NET of
20 bps/month (conservative full-turnover charge).

CONFOUND GUARD (the validity-critical piece): insiders buy after drops
(Lakonishok & Lee 2001). If the long portfolio's PRIOR-month return is sharply
negative, any "edge" is likely short-term reversal, not insider information.
Reported alongside the result -- if the guard strips the edge, that's the
verdict.

Run in the scheduler container (has yfinance + the trades JSONL + EDGAR):
  python /tmp/insider_event_study.py --trades /tmp/form4_trades.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

COST_BPS_PER_MONTH = 20.0
WINDOW_DAYS = 30
BENCH = "SPY"


def load_trades(path: str) -> pd.DataFrame:
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    df = pd.DataFrame(rows)
    df["filing_date"] = pd.to_datetime(df["filing_date"]).dt.tz_localize(None)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    # only price-bearing open-market trades (P/S), value not null
    df = df[df["value"].notna() & df["code"].isin(["P", "S"])]
    return df


def fetch_prices(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    import yfinance as yf

    px = yf.download(tickers, start=start, end=end, auto_adjust=False, progress=False, threads=True)
    # MultiIndex (Price tier, Ticker) -> adj close frame
    adj = px["Adj Close"] if isinstance(px.columns, pd.MultiIndex) else px.to_frame(name=tickers[0])
    adj = adj.dropna(how="all").sort_index()
    return adj


def month_ends(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    return list(pd.date_range(start, end, freq="ME"))


def net_buy_value(trades: pd.DataFrame, ticker: str, as_of: pd.Timestamp) -> float:
    window = trades[(trades["issuer_ticker"] == ticker)
                    & (trades["filing_date"] > as_of - timedelta(days=WINDOW_DAYS))
                    & (trades["filing_date"] <= as_of)]
    buy = window.loc[window["side"] == "buy", "value"].sum()
    sell = window.loc[window["side"] == "sell", "value"].sum()
    return float(buy - sell)


def _stats(series: pd.Series) -> dict:
    s = series.dropna()
    if len(s) < 2:
        return {"n": len(s), "mean": float("nan"), "t": float("nan"), "hit": float("nan")}
    mean = float(s.mean())
    std = float(s.std(ddof=1))
    t = mean / (std / math.sqrt(len(s))) if std > 0 else float("nan")
    return {"n": len(s), "mean_bps": mean * 1e4, "t": t, "hit": float((s > 0).mean())}


def run(trades_path: str) -> int:
    trades = load_trades(trades_path)
    tickers = sorted(set(trades["issuer_ticker"]) - {BENCH})
    print(f"trades: {len(trades)} price-bearing P/S across {len(tickers)} tickers", flush=True)
    print(f"window: {trades['filing_date'].min().date()} .. {trades['filing_date'].max().date()}", flush=True)
    if len(tickers) < 5:
        print("too few tickers with insider trades to form a portfolio")
        return 1

    start = (trades["filing_date"].min() - timedelta(days=40)).strftime("%Y-%m-%d")
    end = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%d")
    px = fetch_prices(tickers + [BENCH], start, end)
    # resample to month-end adj close, monthly returns
    me = px.resample("ME").last()
    mret = me.pct_change()

    ends = month_ends(me.index.min(), me.index.max())
    excess: list[float] = []
    prior_ret: list[float] = []
    sizes: list[int] = []
    for i, t in enumerate(ends[:-1]):
        longs = [tk for tk in tickers if net_buy_value(trades, tk, t) > 0 and tk in mret.columns]
        sizes.append(len(longs))
        if not longs:
            excess.append(0.0)  # cash month -- 0 excess over SPY approx
            prior_ret.append(0.0)
            continue
        nxt = ends[i + 1]
        gross = mret.loc[nxt, longs].mean()
        if pd.isna(gross):
            excess.append(0.0)
            prior_ret.append(0.0)
            continue
        net = gross - COST_BPS_PER_MONTH / 1e4
        bench = mret.loc[nxt, BENCH] if BENCH in mret.columns else 0.0
        if pd.isna(bench):
            bench = 0.0
        excess.append(float(net - bench))
        # confound: prior-month return of the longs (insiders buy dips?)
        prev = ends[i - 1] if i > 0 else None
        pr = mret.loc[t, longs].mean() if prev is not None else float("nan")
        prior_ret.append(float(pr) if pd.notna(pr) else float("nan"))

    ex = pd.Series(excess)
    pr = pd.Series(prior_ret)
    third = max(1, len(ex) // 3)

    print("\n=== INSIDER-FOLLOWING (long net-buy, monthly, vs SPY) ===")
    print(f"months: {len(ex)}   avg portfolio size: {np.mean(sizes):.1f} names")
    print(f"\nfull sample:        {_stats(ex)}")
    print(f"recent third:       {_stats(ex.iloc[-third:])}")
    print(f"cumulative excess (geometric): {(np.prod(1 + ex) - 1) * 100:.2f}%")
    ir_full = math.sqrt(12) * ex.mean() / ex.std(ddof=1) if ex.std(ddof=1) > 0 else float("nan")
    print(f"information ratio (ann.):       {ir_full:.2f}")

    print("\n=== CONFOUND GUARD: prior-month return of the long portfolio ===")
    pr_clean = pr.dropna()
    if len(pr_clean):
        print(f"avg prior-month return: {pr_clean.mean() * 100:.2f}%  (negative = insiders bought DIPS)")
        print("if this is sharply negative and the excess survives, it may be reversal, not information.")
    else:
        print("no prior-return data")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--trades", default="/tmp/form4_trades.jsonl")
    raise SystemExit(run(p.parse_args().trades))
