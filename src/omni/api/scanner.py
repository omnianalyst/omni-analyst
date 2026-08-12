"""Cross-asset market scanner: durable rankings plus regime context.

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

import httpx
import numpy as np
import pandas as pd
from neutron import App, Router
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.coverage.visibility import visible_claims_cte
from omni.market_universe import (
    CRYPTO_REGISTRY,
    MIN_CRYPTO_OBSERVATIONS,
    POLICY_VERSION,
    crypto_assets,
    evaluate_crypto_census,
)

CACHE_TTL = 3600
SECTOR_RETURN_WINDOW = 30
SECTOR_LEADER_COUNT = 15
OVERALL_LEADER_COUNT = 15
_cache: dict[str, dict[str, Any]] = {}

ASSETS: dict[str, list[dict[str, str]]] = {
    "Growth": [
        {"symbol": "VTI", "name": "Vanguard Total Stock Market", "yf": "VTI",
         "asset_class": "stocks", "area": "US broad market"},
        {"symbol": "SPY", "name": "S&P 500 ETF", "yf": "SPY",
         "asset_class": "stocks", "area": "US broad market"},
        {"symbol": "QQQ", "name": "Invesco QQQ Trust", "yf": "QQQ",
         "asset_class": "stocks", "area": "US broad market"},
        {"symbol": "DIA", "name": "Dow Jones Industrial Average", "yf": "DIA",
         "asset_class": "stocks", "area": "US broad market"},
        {"symbol": "IWM", "name": "Russell 2000 ETF", "yf": "IWM",
         "asset_class": "stocks", "area": "US broad market"},
        {"symbol": "VO", "name": "Vanguard Mid-Cap ETF", "yf": "VO",
         "asset_class": "stocks", "area": "US broad market"},
        {"symbol": "VT", "name": "Vanguard Total World Stock", "yf": "VT",
         "asset_class": "stocks", "area": "Global market"},
        {"symbol": "VUG", "name": "Vanguard Growth ETF", "yf": "VUG",
         "asset_class": "stocks", "area": "Investment style"},
        {"symbol": "VTV", "name": "Vanguard Value ETF", "yf": "VTV",
         "asset_class": "stocks", "area": "Investment style"},
        {"symbol": "QUAL", "name": "iShares MSCI USA Quality", "yf": "QUAL",
         "asset_class": "stocks", "area": "Investment style"},
        {"symbol": "USMV", "name": "iShares MSCI USA Min Vol", "yf": "USMV",
         "asset_class": "stocks", "area": "Investment style"},
        {"symbol": "MTUM", "name": "iShares MSCI USA Momentum", "yf": "MTUM",
         "asset_class": "stocks", "area": "Investment style"},
        {"symbol": "DGRO", "name": "iShares Core Dividend Growth", "yf": "DGRO",
         "asset_class": "stocks", "area": "Investment style"},
        {"symbol": "VXUS", "name": "Vanguard Total International", "yf": "VXUS",
         "asset_class": "stocks", "area": "International"},
        {"symbol": "VEA", "name": "Vanguard Developed Markets", "yf": "VEA",
         "asset_class": "stocks", "area": "International"},
        {"symbol": "VWO", "name": "Vanguard Emerging Markets", "yf": "VWO",
         "asset_class": "stocks", "area": "International"},
        {"symbol": "VNQ", "name": "Vanguard Real Estate ETF", "yf": "VNQ",
         "asset_class": "stocks", "area": "Real estate"},
        {"symbol": "XLK", "name": "Technology Select Sector", "yf": "XLK", "asset_class": "stocks", "area": "Sector"},
        {"symbol": "XLF", "name": "Financial Select Sector", "yf": "XLF", "asset_class": "stocks", "area": "Sector"},
        {"symbol": "XLV", "name": "Health Care Select Sector", "yf": "XLV", "asset_class": "stocks", "area": "Sector"},
        {"symbol": "XLI", "name": "Industrial Select Sector", "yf": "XLI", "asset_class": "stocks", "area": "Sector"},
        {"symbol": "XLE", "name": "Energy Select Sector", "yf": "XLE", "asset_class": "stocks", "area": "Sector"},
        {"symbol": "XLY", "name": "Consumer Discretionary Sector", "yf": "XLY", "asset_class": "stocks", "area": "Sector"},
        {"symbol": "XLP", "name": "Consumer Staples Sector", "yf": "XLP", "asset_class": "stocks", "area": "Sector"},
        {"symbol": "XLU", "name": "Utilities Select Sector", "yf": "XLU", "asset_class": "stocks", "area": "Sector"},
        {"symbol": "XLB", "name": "Materials Select Sector", "yf": "XLB", "asset_class": "stocks", "area": "Sector"},
        {"symbol": "XLC", "name": "Communication Services Sector", "yf": "XLC", "asset_class": "stocks", "area": "Sector"},
        {"symbol": "XLRE", "name": "Real Estate Select Sector", "yf": "XLRE", "asset_class": "stocks", "area": "Sector"},
    ],
    "Debasement": [
        {"symbol": "GLD", "name": "SPDR Gold Shares", "yf": "GLD",
         "asset_class": "defensive", "area": "Precious metals"},
        {"symbol": "SLV", "name": "iShares Silver Trust", "yf": "SLV",
         "asset_class": "defensive", "area": "Precious metals"},
        {"symbol": "DBC", "name": "Invesco DB Commodity Index", "yf": "DBC",
         "asset_class": "defensive", "area": "Commodities"},
        *crypto_assets(),
    ],
    "Deflation": [
        {"symbol": "TLT", "name": "iShares 20+ Year Treasury", "yf": "TLT",
         "asset_class": "defensive", "area": "Treasuries"},
        {"symbol": "IEF", "name": "iShares 7-10 Year Treasury", "yf": "IEF", "asset_class": "defensive", "area": "Treasuries"},
        {"symbol": "TIP", "name": "iShares TIPS Bond ETF", "yf": "TIP", "asset_class": "defensive", "area": "Inflation-linked bonds"},
        {"symbol": "BND", "name": "Vanguard Total Bond Market", "yf": "BND", "asset_class": "defensive", "area": "Broad bonds"},
        {"symbol": "BNDX", "name": "Vanguard Total International Bond", "yf": "BNDX", "asset_class": "defensive", "area": "International bonds"},
        {"symbol": "LQD", "name": "iShares Investment Grade Corporate Bond", "yf": "LQD", "asset_class": "defensive", "area": "Corporate credit"},
        {"symbol": "HYG", "name": "iShares High Yield Corporate Bond", "yf": "HYG", "asset_class": "defensive", "area": "High-yield credit"},
    ],
    "Safety": [
        {"symbol": "SHV", "name": "iShares Short Treasury", "yf": "SHV",
         "asset_class": "defensive", "area": "Treasuries"},
        {"symbol": "SGOV", "name": "iShares 0-3 Month Treasury", "yf": "SGOV",
         "asset_class": "defensive", "area": "Cash equivalent"},
    ],
}

BUCKET_ROLES = {
    "Growth": "Expansion — stocks win when the economy grows",
    "Debasement": "Inflation/stagflation — hard assets win when currency devalues",
    "Deflation": "Falling rates/prices — long bonds win when rates cut",
    "Safety": "Recession/liquidity — cash and T-bills preserve capital",
}

CRYPTO_ASSETS = {asset["symbol"] for asset in ASSETS["Debasement"] if asset["asset_class"] == "crypto"}
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"


async def _crypto_census() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                COINGECKO_MARKETS_URL,
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 60,
                    "page": 1,
                    "sparkline": "false",
                },
            )
            response.raise_for_status()
            rows = response.json()
        if not isinstance(rows, list):
            raise TypeError("CoinGecko market census was not a list")
        return {**evaluate_crypto_census(rows), "source": "live", "live": True}
    except (httpx.HTTPError, TypeError, ValueError):
        return {
            "policy_version": POLICY_VERSION,
            "market_cap_limit": 60,
            "included": [
                {
                    "rank": None,
                    "symbol": metadata["symbol"],
                    "name": metadata["name"],
                    "coin_id": coin_id,
                    "registered_symbol": metadata["symbol"],
                }
                for coin_id, metadata in CRYPTO_REGISTRY.items()
            ],
            "excluded": [],
            "unmapped": [],
            "source": "registry fallback",
            "live": False,
        }


def _fetch_prices() -> pd.DataFrame:
    import yfinance as yf

    all_tickers: list[str] = []
    for assets in ASSETS.values():
        for a in assets:
            all_tickers.append(a["yf"])

    ticker_str = " ".join(all_tickers)
    raw = yf.download(
        ticker_str, period="10y", interval="1d",
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


def _compute_metrics(prices: pd.Series, asset_class: str = "stocks") -> dict[str, Any]:
    prices = prices.dropna()
    prices = prices[np.isfinite(prices.astype(float)) & (prices.astype(float) > 0)]
    if len(prices) < 30:
        return {
            "price": float(prices.iloc[-1]) if len(prices) > 0 else None,
            "returns": {},
            "sharpe": None,
            "volatility": None,
            "max_drawdown": None,
            "cagr_5y": None,
            "cagr_10y": None,
            "median_annual_return": None,
            "positive_year_rate": None,
            "history_years": 0,
            "complete_years": 0,
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

    sessions = 365 if asset_class == "crypto" else 252
    ann_vol = float(daily_ret.std() * np.sqrt(sessions) * 100)
    ann_ret = float(daily_ret.mean() * sessions * 100)
    sharpe = round(ann_ret / ann_vol, 2) if ann_vol > 0 else None

    cumulative = (1 + daily_ret).cumprod()
    peak = cumulative.expanding().max()
    drawdown = (cumulative - peak) / peak
    max_dd = round(float(drawdown.min()) * 100, 2)

    elapsed_years = max((prices.index[-1] - prices.index[0]).days / 365.2425, 0)

    def cagr(years: int) -> float | None:
        target = prices.index[-1] - pd.DateOffset(years=years)
        eligible = prices[prices.index <= target]
        if eligible.empty and elapsed_years >= years * 0.98:
            eligible = prices.iloc[:1]
        if eligible.empty or elapsed_years < years * 0.95:
            return None
        start = float(eligible.iloc[-1])
        actual_years = (prices.index[-1] - eligible.index[-1]).days / 365.2425
        if start <= 0 or actual_years <= 0:
            return None
        return round(((current / start) ** (1 / actual_years) - 1) * 100, 2)

    year_ends = prices.resample("YE").last()
    annual = year_ends.pct_change(fill_method=None).dropna() * 100
    annual = annual[annual.index.year < prices.index[-1].year]

    return {
        "price": round(current, 2),
        "returns": {
            "7d": trailing_return(7),
            "30d": trailing_return(30),
            "90d": trailing_return(90),
            "365d": trailing_return(sessions),
        },
        "sharpe": sharpe,
        "volatility": round(ann_vol, 1),
        "max_drawdown": max_dd,
        "cagr_5y": cagr(5),
        "cagr_10y": cagr(10),
        "median_annual_return": round(float(annual.median()), 2) if len(annual) else None,
        "positive_year_rate": round(float((annual > 0).mean() * 100), 1) if len(annual) else None,
        "history_years": round(elapsed_years, 1),
        "complete_years": len(annual),
    }


def _percentile_scores(entries: list[dict], field: str, *, inverse: bool = False) -> dict[str, float]:
    available = [(entry["symbol"], entry.get(field)) for entry in entries]
    available = [(symbol, float(value)) for symbol, value in available if value is not None and np.isfinite(value)]
    if not available:
        return {}
    values = pd.Series({symbol: value for symbol, value in available})
    scores = values.rank(pct=True, method="average") * 100
    if inverse:
        scores = 100 - scores + (100 / len(scores))
    return {symbol: round(float(score), 1) for symbol, score in scores.items()}


def _score_assets(entries: list[dict]) -> None:
    components = {
        "return_1y": _percentile_scores(entries, "return_1y"),
        "cagr_5y": _percentile_scores(entries, "cagr_5y"),
        "cagr_10y": _percentile_scores(entries, "cagr_10y"),
        "median": _percentile_scores(entries, "median_annual_return"),
        "positive_years": _percentile_scores(entries, "positive_year_rate"),
        "volatility": _percentile_scores(entries, "volatility", inverse=True),
        "drawdown": _percentile_scores(entries, "max_drawdown"),
        "diversification": _percentile_scores(entries, "correlation_to_spy", inverse=True),
    }
    for entry in entries:
        symbol = entry["symbol"]

        def average(names: tuple[str, ...], asset_symbol: str = symbol) -> float | None:
            values = [
                components[name][asset_symbol]
                for name in names
                if asset_symbol in components[name]
            ]
            return round(sum(values) / len(values), 1) if values else None

        has_long_enough_record = entry.get("complete_years", 0) >= 3
        durable = average(("cagr_5y", "cagr_10y", "median")) if has_long_enough_record else None
        consistency = average(("median", "positive_years")) if has_long_enough_record else None
        stability = average(("volatility", "drawdown"))
        diversification = average(("diversification",))
        recent = average(("return_1y",))
        weighted = [(durable, 0.35), (consistency, 0.25), (stability, 0.20), (recent, 0.10), (diversification, 0.10)]
        present = [(value, weight) for value, weight in weighted if value is not None]
        overall = sum(value * weight for value, weight in present) / sum(weight for _, weight in present) if present else None
        entry["scores"] = {
            "balanced": round(overall, 1) if overall is not None else None,
            "durable_growth": durable,
            "consistency": consistency,
            "stability": stability,
            "diversification": diversification,
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
            "return_window": round((latest / start - 1) * 100, 2),
            "as_of": latest_date.isoformat(),
        })

    sectors: list[dict] = []
    for (sector_symbol, sector_name), companies in ranked.items():
        companies.sort(key=lambda company: company["return_window"], reverse=True)
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
    companies.sort(key=lambda company: company["return_window"], reverse=True)
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


def _payload(buckets_data: list[dict], sectors: list[dict], coverage: dict[str, Any]) -> dict:
    assets = [asset for bucket in buckets_data for asset in bucket["assets"]]

    def category_ranked(asset_class: str) -> list[dict]:
        available = [
            asset
            for asset in assets
            if asset["asset_class"] == asset_class
            and asset.get("scores", {}).get("balanced") is not None
        ]
        available.sort(key=lambda asset: asset["scores"]["balanced"], reverse=True)
        return available

    return {
        "buckets": buckets_data,
        "category_rankings": {
            "stocks": category_ranked("stocks"),
            "defensive": category_ranked("defensive"),
            "crypto": category_ranked("crypto"),
        },
        "ranking_method": {
            "balanced": "Within each category: 35% durable growth, 25% consistency, 20% stability, 10% one-year return, and 10% diversification; available measures are reweighted when history is shorter.",
            "history": "One-year return is trailing. Five- and ten-year figures are annualized. Median return uses complete calendar years; long-term and consistency ranks require at least three complete years.",
            "scope": "Scores are percentile ranks against the other assets in the same category, not forecasts or recommendations.",
        },
        "sectors": sectors,
        "overall_leaders": _overall_leaders(sectors) if len(sectors) == 11 else [],
        "sector_coverage": {
            "available": len(sectors),
            "total": 11,
            "window_sessions": SECTOR_RETURN_WINDOW,
        },
        "coverage": coverage,
        "as_of": datetime.now(UTC).isoformat(),
    }


async def _build_scanner(app: App, audience) -> dict:
    now = time.time()
    cache_key = str(audience) if audience is not None else "shared"
    cached = _cache.get(cache_key)
    if cached is not None and now - cached["ts"] < CACHE_TTL:
        return cached["data"]

    crypto_census = await _crypto_census()
    eligible_crypto = {
        item["registered_symbol"]: item
        for item in crypto_census["included"]
    }
    prices = _fetch_prices()
    funding = await _funding_rates(app.db.pool, audience)
    sectors = await _sector_leaders(app.db.pool, audience)

    buckets_data: list[dict] = []
    all_entries: list[dict] = []
    unavailable_assets: list[str] = []
    insufficient_crypto: list[dict[str, Any]] = []
    for bucket_name, assets in ASSETS.items():
        bucket_assets: list[dict] = []
        for a in assets:
            symbol = a["symbol"]
            if a["asset_class"] == "crypto" and symbol not in eligible_crypto:
                continue
            if symbol not in prices.columns:
                unavailable_assets.append(symbol)
                continue
            observations = int(prices[symbol].dropna().shape[0])
            if a["asset_class"] == "crypto" and observations < MIN_CRYPTO_OBSERVATIONS:
                insufficient_crypto.append({
                    "symbol": symbol,
                    "observations": observations,
                    "required": MIN_CRYPTO_OBSERVATIONS,
                })
                continue
            metrics = _compute_metrics(prices[symbol], a["asset_class"])
            correlation = (
                _correlation_to_market(prices[symbol], prices["SPY"])
                if "SPY" in prices.columns
                else None
            )
            entry: dict[str, Any] = {
                "symbol": symbol,
                "name": a["name"],
                "asset_class": a["asset_class"],
                "area": a["area"],
                "risk_tier": _risk_tier(metrics["volatility"]),
                "correlation_to_spy": correlation,
                "market_behavior": _market_behavior(correlation),
                **metrics,
            }
            if a["asset_class"] == "crypto":
                entry["market_cap_rank"] = eligible_crypto[symbol].get("rank")
            entry["return_1y"] = metrics.get("returns", {}).get("365d")
            if symbol in CRYPTO_ASSETS:
                entry["funding_apr"] = funding.get(symbol)
            else:
                entry["funding_apr"] = None
            bucket_assets.append(entry)
            all_entries.append(entry)

        bucket_assets.sort(
            key=lambda x: (x.get("returns", {}).get("90d") or -999),
            reverse=True,
        )
        buckets_data.append({
            "name": bucket_name,
            "role": BUCKET_ROLES.get(bucket_name, ""),
            "assets": bucket_assets,
        })

    for asset_class in ("stocks", "defensive", "crypto"):
        _score_assets([entry for entry in all_entries if entry["asset_class"] == asset_class])
    for bucket in buckets_data:
        bucket["assets"].sort(
            key=lambda asset: asset.get("scores", {}).get("balanced") or -1,
            reverse=True,
        )

    ranked_crypto = [entry for entry in all_entries if entry["asset_class"] == "crypto"]
    company_complete = len(sectors) == 11
    crypto_complete = (
        crypto_census["live"]
        and not crypto_census["unmapped"]
        and not insufficient_crypto
    )
    coverage = {
        "policy_version": POLICY_VERSION,
        "complete": company_complete and crypto_complete and not unavailable_assets,
        "crypto": {
            "source": crypto_census["source"],
            "live": crypto_census["live"],
            "market_cap_limit": crypto_census["market_cap_limit"],
            "ranked": len(ranked_crypto),
            "excluded": crypto_census["excluded"],
            "unmapped": crypto_census["unmapped"],
            "insufficient_history": insufficient_crypto,
        },
        "broad_assets": {
            "configured": sum(len(items) for items in ASSETS.values()),
            "ranked": len(all_entries),
            "unavailable": sorted(set(unavailable_assets)),
        },
        "companies": {
            "sectors_measured": len(sectors),
            "sectors_required": 11,
            "complete": company_complete,
        },
        "industries": {
            "complete": False,
            "reason": "Verified GICS industry metadata is not yet stored.",
        },
    }
    payload = _payload(buckets_data, sectors, coverage)
    # A provider/DNS outage must not pin an empty market to the one-hour cache.
    # The honest empty response can be shown once, then the next request retries.
    if not prices.empty:
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
