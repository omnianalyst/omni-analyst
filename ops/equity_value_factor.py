"""Door B: book-to-market (HML) value factor on the equity cross-section.

Pre-registered design, stated before any result is seen:

  signal    book-to-market = StockholdersEquity / (shares * close)
  universe  every company with fundamentals AND prices in the store
  horizon   63 trading days (~quarterly) -- Finding 47 says slow is where
            the cost arithmetic survives
  costs     20 bps round trip
  shape     long the top B/M quintile (cheapest), short the bottom (richest)
  bar       the registry's strict bar, counting this run's cells

PIT DISCIPLINE -- the part Finding 32 bled on. Fundamentals join on
`knowledge_date` (when the filing went public), NOT `event_date` (the fiscal
period end). The gap between them is the lookahead. At rebalance date t the
book equity and shares are the most recent values with knowledge_date <= t,
via pandas merge_asof(direction='backward').

Book-to-market rather than the agenda's "earnings yield": NetIncomeLoss is
cumulative YTD in quarterly filings and would need TTM construction;
StockholdersEquity is a clean stock measure. B/M is the Fama-French HML factor.

Run on the deployment host (data lives on prod):
  docker compose -f docker-compose.prod.yml exec -T scheduler \
    python - < ops/equity_value_factor.py
"""

from __future__ import annotations

import asyncio

import pandas as pd

from omni.research.harness import evaluate
from omni.research.registry import Registry

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


def _registry() -> Registry:
    import os
    path = os.environ.get("OMNI_REGISTRY_PATH")
    return Registry(path=path) if path else Registry()


def _book_to_market(prices: pd.DataFrame, bm_panel: pd.DataFrame) -> pd.DataFrame:
    """Score = book-to-market, aligned to the price panel's dates and symbols.

    NaN where no fundamental is knowable yet at that date: the harness drops
    those names from the cross-section at those dates, which is correct (a name
    with no filed balance sheet has no book-to-market to rank on).
    """
    return bm_panel.reindex(index=prices.index, columns=prices.columns)


async def main() -> int:
    import asyncpg

    conn = await asyncpg.connect(_database_url())

    companies = {
        r["symbol"]: r["id"]
        for r in await conn.fetch(
            "SELECT id, symbol FROM entity WHERE kind='company' ORDER BY symbol"
        )
    }
    if not companies:
        print("no companies in store")
        return 1
    ids = list(companies.values())

    # Prices: one row per (entity, event_date). The audience that owns the most
    # company price claims is the operator; equity prices are byo_only.
    owner = await conn.fetchval(
        "SELECT audience_user_id FROM claim WHERE claim_type='price_snapshot' "
        "AND entity_id = ANY($1::uuid[]) GROUP BY audience_user_id "
        "ORDER BY count(*) DESC LIMIT 1",
        ids,
    )
    price_rows = await conn.fetch(
        "SELECT entity_id, event_date, (value->>'close')::float8 AS close "
        "FROM claim WHERE claim_type='price_snapshot' AND entity_id = ANY($1::uuid[]) "
        "AND audience_user_id = $2 AND value->>'close' IS NOT NULL "
        "ORDER BY entity_id, event_date",
        ids,
        owner,
    )
    if not price_rows:
        print("no equity prices visible to the owner; check Polygon ingest + audience")
        return 1

    # Fundamentals: book equity + shares, both PIT-stamped by knowledge_date.
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

    # Price panel: date x symbol. Normalize to midnight so every symbol aligns
    # on the same daily rows regardless of the source's intraday stamp.
    plong = pd.DataFrame(
        [{"sym": id_to_sym[r["entity_id"]], "date": pd.Timestamp(r["event_date"]), "close": r["close"]}
         for r in price_rows if r["entity_id"] in id_to_sym]
    )
    if plong.empty:
        print("no price rows after symbol join")
        return 1
    plong["date"] = plong["date"].dt.tz_convert(None).dt.normalize()
    prices = plong.pivot(index="date", columns="sym", values="close").sort_index()
    prices = prices.groupby(level=0).last()  # last close per (date, symbol) if dupes

    # Fundamentals long, per symbol, sorted by knowledge_date for the asof join.
    flong = pd.DataFrame(
        [{"sym": id_to_sym[r["entity_id"]], "knowledge_date": pd.Timestamp(r["knowledge_date"]),
          "kind": r["key"], "val": r["val"]}
         for r in fund_rows if r["entity_id"] in id_to_sym and r["val"] is not None]
    )
    if flong.empty:
        print("no StockholdersEquity/shares fundamentals")
        return 1
    flong["knowledge_date"] = flong["knowledge_date"].dt.tz_convert(None).dt.normalize()

    # PIT asof join per symbol: most recent book equity + shares known by each
    # price date (knowledge_date <= date), then B/M = book / (shares * close).
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
        m = pd.merge_asof(pdf.sort_values("date"), book_f, on="date", direction="backward")
        m = pd.merge_asof(m.sort_values("date"), shares_f, on="date", direction="backward")
        m["bm"] = m["book"] / (m["shares"] * m["close"])
        s = pd.Series(m["bm"].values, index=m["date"])
        bm_series[sym] = s[~s.index.duplicated(keep="last")]

    bm = pd.DataFrame(bm_series).sort_index()

    finite = bm.count().sum()
    print(f"price panel: {prices.shape[0]} dates x {prices.shape[1]} symbols")
    print(f"B/M panel: {finite} finite (symbol,date) cells")
    if finite < 200:
        print("too few finite B/M cells to form quintile portfolios")

    def signal(p: pd.DataFrame) -> pd.DataFrame:
        return _book_to_market(p, bm)

    verdicts = evaluate(
        name="equity.book_to_market",
        source="claim_store",
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
