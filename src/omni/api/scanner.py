"""Cross-asset market scanner: sector leadership plus five regime buckets.

Shows the leading measured companies in each GICS sector, followed by tracked
assets across the five portfolio buckets (growth, debasement, deflation, safety,
alpha) with trailing returns, risk metrics, and current crypto funding rates.

Broad-asset prices come from yfinance (display-only, never ingested as claims —
the same pattern as the research probes). Company histories and funding rates
come from the audience-visible claim store. Results are cached for 1 hour.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from neutron import App, Router
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.coverage.visibility import visible_claims_cte

CACHE_TTL = 3600
SECTOR_RETURN_WINDOW = 30
SECTOR_LEADER_COUNT = 15
OVERALL_LEADER_COUNT = 15
_cache: dict[str, dict[str, Any]] = {}

ASSETS: dict[str, list[dict[str, str]]] = {
    "Growth": [
        {"symbol": "VTI", "name": "Vanguard Total Stock Market", "yf": "VTI",
         "asset_class": "stocks"},
        {"symbol": "SPY", "name": "S&P 500 ETF", "yf": "SPY",
         "asset_class": "stocks"},
        {"symbol": "QQQ", "name": "Invesco QQQ Trust", "yf": "QQQ",
         "asset_class": "stocks"},
        {"symbol": "VXUS", "name": "Vanguard Total International", "yf": "VXUS",
         "asset_class": "stocks"},
    ],
    "Debasement": [
        {"symbol": "GLD", "name": "SPDR Gold Shares", "yf": "GLD",
         "asset_class": "defensive"},
        {"symbol": "SLV", "name": "iShares Silver Trust", "yf": "SLV",
         "asset_class": "defensive"},
        {"symbol": "BTC", "name": "Bitcoin", "yf": "BTC-USD",
         "asset_class": "crypto"},
        {"symbol": "ETH", "name": "Ethereum", "yf": "ETH-USD",
         "asset_class": "crypto"},
        {"symbol": "SOL", "name": "Solana", "yf": "SOL-USD",
         "asset_class": "crypto"},
    ],
    "Deflation": [
        {"symbol": "TLT", "name": "iShares 20+ Year Treasury", "yf": "TLT",
         "asset_class": "defensive"},
    ],
    "Safety": [
        {"symbol": "SHV", "name": "iShares Short Treasury", "yf": "SHV",
         "asset_class": "defensive"},
    ],
}

BUCKET_ROLES = {
    "Growth": "Expansion — stocks win when the economy grows",
    "Debasement": "Inflation/stagflation — hard assets win when currency devalues",
    "Deflation": "Falling rates/prices — long bonds win when rates cut",
    "Safety": "Recession/liquidity — cash and T-bills preserve capital",
}

CRYPTO_ASSETS = {"BTC", "ETH", "SOL"}


def _fetch_prices() -> pd.DataFrame:
    import yfinance as yf

    all_tickers: list[str] = []
    for assets in ASSETS.values():
        for a in assets:
            all_tickers.append(a["yf"])

    ticker_str = " ".join(all_tickers)
    raw = yf.download(
        ticker_str, period="2y", interval="1d",
        auto_adjust=True, progress=False, group_by="ticker",
    )
    if raw.empty:
        return pd.DataFrame()

    frames: dict[str, pd.Series] = {}
    if len(all_tickers) == 1:
        asset = ASSETS[next(iter(ASSETS.keys()))][0]
        try:
            frames[asset["symbol"]] = raw[("Close", asset["yf"])].dropna()
        except (KeyError, TypeError):
            pass
    else:
        for assets in ASSETS.values():
            for a in assets:
                try:
                    col = raw[a["yf"]]["Close"]
                    s = col.dropna() if hasattr(col, "dropna") else None
                    if s is not None and not s.empty:
                        frames[a["symbol"]] = s
                except (KeyError, TypeError):
                    continue

    if not frames:
        return pd.DataFrame()

    panel = pd.DataFrame(frames).sort_index()
    panel.index = pd.to_datetime(panel.index).tz_localize(None).normalize()
    panel = panel.groupby(level=0).last()
    return panel


def _compute_metrics(prices: pd.Series) -> dict[str, float | None]:
    prices = prices.dropna()
    if len(prices) < 30:
        return {
            "price": float(prices.iloc[-1]) if len(prices) > 0 else None,
            "returns": {},
            "sharpe": None,
            "volatility": None,
            "max_drawdown": None,
        }

    daily_ret = prices.pct_change().dropna()
    current = float(prices.iloc[-1])

    def trailing_return(days: int) -> float | None:
        if len(prices) <= days:
            return None
        start = float(prices.iloc[-days - 1])
        if start <= 0:
            return None
        return round(((current / start) - 1) * 100, 2)

    ann_vol = float(daily_ret.std() * np.sqrt(365) * 100)
    ann_ret = float(daily_ret.mean() * 365 * 100)
    sharpe = round(ann_ret / ann_vol, 2) if ann_vol > 0 else None

    cumulative = (1 + daily_ret).cumprod()
    peak = cumulative.expanding().max()
    drawdown = (cumulative - peak) / peak
    max_dd = round(float(drawdown.min()) * 100, 2)

    return {
        "price": round(current, 2),
        "returns": {
            "7d": trailing_return(7),
            "30d": trailing_return(30),
            "90d": trailing_return(90),
            "365d": trailing_return(365),
        },
        "sharpe": sharpe,
        "volatility": round(ann_vol, 1),
        "max_drawdown": max_dd,
    }


def _correlation_to_market(asset: pd.Series, market: pd.Series) -> float | None:
    returns = pd.concat(
        [asset.pct_change(fill_method=None), market.pct_change(fill_method=None)],
        axis=1,
        join="inner",
    ).dropna()
    if len(returns) < 30:
        return None
    correlation = float(returns.iloc[:, 0].corr(returns.iloc[:, 1]))
    if not np.isfinite(correlation):
        return None
    return round(correlation, 2)


def _risk_tier(volatility: float | None) -> str:
    if volatility is None or not np.isfinite(volatility):
        return "unrated"
    if volatility < 10:
        return "low"
    if volatility < 30:
        return "medium"
    return "high"


def _market_behavior(correlation: float | None) -> str:
    if correlation is None or not np.isfinite(correlation):
        return "unrated"
    if correlation <= -0.15:
        return "counterweight"
    if correlation < 0.35:
        return "diversifier"
    return "risk_on"


async def _funding_rates(pool, audience) -> dict[str, float | None]:
    rows = await pool.fetch(
        f"""
        SELECT split_part(split_part(c.key, ':', 2), '/', 1) AS asset,
               avg((c.value ->> 'rate')::numeric) AS mean_rate
        FROM ({visible_claims_cte("$1")}) c
        WHERE c.claim_type = 'funding_rate'
          AND split_part(c.key, ':', 1) = 'hyperliquid'
          AND c.event_date > now() - interval '7 days'
        GROUP BY 1
        """,
        audience,
    )
    out: dict[str, float | None] = {}
    for r in rows:
        asset = r["asset"]
        if asset in CRYPTO_ASSETS and r["mean_rate"] is not None:
            rate = float(r["mean_rate"])
            out[asset] = round(rate * 24 * 365 * 100, 2)
    return out


def _price(value: Any) -> float | None:
    if isinstance(value, (str, bytes)):
        import json

        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    raw = value.get("close", value.get("price"))
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _sector_leader_payload(rows: list[Any]) -> list[dict]:
    """Rank companies within each sector from visible 30-session histories."""
    histories: dict[tuple[str, str, str, str], list[tuple[Any, Any]]] = {}
    for row in rows:
        key = (
            row["sector_symbol"],
            row["sector_name"],
            row["symbol"],
            row["name"],
        )
        histories.setdefault(key, []).append((row["event_date"], row["value"]))

    ranked: dict[tuple[str, str], list[dict]] = {}
    for (sector_symbol, sector_name, symbol, name), observations in histories.items():
        valid = [
            (event_date, price)
            for event_date, value in observations
            if (price := _price(value)) is not None
        ]
        valid.sort(key=lambda item: item[0], reverse=True)
        if len(valid) <= SECTOR_RETURN_WINDOW:
            continue
        latest_date, latest = valid[0]
        _, start = valid[SECTOR_RETURN_WINDOW]
        sector_key = (sector_symbol, sector_name)
        ranked.setdefault(sector_key, []).append({
            "symbol": symbol,
            "name": name,
            "return_30d": round((latest / start - 1) * 100, 2),
            "as_of": latest_date.isoformat(),
        })

    sectors: list[dict] = []
    for (sector_symbol, sector_name), companies in ranked.items():
        companies.sort(key=lambda company: company["return_30d"], reverse=True)
        sectors.append({
            "name": sector_name,
            "symbol": sector_symbol,
            "coverage": len(companies),
            "leaders": companies[:SECTOR_LEADER_COUNT],
        })
    sectors.sort(key=lambda sector: sector["name"])
    return sectors


def _overall_leaders(sectors: list[dict]) -> list[dict]:
    companies = [
        {
            **leader,
            "sector": sector["name"],
            "sector_symbol": sector["symbol"],
        }
        for sector in sectors
        for leader in sector["leaders"]
    ]
    companies.sort(key=lambda company: company["return_30d"], reverse=True)
    return companies[:OVERALL_LEADER_COUNT]


async def _sector_leaders(pool, audience) -> list[dict]:
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$1")}
        ), observations AS (
            SELECT DISTINCT ON (c.entity_id, c.event_date)
                   company.symbol, company.name,
                   sector.symbol AS sector_symbol,
                   COALESCE(sector.identifiers ->> 'gics_sector', sector.name)
                       AS sector_name,
                   c.event_date, c.knowledge_date, c.value
            FROM visible c
            JOIN entity company ON company.id = c.entity_id
            JOIN entity_edge edge
              ON edge.from_entity = company.id
             AND edge.relation = 'member_of_sector'
            JOIN entity sector
              ON sector.id = edge.to_entity
             AND sector.kind = 'sector_etf'
            WHERE company.kind = 'company'
              AND c.claim_type = 'price_snapshot'
            ORDER BY c.entity_id, c.event_date, c.knowledge_date DESC
        ), ranked AS (
            SELECT *, row_number() OVER (
                PARTITION BY symbol ORDER BY event_date DESC
            ) AS observation_rank
            FROM observations
        )
        SELECT symbol, name, sector_symbol, sector_name, event_date, value
        FROM ranked
        WHERE observation_rank <= {SECTOR_RETURN_WINDOW + 1}
        ORDER BY sector_name, symbol, event_date DESC
        """,
        audience,
    )
    return _sector_leader_payload(rows)


def _payload(buckets_data: list[dict], sectors: list[dict]) -> dict:
    return {
        "buckets": buckets_data,
        "sectors": sectors,
        "overall_leaders": _overall_leaders(sectors),
        "sector_coverage": {
            "available": len(sectors),
            "total": 11,
            "window_sessions": SECTOR_RETURN_WINDOW,
        },
        "as_of": datetime.now(UTC).isoformat(),
    }


async def _build_scanner(app: App, audience) -> dict:
    now = time.time()
    cache_key = str(audience) if audience is not None else "shared"
    cached = _cache.get(cache_key)
    if cached is not None and now - cached["ts"] < CACHE_TTL:
        return cached["data"]

    prices = _fetch_prices()
    funding = await _funding_rates(app.db.pool, audience)
    sectors = await _sector_leaders(app.db.pool, audience)

    buckets_data: list[dict] = []
    for bucket_name, assets in ASSETS.items():
        bucket_assets: list[dict] = []
        for a in assets:
            symbol = a["symbol"]
            if symbol not in prices.columns:
                continue
            metrics = _compute_metrics(prices[symbol])
            correlation = (
                _correlation_to_market(prices[symbol], prices["SPY"])
                if "SPY" in prices.columns
                else None
            )
            entry: dict[str, Any] = {
                "symbol": symbol,
                "name": a["name"],
                "asset_class": a["asset_class"],
                "risk_tier": _risk_tier(metrics["volatility"]),
                "correlation_to_spy": correlation,
                "market_behavior": _market_behavior(correlation),
                **metrics,
            }
            if symbol in CRYPTO_ASSETS:
                entry["funding_apr"] = funding.get(symbol)
            else:
                entry["funding_apr"] = None
            bucket_assets.append(entry)

        bucket_assets.sort(
            key=lambda x: (x.get("returns", {}).get("90d") or -999),
            reverse=True,
        )
        buckets_data.append({
            "name": bucket_name,
            "role": BUCKET_ROLES.get(bucket_name, ""),
            "assets": bucket_assets,
        })

    payload = _payload(buckets_data, sectors)
    _cache[cache_key] = {"data": payload, "ts": now}
    return payload


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/scanner/market")
    async def scanner_market(request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        return await _build_scanner(app, audience)

    return router


__all__ = ["build_router"]
