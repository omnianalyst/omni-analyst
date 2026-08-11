"""Cross-asset market scanner: the five-bucket dashboard.

Shows every tracked asset across the five portfolio buckets (growth, debasement,
deflation, safety, alpha) with trailing returns, risk metrics, and for crypto
the current funding rate that the carry book ranks on.

Price data comes from yfinance (display-only, never ingested as claims — same
pattern as the research probes). Funding rates come from the claim store.
Results are cached for 1 hour to keep the endpoint fast.
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
_cache: dict[str, Any] = {"data": None, "ts": 0.0}

ASSETS: dict[str, list[dict[str, str]]] = {
    "Growth": [
        {"symbol": "VTI", "name": "Vanguard Total Stock Market", "yf": "VTI"},
        {"symbol": "SPY", "name": "S&P 500 ETF", "yf": "SPY"},
        {"symbol": "QQQ", "name": "Invesco QQQ Trust", "yf": "QQQ"},
        {"symbol": "VXUS", "name": "Vanguard Total International", "yf": "VXUS"},
    ],
    "Debasement": [
        {"symbol": "GLD", "name": "SPDR Gold Shares", "yf": "GLD"},
        {"symbol": "SLV", "name": "iShares Silver Trust", "yf": "SLV"},
        {"symbol": "BTC", "name": "Bitcoin", "yf": "BTC-USD"},
        {"symbol": "ETH", "name": "Ethereum", "yf": "ETH-USD"},
        {"symbol": "SOL", "name": "Solana", "yf": "SOL-USD"},
    ],
    "Deflation": [
        {"symbol": "TLT", "name": "iShares 20+ Year Treasury", "yf": "TLT"},
    ],
    "Safety": [
        {"symbol": "SHV", "name": "iShares Short Treasury", "yf": "SHV"},
    ],
}

BUCKET_ROLES = {
    "Growth": "Expansion — stocks win when the economy grows",
    "Debasement": "Inflation/stagflation — hard assets win when currency devalues",
    "Deflation": "Falling rates/prices — long bonds win when rates cut",
    "Safety": "Recession/liquidity — cash and T-bills preserve capital",
    "Alpha": "Uncorrelated harvest — carry book collects funding regardless of regime",
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


def _payload(buckets_data: list[dict]) -> dict:
    return {"buckets": buckets_data, "as_of": datetime.now(UTC).isoformat()}


async def _build_scanner(app: App, audience) -> dict:
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    prices = _fetch_prices()
    funding = await _funding_rates(app.db.pool, audience)

    buckets_data: list[dict] = []
    for bucket_name, assets in ASSETS.items():
        bucket_assets: list[dict] = []
        for a in assets:
            symbol = a["symbol"]
            if symbol not in prices.columns:
                continue
            metrics = _compute_metrics(prices[symbol])
            entry: dict[str, Any] = {
                "symbol": symbol,
                "name": a["name"],
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

    carry_pairs = []
    for symbol in ["ETH", "SOL", "BTC"]:
        if symbol in funding and funding[symbol] is not None:
            carry_pairs.append({
                "symbol": symbol,
                "funding_apr": funding[symbol],
            })
    carry_pairs.sort(key=lambda x: x["funding_apr"], reverse=True)
    buckets_data.append({
        "name": "Alpha",
        "role": BUCKET_ROLES["Alpha"],
        "assets": carry_pairs,
    })

    payload = _payload(buckets_data)
    _cache["data"] = payload
    _cache["ts"] = now
    return payload


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/scanner/market")
    async def scanner_market(request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        return await _build_scanner(app, audience)

    return router


__all__ = ["build_router"]
