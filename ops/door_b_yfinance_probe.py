"""Door B scratch probe: book-to-market value factor using yfinance prices.

equity_value_factor.py ran the PIT-correct B/M factor end-to-end but returned
"underpowered, 7 periods below the 20-period floor" -- because Polygon's free
tier caps equity prices at 2 years. The fundamentals span 20 (EDGAR, 715k
claims). This probe fills the price gap with yfinance (~decades, free) to
settle whether Door B deserves a proper ingest path.

LICENSING: yfinance data is NOT ingested into the claim store. This is a
measurement-only probe -- it reads fundamentals from the store (EDGAR, public
domain) and prices directly from Yahoo, computes the factor, runs it through
the harness, reports a t-stat, and exits. The prices never become claims and
are never audience-scoped. If Door B passes, the ingest decision (yfinance
classification, paid Polygon, or Alpha Vantage) is made separately.

Run on deployment-host (fundamentals live on prod):

  pip install --target=/tmp/yflib --no-deps yfinance multitasking peewee
  PYTHONPATH=/tmp/yflib docker compose -f docker-compose.prod.yml exec -T \
    -e PYTHONPATH=/tmp/yflib scheduler python - < ops/door_b_yfinance_probe.py

Pre-registered design (identical to equity_value_factor.py except the price
source):

  signal    book-to-market = StockholdersEquity / (shares * close)
  universe  every company with fundamentals in the store
  horizon   63 trading days (~quarterly)
  costs     20 bps round trip
  shape     long the top B/M quintile (cheapest), short the bottom (richest)
  bar       the registry's strict bar, NOT recorded (record=False)
"""

from __future__ import annotations

import asyncio

import pandas as pd

HORIZON = 63
COST_BPS = 20.0


def _database_url() -> str:
    try:
        from omni.config import settings
        if settings.database_url:
            return settings.database_url
    except ImportError:
        pass
    return "postgresql://postgres:postgres@localhost:5434/omni_v2"


def _registry():
    import os

    from omni.research.registry import Registry
    path = os.environ.get("OMNI_REGISTRY_PATH")
    return Registry(path=path) if path else Registry()


def _fetch_yfinance_prices(symbols: list[str]) -> pd.DataFrame:
    """Deep daily closes from yfinance, returned as a date x symbol panel."""
    import yfinance as yf

    print(f"fetching yfinance prices for {len(symbols)} symbols...")
    batches = [symbols[i:i + 50] for i in range(0, len(symbols), 50)]
    frames = []
    for i, batch in enumerate(batches):
        ticker_str = " ".join(batch)
        print(f"  batch {i+1}/{len(batches)} ({len(batch)} symbols)")
        df = yf.download(
            ticker_str,
            period="max",
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="ticker",
        )
        if df.empty:
            continue
        if len(batch) == 1:
            sym = batch[0]
            if "Close" in df.columns:
                s = df["Close"].dropna()
                s.name = sym
                frames.append(s.to_frame())
        else:
            for sym in batch:
                if sym in df.columns.get_level_values(0):
                    col = df[sym]["Close"] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
                    s = col.dropna() if hasattr(col, "dropna") else pd.Series(dtype=float)
                    if not s.empty:
                        s.name = sym
                        frames.append(s.to_frame())
    if not frames:
        return pd.DataFrame()
    prices = pd.concat(frames, axis=1).sort_index()
    prices.index = pd.to_datetime(prices.index).tz_localize(None).normalize()
    prices = prices.groupby(level=0).last()
    return prices


async def main() -> int:
    import asyncpg

    conn = await asyncpg.connect(_database_url())

    companies = {
        r["symbol"]: r["id"]
        for r in await conn.fetch(
            "SELECT id, symbol FROM entity WHERE kind='company' "
            "AND symbol IS NOT NULL ORDER BY symbol"
        )
    }
    if not companies:
        print("no companies in store")
        return 1
    print(f"companies with symbols: {len(companies)}")
    ids = list(companies.values())

    fund_rows = await conn.fetch(
        "SELECT entity_id, key, event_date, knowledge_date, "
        "(value->>'value')::float8 AS val FROM claim "
        "WHERE claim_type='fundamental_metric' AND entity_id = ANY($1::uuid[]) "
        "AND key IN ('StockholdersEquity','CommonStockSharesOutstanding') "
        "AND value->>'value' IS NOT NULL",
        ids,
    )
    await conn.close()

    id_to_sym = {v: k for k, v in companies.items()}
    symbols_with_fundamentals = sorted({
        id_to_sym[r["entity_id"]]
        for r in fund_rows
        if r["entity_id"] in id_to_sym and r["val"] is not None
    })
    print(f"symbols with fundamentals: {len(symbols_with_fundamentals)}")
    if not symbols_with_fundamentals:
        print("no fundamentals found")
        return 1

    prices = _fetch_yfinance_prices(symbols_with_fundamentals)
    if prices.empty:
        print("yfinance returned no prices")
        return 1
    print(f"price panel: {prices.shape[0]} dates x {prices.shape[1]} symbols")
    print(f"  date range: {prices.index.min().date()} -> {prices.index.max().date()}")

    flong = pd.DataFrame(
        [{"sym": id_to_sym[r["entity_id"]], "knowledge_date": pd.Timestamp(r["knowledge_date"]),
          "kind": r["key"], "val": r["val"]}
         for r in fund_rows if r["entity_id"] in id_to_sym and r["val"] is not None]
    )
    flong["knowledge_date"] = flong["knowledge_date"].dt.tz_convert(None).dt.normalize()

    bm_series: dict[str, pd.Series] = {}
    for sym in prices.columns:
        if sym not in set(flong["sym"]):
            continue
        pseries = prices[sym].dropna()
        if pseries.empty:
            continue
        book_f = flong[(flong["sym"] == sym) & (flong["kind"] == "StockholdersEquity")]
        shares_f = flong[(flong["sym"] == sym) & (flong["kind"] == "CommonStockSharesOutstanding")]
        if book_f.empty or shares_f.empty:
            continue
        book_f = book_f.sort_values("knowledge_date")[["knowledge_date", "val"]].rename(
            columns={"knowledge_date": "date", "val": "book"})
        shares_f = shares_f.sort_values("knowledge_date")[["knowledge_date", "val"]].rename(
            columns={"knowledge_date": "date", "val": "shares"})
        pdf = pseries.rename("close").reset_index()
        if "Date" in pdf.columns:
            pdf = pdf.rename(columns={"Date": "date"})
        m = pd.merge_asof(pdf.sort_values("date"), book_f, on="date", direction="backward")
        m = pd.merge_asof(m.sort_values("date"), shares_f, on="date", direction="backward")
        m["bm"] = m["book"] / (m["shares"] * m["close"])
        s = pd.Series(m["bm"].values, index=m["date"])
        bm_series[sym] = s[~s.index.duplicated(keep="last")]

    bm = pd.DataFrame(bm_series).sort_index()
    bm = bm.reindex(index=prices.index)

    finite = bm.count().sum()
    print(f"B/M panel: {finite} finite (symbol,date) cells")
    if finite < 200:
        print("too few finite B/M cells to form quintile portfolios")
        return 1

    def signal(p: pd.DataFrame) -> pd.DataFrame:
        return bm.reindex(index=p.index, columns=p.columns)

    from omni.research.harness import evaluate
    verdicts = evaluate(
        name="equity.book_to_market.yfinance_probe",
        source="yfinance+claim_store",
        signal=signal,
        prices=prices,
        horizons=(HORIZON,),
        cost_bps=COST_BPS,
        registry=_registry(),
        record=False,
    )
    for v in verdicts:
        print()
        print(v.summary())
        for w in v.warnings:
            print(f"  warn: {w}")
    return 0


raise SystemExit(asyncio.run(main()))
