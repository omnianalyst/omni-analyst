"""Consensus picks vs the system's picks, measured over 5 and 10 years.

What it compares, equal-weight buy-and-hold (no rebalance), dividends
included (auto-adjusted closes), from the same display feed the scanner
reads:

  - consensus_stocks: the mega-caps everyone recommends (Mag 7)
  - consensus_etfs:   the five ETFs everyone recommends (VOO VTI QQQ SCHD VXUS)
  - consensus_blend:  50/50 of the two above
  - system_top5:      live top 5 by balanced score, all classes
  - system_quality5:  live top 5 by quality score (the candidate list)
  - system_top5_excrypto: top 5 balanced excluding crypto, so the 10-year
    window is not shortened by assets younger than the window

Honesty notes printed with the results:
  - The system's ranking is TODAY'S, applied retroactively. The balanced
    score's components (5y/10y CAGR) are computed on these same windows, so
    this is descriptive -- what holding today's picks would have looked like
    -- not a live backtest of decisions the system made then.
  - A portfolio's window starts when its youngest member started trading;
    10y/5y metrics only print when the window covers 95%+ of the span
    (the scanner's own floor).

Usage (sidecar from the app image):

    docker run --rm --network <stack>_default \\
      -e AUDIT_DB=postgresql://postgres:...@postgres:5432/omni_v2 \\
      -v $PWD/ops/portfolio_compare.py:/cmp.py:ro omni-api:latest python /cmp.py
"""

import asyncio
import os

import numpy as np
import pandas as pd

MAG7 = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"]
FIVE_ETFS = ["VOO", "VTI", "QQQ", "SCHD", "VXUS"]
YF = {**{s: s for s in MAG7 + FIVE_ETFS}}
MIN_COVER = 0.95


def fetch(tickers: list[str]) -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download(" ".join(tickers), period="11y", interval="1d",
                      auto_adjust=True, progress=False, group_by="ticker")
    frames = {}
    for ticker in tickers:
        try:
            col = raw[ticker]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        if not col.empty:
            frames[ticker] = col
    panel = pd.DataFrame(frames).sort_index()
    panel.index = pd.to_datetime(panel.index).tz_localize(None).normalize()
    return panel.groupby(level=0).last()


def portfolio_series(panel: pd.DataFrame, symbols: list[str]) -> pd.Series | None:
    cols = [panel[s].dropna() for s in symbols if s in panel.columns]
    if len(cols) != len(symbols):
        return None
    start = max(c.index[0] for c in cols)
    aligned = pd.concat(cols, axis=1, join="inner")
    aligned = aligned[aligned.index >= start]
    if aligned.empty:
        return None
    daily = aligned.mean(axis=1)  # equal weight, buy-and-hold price path
    return daily


def measure(series: pd.Series, label: str, as_of: str) -> None:
    first = series.index[0]
    last = series.index[-1]
    years_all = (last - first).days / 365.2425
    year_ends = series.resample("YE").last()
    annual = year_ends.pct_change(fill_method=None).dropna() * 100
    annual = annual[annual.index.year < last.year]
    daily_ret = series.pct_change().dropna()
    vol = float(daily_ret.std(ddof=1) * np.sqrt(252) * 100)
    roll_max = series.cummax()
    dd = (series / roll_max - 1).min()
    worst_year = float(annual.min()) if len(annual) else None

    def cagr(window: float) -> float | None:
        if years_all < window * MIN_COVER:
            return None
        target = last - pd.DateOffset(years=int(window))
        eligible = series[series.index <= target]
        if eligible.empty and years_all >= window * 0.98:
            eligible = series.iloc[:1]  # window start sits just before first print
        if eligible.empty:
            return None
        start_price = float(eligible.iloc[-1])
        actual_years = (last - eligible.index[-1]).days / 365.2425
        if start_price <= 0 or actual_years <= 0:
            return None
        return ((float(series.iloc[-1]) / start_price) ** (1 / actual_years) - 1) * 100

    def growth(window: float) -> float | None:
        c = cagr(window)
        return None if c is None else 10000 * ((1 + c / 100) ** window)

    print(f"\n{label}")
    print(f"  window: {first.date()} to {last.date()} ({years_all:.1f}y) · as of ranking {as_of[:10]}")
    for window in (5, 10):
        c = cagr(window)
        g = growth(window)
        print(
            f"  {window}y CAGR: " + (f"{c:.1f}%/yr" if c is not None else "n/a (window too short)")
            + (f" · $10,000 -> ${g:,.0f}" if g is not None else "")
        )
    print(
        f"  worst calendar year: {worst_year:.1f}% ({int(annual.idxmin().year)})" if worst_year is not None
        else "  worst calendar year: n/a"
    )
    print(f"  volatility {vol:.1f}% · max drawdown {dd * 100:.1f}%")


async def main() -> int:
    from omni.api.scanner import _build_scanner
    from omni.main import create_app

    app = create_app()
    from omni.db import connect, migrate

    client = await connect(os.environ.get("AUDIT_DB") or None)
    await migrate(client)
    app.db = client

    audience = os.environ.get("AUDIT_AUDIENCE")
    if not audience:
        row = await client.pool.fetchrow("SELECT id FROM users ORDER BY created_at LIMIT 1")
        audience = str(row["id"]) if row else None
    payload = await _build_scanner(app, audience)

    universe = []
    for cls in ("stocks", "defensive", "crypto"):
        for a in payload["category_rankings"].get(cls, []):
            if a["scores"].get("evidence_complete") is False:
                continue
            if not isinstance(a["scores"].get("balanced"), (int, float)):
                continue
            universe.append((a["symbol"], a["scores"]["balanced"], a["scores"].get("quality"), cls))
    universe.sort(key=lambda t: t[1], reverse=True)
    top5 = [s for s, _, _, _ in universe[:5]]
    excrypto = [s for s, _, _, cls in universe if cls != "crypto"][:5]
    quality = [s for s, _, q, _ in sorted(
        [u for u in universe if isinstance(u[2], (int, float))],
        key=lambda t: t[2], reverse=True,
    )][:5]

    print("System picks (live ranking):")
    print(f"  top 5 balanced : {', '.join(top5)}")
    print(f"  top 5 quality  : {', '.join(quality)}")
    print(f"  top 5 ex-crypto: {', '.join(excrypto)}")

    # Map symbols to display-feed tickers the same way the scanner does.
    import yfinance as yf  # noqa: F401  (import check)

    from omni.api.scanner import ASSETS
    yf_of = {a["symbol"]: a["yf"] for assets in ASSETS.values() for a in assets}

    portfolios = {
        "Consensus stocks (Mag 7, equal weight)": MAG7,
        "Consensus ETFs (VOO VTI QQQ SCHD VXUS, equal weight)": FIVE_ETFS,
        "Consensus blend (50/50 stocks+ETFs)": MAG7 + FIVE_ETFS,
        "System top 5 balanced (all classes)": top5,
        "System top 5 quality": quality,
        "System top 5 balanced ex-crypto": excrypto,
    }

    tickers = sorted({yf_of.get(s, s) for syms in portfolios.values() for s in syms})
    panel = fetch(tickers)
    as_of = payload.get("as_of", "")

    print("\nCaveats: the system's ranking is today's, applied retroactively -- its")
    print("balanced score uses these same 5y/10y windows, so treat this as descriptive")
    print("(what holding today's picks would have looked like), not a live backtest.")
    print("Equal weight, bought once at window start, no rebalance, dividends included.")

    for label, symbols in portfolios.items():
        series = portfolio_series(panel, [yf_of.get(s, s) for s in symbols])
        if series is None:
            print(f"\n{label}\n  skipped: a member is missing from the feed")
            continue
        measure(series, label, as_of)

    await client.close()
    return 0


raise SystemExit(asyncio.run(main()))
