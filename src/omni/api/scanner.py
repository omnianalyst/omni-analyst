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

# Risk tier cuts, in annualised volatility percent.
#
# Worth knowing when reading a tier census: a universe of diversified funds
# cannot reach `high`. Measured 2026-08-12 over two years, the most volatile of
# the 28 ranked broad stock ETFs was XLK at 27.1%, and the only broad asset of
# any class above 30% was SLV at 47.8%. That is a property of diversification,
# not of the threshold -- individual companies routinely clear it.
RISK_TIER_LOW_MAX = 10.0
RISK_TIER_MEDIUM_MAX = 30.0

# Annualised volatility percent below which a Sharpe ratio is not reported.
# Cash equivalents sit near 0.2%, where the ratio is dominated by the
# denominator's noise rather than by any risk-adjusted skill.
MIN_SHARPE_VOLATILITY = 1.0

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
        {"symbol": "XLRE", "name": "Real Estate Select Sector", "yf": "XLRE",
         "asset_class": "stocks", "area": "Sector"},
        # High-yield credit sits in Growth, not Deflation, on the system's own
        # measurement: HYG's correlation to SPY is risk_on (>= 0.35), and in the
        # deflationary recessions the Deflation bucket exists for (2008), credit
        # spreads widened into a ~35% drawdown -- it behaves like equity risk
        # with a coupon, not like duration. It still scans, ranks, and appears
        # in the Defensive tables; it just no longer competes to represent
        # falling rates.
        {"symbol": "HYG", "name": "iShares High Yield Corporate Bond", "yf": "HYG",
         "asset_class": "defensive", "area": "High-yield credit"},
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

# The sleeve each bucket exists to hold, fixed by policy rather than score.
#
# Picking each bucket's top scorer instead (XLK led Growth on 2026-08 data) is
# a momentum bet wearing a score's clothes: over a 9-year sample the winner is
# mostly whoever had the best recent run, and hand a novice that year's sector
# leader as "the safe answer" is exactly the behaviour this product exists to
# prevent. Each choice below is the asset the regime is *defined by* in the
# literature (Browne's Permanent Portfolio and Dalio's All-Weather hold the
# same four shapes): the whole market for growth, gold for currency
# devaluation, the longest Treasuries for falling rates, T-bills for safety.
# The scored alternatives still display on every scenario card; only the
# portfolio's pick is policy.
REPRESENTATIVE_ASSETS: dict[str, dict[str, str]] = {
    "Growth": {
        "symbol": "VTI",
        "reason": "the entire US stock market, not a bet on which sector wins",
    },
    "Debasement": {
        "symbol": "GLD",
        "reason": "gold, the hard asset held through every currency devaluation",
    },
    "Deflation": {
        "symbol": "TLT",
        "reason": "20+ year Treasuries, the longest duration and the classic winner of falling rates",
    },
    "Safety": {
        "symbol": "SGOV",
        "reason": "0-3 month T-bills, cash that pays the policy rate with no duration risk",
    },
}


def _representative(bucket_name: str, ranked_symbols: set[str]) -> dict[str, str] | None:
    """The bucket's designated sleeve, when it survived ranking.

    ``None`` when the representative was refused (feed defect) or unranked;
    the caller falls back to the best measured pick rather than naming an
    asset it cannot stand behind.
    """
    designated = REPRESENTATIVE_ASSETS.get(bucket_name)
    if designated is None or designated["symbol"] not in ranked_symbols:
        return None
    return designated


# Approximate income and cost figures, from fund sponsors' latest published
# pages. They drift with rates and distributions, so they carry their date and
# are labelled approximate wherever shown; a veteran checks the fund page
# before acting, and a novice should not be shown a number pretending to be
# exact. Crypto holdings carry no entry: no distribution, no expense ratio --
# the exchange spread and funding are theirs, and are measured elsewhere.
INCOME_AND_COST: dict[str, dict[str, float]] = {
    "VTI": {"yield_pct": 1.3, "expense_ratio_pct": 0.03},
    "SPY": {"yield_pct": 1.2, "expense_ratio_pct": 0.09},
    "QQQ": {"yield_pct": 0.6, "expense_ratio_pct": 0.20},
    "DIA": {"yield_pct": 1.6, "expense_ratio_pct": 0.16},
    "IWM": {"yield_pct": 1.2, "expense_ratio_pct": 0.19},
    "VO": {"yield_pct": 1.4, "expense_ratio_pct": 0.04},
    "VT": {"yield_pct": 1.9, "expense_ratio_pct": 0.07},
    "VUG": {"yield_pct": 0.7, "expense_ratio_pct": 0.04},
    "VTV": {"yield_pct": 2.2, "expense_ratio_pct": 0.04},
    "QUAL": {"yield_pct": 0.7, "expense_ratio_pct": 0.15},
    "MTUM": {"yield_pct": 0.6, "expense_ratio_pct": 0.15},
    "USMV": {"yield_pct": 1.7, "expense_ratio_pct": 0.15},
    "DGRO": {"yield_pct": 1.8, "expense_ratio_pct": 0.08},
    "VEA": {"yield_pct": 3.0, "expense_ratio_pct": 0.05},
    "VWO": {"yield_pct": 2.9, "expense_ratio_pct": 0.07},
    "VXUS": {"yield_pct": 3.0, "expense_ratio_pct": 0.05},
    "XLK": {"yield_pct": 0.6, "expense_ratio_pct": 0.08},
    "XLF": {"yield_pct": 1.5, "expense_ratio_pct": 0.08},
    "XLV": {"yield_pct": 1.5, "expense_ratio_pct": 0.08},
    "XLI": {"yield_pct": 1.3, "expense_ratio_pct": 0.08},
    "XLE": {"yield_pct": 3.2, "expense_ratio_pct": 0.08},
    "XLY": {"yield_pct": 0.8, "expense_ratio_pct": 0.08},
    "XLP": {"yield_pct": 2.1, "expense_ratio_pct": 0.08},
    "XLU": {"yield_pct": 2.7, "expense_ratio_pct": 0.08},
    "XLB": {"yield_pct": 1.9, "expense_ratio_pct": 0.08},
    "XLC": {"yield_pct": 1.1, "expense_ratio_pct": 0.08},
    "XLRE": {"yield_pct": 3.5, "expense_ratio_pct": 0.08},
    "VNQ": {"yield_pct": 3.9, "expense_ratio_pct": 0.12},
    "GLD": {"yield_pct": 0.0, "expense_ratio_pct": 0.40},
    "SLV": {"yield_pct": 0.0, "expense_ratio_pct": 0.50},
    "DBC": {"yield_pct": 0.0, "expense_ratio_pct": 0.87},
    "TLT": {"yield_pct": 3.9, "expense_ratio_pct": 0.15},
    "IEF": {"yield_pct": 3.6, "expense_ratio_pct": 0.15},
    "TIP": {"yield_pct": 3.1, "expense_ratio_pct": 0.19},
    "BND": {"yield_pct": 4.3, "expense_ratio_pct": 0.03},
    "BNDX": {"yield_pct": 3.5, "expense_ratio_pct": 0.07},
    "LQD": {"yield_pct": 4.5, "expense_ratio_pct": 0.14},
    "HYG": {"yield_pct": 5.9, "expense_ratio_pct": 0.49},
    "SHV": {"yield_pct": 4.8, "expense_ratio_pct": 0.12},
    "SGOV": {"yield_pct": 4.9, "expense_ratio_pct": 0.09},
}
INCOME_AND_COST_AS_OF = "2026-08"

# The risk/return decision table: measured over 1971-2023 (630 months),
# annually rebalanced, from the system's own ingested series (Shiller S&P
# total return, World Bank Pink Sheet gold, FRED 10y/3mo). Static like
# INCOME_AND_COST: research results stamped with their date, not live
# computations. The finding it encodes: the whole risk spectrum costs only
# ~2.6%/yr -- the safe rows keep most of the money. The walk-forward tests
# behind it (optimizer won 1/8 periods; trailing-winner rotation won 4/8 at
# -0.9%/yr) are why the default is the equal split and not a "best" one.
DECISION_TABLE_AS_OF = "2026-08"
DECISION_TABLE: list[dict[str, object]] = [
    {
        "tolerate": "-6%",
        "allocation": "25% each: VTI, GLD, TLT, SGOV",
        "cagr_pct": 8.1,
        "worst_year_pct": -5.9,
    },
    {
        "tolerate": "-12%",
        "allocation": "stocks, gold, cash equal",
        "cagr_pct": 8.6,
        "worst_year_pct": -12.1,
    },
    {
        "tolerate": "-19%",
        "allocation": "60/40 or 50/50 stocks+gold",
        "cagr_pct": 9.3,
        "worst_year_pct": -18.5,
    },
    {
        "tolerate": "-39%",
        "allocation": "all stocks",
        "cagr_pct": 10.7,
        "worst_year_pct": -39.2,
    },
]


def _portfolio_history(prices: pd.DataFrame, symbols: list[str]) -> dict[str, Any] | None:
    """The equal-weight mix of the representatives (see _mix_history)."""
    return _mix_history(prices, [(symbol, 1.0) for symbol in symbols])


def _mix_history(
    prices: pd.DataFrame,
    positions: list[tuple[str, float]],
    window_symbols: list[str] | None = None,
) -> dict[str, Any] | None:
    """A weighted mix of assets, as it actually measured.

    This is a description of the parts assembled, not a forecast: it is what
    holding these at the given weights, rebalanced every January, did over the
    window where all of them have prices. Exact annual rebalancing -- within
    each calendar year every asset is normalised to its first price of that
    year and blended, and the years chain. The window starts at the first
    January after the shortest history begins, so every reported calendar year
    is whole; a partial year would report a real but incomparable number.

    ``window_symbols`` extends the window determination -- used when two mixes
    must be compared over exactly the same dates: the union of both mixes'
    symbols decides the window, and each mix is computed only over its own
    holdings within it.

    ``None`` when any held symbol is missing, so the page shows nothing rather
    than a mix it cannot stand behind.
    """
    symbols = [symbol for symbol, _ in positions]
    total_weight = sum(weight for _, weight in positions)
    if not symbols or total_weight <= 0:
        return None
    weights = [weight / total_weight for _, weight in positions]
    window = list(dict.fromkeys([*symbols, *(window_symbols or [])]))
    if any(symbol not in prices.columns for symbol in window):
        return None
    series = prices[window].dropna()
    if len(series) < 60:
        return None
    # Drop a partial first year so every reported calendar year is whole. A
    # year with under 200 trading days did not span the year (SGOV begins
    # 2020-05; a 20-day January stub is not a year either), so the window
    # starts at the first fully-spanned January.
    first_year = series.index[0].year
    if int((series.index.year == first_year).sum()) < 200:
        series = series[series.index >= pd.Timestamp(year=first_year + 1, month=1, day=1)]
    if len(series) < 60:
        return None
    held = prices[symbols].loc[series.index]

    # A gap in any held series splices the path: dropna() removed the missing
    # dates for everyone, so a crash inside the gap vanishes from the history
    # and the mix reports a smoother ride than any holder experienced. The
    # baseline is each symbol's own observed cadence (a crypto panel runs
    # calendar days, an ETF panel business days) -- not a synthetic calendar,
    # which would flag every weekend as a hole.
    for symbol in symbols:
        observed = prices[symbol].dropna().index
        if len(observed) < 2:
            return None
        gaps = observed.to_series().diff().dt.days.dropna()
        if (gaps > 15).any():
            return None

    # Each year is measured from the previous year's final close, not the
    # current year's first print -- a return realized over new year's belongs
    # to the new year, and normalising to the first print would attribute it
    # to neither.
    chained: list[float] = []
    year_returns: dict[int, float] = {}
    base = held.iloc[0]
    value = 1.0
    for year, year_prices in held.groupby(held.index.year):
        relative = year_prices / base
        mix = (relative * weights).sum(axis=1)
        end = float(mix.iloc[-1])
        chained.extend(value * level for level in mix.tolist())
        value *= end
        # Stored as a percent return, not a growth factor -- a flat year is 0,
        # never 100.
        year_returns[year] = (end - 1.0) * 100
        base = year_prices.iloc[-1]
    path = pd.Series(chained, index=series.index)

    daily = path.pct_change().dropna()
    returns = pd.Series(year_returns)
    drawdown = float((path / path.cummax() - 1).min())
    worst = returns.idxmin()
    best = returns.idxmax()
    return {
        # The monthly path (index, growth of 100): what the mix's own line
        # looks like. Carried so the UI can draw the journey, not just the
        # summary stats -- the summary is the verdict, the path is the story.
        "path": [
            [i, round(float(v), 3)] for i, v in enumerate(path.tolist())
        ],
        "window_start": series.index[0].strftime("%Y-%m"),
        "window_end": series.index[-1].strftime("%Y-%m"),
        "volatility": round(float(daily.std() * np.sqrt(252) * 100), 1),
        "median_year": round(float(returns.median()), 2),
        "worst_year": {"year": str(worst), "return": round(float(returns.loc[worst]), 1)},
        "best_year": {"year": str(best), "return": round(float(returns.loc[best]), 1)},
        "worst_drawdown": round(drawdown * 100, 1),
        "up_years": round(float((returns > 0).mean() * 100), 0),
        "complete_years": len(returns),
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


# A price series from a provider sometimes opens with a broken seed print --
# observed 2026-08-14 on five yfinance crypto feeds (AAVE, TAO, TON, WLD, SHIB):
# a first close one or two orders of magnitude off (AAVE seeded $0.52 against a
# real $53), implying a single-day move of 90x-7000x. No ranked asset has ever
# moved 10x in one calendar day, so a 10x multiple (a +900% daily return) is a
# feed defect, not a market event -- and letting it through priced AAVE at
# 4,207% annualised volatility (true: ~109%) and corrupted every CAGR that
# divides by the start price. The series is truncated to start after the break:
# the seed print is discarded, not corrected to a guessed value.
IMPOSSIBLE_DAILY_RETURN = 9.0


def _drop_broken_seed_prefix(prices: pd.Series) -> pd.Series:
    while len(prices) > 2:
        ret = prices.pct_change().dropna()
        broken = ret[ret.abs() > IMPOSSIBLE_DAILY_RETURN]
        if broken.empty:
            return prices
        prices = prices.loc[broken.index[0]:]
    return prices


# A display feed can rot after its mapping was verified -- measured 2026-08-14,
# TON-USD flipped between a ~$0.5 scale and the real ~$3 market through mid-2025
# and ended on a smooth but wrong ~$0.005 tail. Two runtime checks catch the two
# shapes, and both refuse rather than price the defect:
#
# * a cluster of impossible daily moves. Measured across the whole ranked
#   universe after seed truncation, no legitimate asset has more than ONE
#   daily move beyond 3x -- DOGE's real +355% mania print is the single worst
#   case -- while the broken TON feed carries six. One is a mania print; a
#   cluster is a feed flipping between scales.
# * a current-price disagreement. The latest display close is compared to the
#   same asset's live CoinGecko price (the census already fetches it); a
#   factor-of-4 gap is a wrong instrument or scale, not a market move.
IMPOSSIBLE_MOVE_RETURN = 2.0
MAX_IMPOSSIBLE_MOVES = 1
CENSUS_PRICE_FACTOR = 4.0


def _feed_defect_reasons(series: pd.Series, census_price: float | None) -> list[str]:
    series = series.dropna()
    if len(series) < 2:
        return []
    reasons: list[str] = []
    ret = series.pct_change().dropna()
    wild = int((ret.abs() > IMPOSSIBLE_MOVE_RETURN).sum())
    if wild > MAX_IMPOSSIBLE_MOVES:
        reasons.append(
            f"display feed carries {wild} daily moves beyond 3x; no ranked asset "
            f"has more than one, so the feed is flipping between price scales"
        )
    latest = float(series.iloc[-1])
    if census_price is not None and census_price > 0:
        factor = latest / census_price
        if factor > CENSUS_PRICE_FACTOR or factor < 1 / CENSUS_PRICE_FACTOR:
            reasons.append(
                f"display feed's last close ${latest:.6g} disagrees with the live "
                f"census price ${census_price:.6g} by more than {CENSUS_PRICE_FACTOR:.0f}x"
            )
    return reasons


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
            series = _drop_broken_seed_prefix(raw[("Close", asset["yf"])].dropna())
            if len(series) > 0:
                frames[asset["symbol"]] = series
        except (KeyError, TypeError):
            pass
    else:
        for assets in ASSETS.values():
            for a in assets:
                try:
                    col = raw[a["yf"]]["Close"]
                    s = col.dropna() if hasattr(col, "dropna") else None
                    if s is not None and not s.empty:
                        s = _drop_broken_seed_prefix(s)
                        if not s.empty:
                            frames[a["symbol"]] = s
                except (KeyError, TypeError):
                    continue

    if not frames:
        return pd.DataFrame()

    panel = pd.DataFrame(frames).sort_index()
    panel.index = pd.to_datetime(panel.index).tz_localize(None).normalize()
    panel = panel.groupby(level=0).last()
    return panel


# The price panel is the expensive fetch (the whole measured universe). It is
# cached for an hour beside the payload cache so the custom-comparison
# endpoint does not re-pull 74 tickers per click. An empty panel (provider
# outage) is never cached.
_panel_cache: dict[str, Any] = {"ts": 0.0, "prices": None}


def _panel() -> pd.DataFrame:
    now = time.time()
    cached = _panel_cache["prices"]
    if cached is not None and now - _panel_cache["ts"] < CACHE_TTL:
        return cached
    prices = _fetch_prices()
    if not prices.empty:
        _panel_cache["ts"] = now
        _panel_cache["prices"] = prices
    return prices


_company_cache: dict[str, dict[str, Any]] = {}


async def _company_panel_cached(pool, audience) -> pd.DataFrame:
    """The audience-scoped company panel, cached for an hour beside the other
    scanner caches -- the claim scan is a quarter-million rows and must not
    run per comparator click. An empty result is not cached: it may mean the
    audience is entitled but unlicensed, and a later license change should be
    visible immediately rather than pinned to the hour."""
    key = str(audience) if audience is not None else "anonymous"
    now = time.time()
    cached = _company_cache.get(key)
    if cached is not None and now - cached["ts"] < CACHE_TTL:
        return cached["panel"]
    panel = await _company_panel(pool, audience)
    if not panel.empty:
        _company_cache[key] = {"panel": panel, "ts": now}
    return panel


async def _company_symbols(pool, audience) -> list[tuple[str, str]]:
    """Symbols and names of every company with measured price history that
    clears the comparator's history floor -- the searchable tail of the mix
    comparator's universe."""
    panel = await _company_panel_cached(pool, audience)
    if panel.empty:
        return []
    rows = await pool.fetch(
        "SELECT symbol, name FROM entity WHERE kind = 'company' AND symbol = ANY($1)",
        list(panel.columns),
    )
    return sorted((row["symbol"], row["name"]) for row in rows)


def _company_rows_to_series(rows) -> dict[str, dict[pd.Timestamp, float]]:
    """Claim rows to {symbol: {date: close}}, reading the OHLCV snapshot
    shape company price claims actually store."""
    import json as _json

    series: dict[str, dict[pd.Timestamp, float]] = {}
    for row in rows:
        raw = row["value"]
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except _json.JSONDecodeError:
                continue
        # Price snapshots store the full OHLCV object; the close is the line.
        if isinstance(raw, dict):
            raw = raw.get("close")
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(price) or price <= 0:
            continue
        stamp = pd.Timestamp(row["event_date"])
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("UTC")
        series.setdefault(row["symbol"], {})[stamp.normalize()] = price
    return series


async def _company_panel(pool, audience) -> pd.DataFrame:
    """Measured daily closes for the company universe, from the claim store --
    the same audience-scoped source the companies scanner reads. Company
    prices are byo_only Polygon claims, so an anonymous caller is not entitled
    to them and gets an empty panel (the endpoint layer refuses the request
    outright). Only symbols that clear the comparator's own 200-session
    history floor are returned."""
    from omni.coverage.visibility import visible_claims_cte

    rows = await pool.fetch(
        f"""
        WITH visible AS ({visible_claims_cte("$1")})
        SELECT e.symbol, v.value, v.event_date
        FROM visible v
        JOIN entity e ON e.id = v.entity_id
        WHERE e.kind = 'company' AND v.claim_type = 'price_snapshot'
        ORDER BY e.symbol, v.event_date
        """,
        audience,
    )
    series = _company_rows_to_series(rows)

    frames = {
        symbol: pd.Series(points).sort_index()
        for symbol, points in series.items()
        if len(points) >= 200
    }
    if not frames:
        return pd.DataFrame()
    panel = pd.DataFrame(frames)
    panel.index = pd.to_datetime(panel.index).tz_localize(None).normalize()
    return panel.groupby(level=0).last()


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
    # `ann_vol > 0` is not a sufficient guard, and the failure it misses is the
    # one that actually occurs. A cash-equivalent fund is not exactly constant,
    # it is *nearly* constant: SGOV measured 0.205% annualised volatility, so
    # the ratio passed the guard and reported a Sharpe of 20.6 -- a figure no
    # real strategy achieves, produced by dividing a genuine 4.2% return by a
    # volatility that rounds to nothing.
    #
    # The floor is on the standard deviation rather than the variance because
    # only the former is in the same units as the return, and it is expressed
    # in annualised percent to match `ann_vol`. Below it the asset carries no
    # risk worth dividing by, so the honest answer is that it has no
    # risk-adjusted return -- not a large one.
    sharpe = (
        round(ann_ret / ann_vol, 2)
        if np.isfinite(ann_vol) and ann_vol >= MIN_SHARPE_VOLATILITY
        else None
    )

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
    if volatility < RISK_TIER_LOW_MAX:
        return "low"
    if volatility < RISK_TIER_MEDIUM_MAX:
        return "medium"
    return "high"


def _tier_census(entries: list[dict]) -> dict[str, int]:
    """How many assets landed in each tier, including the empty ones.

    Reported per category so a tier that no asset reached is visibly zero
    rather than silently absent. The distinction matters: a category of
    diversified funds cannot produce a `high` tier at all -- none of them
    reaches 30% annualised volatility -- and an omitted row reads as a filter
    the caller has applied, which would be a claim about the universe that is
    not true.
    """
    census = dict.fromkeys(("low", "medium", "high", "unrated"), 0)
    for entry in entries:
        tier = entry.get("risk_tier", "unrated")
        census[tier] = census.get(tier, 0) + 1
    return census


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


def _payload(
    buckets_data: list[dict],
    sectors: list[dict],
    coverage: dict[str, Any],
    company_entries: list[dict] | None = None,
) -> dict:
    assets = [asset for bucket in buckets_data for asset in bucket["assets"]]

    def category_ranked(asset_class: str, extra: list[dict] | None = None) -> list[dict]:
        available = [
            asset
            for asset in list(assets) + list(extra or [])
            if asset["asset_class"] == asset_class
            and asset.get("scores", {}).get("balanced") is not None
        ]
        available.sort(key=lambda asset: asset["scores"]["balanced"], reverse=True)
        return available

    rankings = {
        "stocks": category_ranked("stocks", company_entries or []),
        "defensive": category_ranked("defensive"),
        "crypto": category_ranked("crypto"),
    }

    return {
        "buckets": buckets_data,
        "category_rankings": rankings,
        "risk_census": {
            category: _tier_census(entries) for category, entries in rankings.items()
        },
        "ranking_method": {
            "balanced": "Within each category: 35% durable growth, 25% consistency, 20% stability, 10% one-year return, and 10% diversification; available measures are reweighted when history is shorter.",
            "history": "One-year return is trailing. Five- and ten-year figures are annualized. Median return uses complete calendar years; long-term and consistency ranks require at least three complete years.",
            "scope": "Scores are percentile ranks against the other assets in the same category, not forecasts or recommendations.",
            "risk_tier": f"Annualised volatility under {RISK_TIER_LOW_MAX:.0f}% is low, under {RISK_TIER_MEDIUM_MAX:.0f}% is medium, and at or above it is high. A tier showing zero means no asset in that category reached it, not that any were filtered out — diversified funds rarely clear the high threshold.",
            "sharpe": f"Sharpe is withheld below {MIN_SHARPE_VOLATILITY:.0f}% annualised volatility, where the ratio measures the denominator's noise rather than risk-adjusted return.",
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
    prices = _panel()
    funding = await _funding_rates(app.db.pool, audience)
    sectors = await _sector_leaders(app.db.pool, audience)

    buckets_data: list[dict] = []
    all_entries: list[dict] = []
    unavailable_assets: list[str] = []
    insufficient_crypto: list[dict[str, Any]] = []
    feed_defects: list[dict[str, Any]] = []
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
            census_price = (
                eligible_crypto[symbol].get("price")
                if a["asset_class"] == "crypto"
                else None
            )
            defect_reasons = _feed_defect_reasons(prices[symbol], census_price)
            if defect_reasons:
                feed_defects.append({
                    "symbol": symbol,
                    "reasons": defect_reasons,
                    "last_close": float(prices[symbol].dropna().iloc[-1]),
                    **({"census_price": census_price} if census_price is not None else {}),
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
            income = INCOME_AND_COST.get(symbol)
            if income is not None:
                entry["income_yield"] = income["yield_pct"]
                entry["expense_ratio"] = income["expense_ratio_pct"]
            else:
                entry["income_yield"] = None
                entry["expense_ratio"] = None
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
            "representative": _representative(
                bucket_name, {asset["symbol"] for asset in bucket_assets}
            ),
            "assets": bucket_assets,
        })

    # The company universe joins the stocks ranking from the claim store --
    # the same audience-scoped panel the comparator reads (byo_only Polygon
    # claims; an unlicensed audience scores none and the ETF panel stands
    # alone). Same metrics, same scoring, one honest universe instead of a
    # 28-ETF proxy set wearing "stocks".
    company_panel = await _company_panel_cached(app.db.pool, audience)
    spy = prices["SPY"] if "SPY" in prices.columns else None
    company_entries: list[dict[str, Any]] = []
    if not company_panel.empty:
        company_names = dict(await app.db.pool.fetch(
            "SELECT symbol, name FROM entity WHERE kind = 'company'"
        )) if False else {
            row["symbol"]: row["name"]
            for row in await app.db.pool.fetch(
                "SELECT symbol, name FROM entity WHERE kind = 'company'"
            )
        }
        for symbol in company_panel.columns:
            metrics = _compute_metrics(company_panel[symbol], "stocks")
            # No 3-complete-year floor here, deliberately: Polygon's free
            # window is two years, so every company in the store has exactly
            # 2 complete years and a 3-year floor would rank none of them
            # ever. The panel's own 200-session admission floor already ran;
            # the long-term/consistency components simply come back None and
            # the scorer reweights what remains (volatility, drawdown, 1y
            # return) -- the documented "available measures are reweighted
            # when history is shorter" behavior. The honest cost: company
            # scores lean on stability and recency, and the table's
            # ranking_method line already says so.
            if not metrics.get("returns"):
                continue
            correlation = (
                _correlation_to_market(company_panel[symbol], spy)
                if spy is not None else None
            )
            company_entries.append({
                "symbol": symbol,
                "name": company_names.get(symbol, symbol),
                "asset_class": "stocks",
                "area": "Companies",
                "risk_tier": _risk_tier(metrics["volatility"]),
                "correlation_to_spy": correlation,
                "market_behavior": _market_behavior(correlation),
                "from_claim_store": True,
                **metrics,
            })
            company_entries[-1]["return_1y"] = metrics.get("returns", {}).get("365d")
            company_entries[-1]["income_yield"] = None
            company_entries[-1]["expense_ratio"] = None
            company_entries[-1]["funding_apr"] = None

    for asset_class in ("stocks", "defensive", "crypto"):
        _score_assets(
            [entry for entry in all_entries if entry["asset_class"] == asset_class]
            + (company_entries if asset_class == "stocks" else [])
        )
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
        and not feed_defects
    )
    coverage = {
        "policy_version": POLICY_VERSION,
        "complete": company_complete and crypto_complete and not unavailable_assets,
        "feed_defects": feed_defects,
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
    payload = _payload(buckets_data, sectors, coverage, company_entries)
    # The assembled mix's own history, shown only when every representative
    # sleeve is present: one refused feed and the honest answer is nothing.
    representatives = [
        designated["symbol"]
        for designated in REPRESENTATIVE_ASSETS.values()
        if designated["symbol"] in prices.columns
    ]
    history = (
        _portfolio_history(prices, [REPRESENTATIVE_ASSETS[b]["symbol"] for b in REPRESENTATIVE_ASSETS])
        if len(representatives) == len(REPRESENTATIVE_ASSETS)
        else None
    )
    payload["portfolio_history"] = history
    payload["income_as_of"] = INCOME_AND_COST_AS_OF
    payload["decision_table"] = DECISION_TABLE
    payload["decision_table_as_of"] = DECISION_TABLE_AS_OF
    # The comparator's searchable universe: every broad asset plus every
    # measured company, so a search for NVDA finds it. Companies carry only
    # the fields the comparator needs -- the full company ranking lives on
    # its own endpoint with sector and score detail.
    company_symbols = await _company_symbols(app.db.pool, audience)
    if company_symbols:
        payload["comparator_universe"] = [
            {"symbol": symbol, "name": name, "kind": "company"}
            for symbol, name in company_symbols
        ]
    else:
        payload["comparator_universe"] = []
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

    @router.post("/scanner/custom-portfolio")
    async def custom_portfolio(request: Request) -> dict:
        """Compare a caller-assembled mix against the policy portfolio.

        Both mixes are measured over exactly the same window -- the dates
        where every symbol in either mix has prices -- so the comparison is
        apples to apples and the window is named in the response. Refuses a
        symbol that is not measured or whose feed failed the defect checks,
        by name, rather than quietly dropping it and comparing a different
        portfolio than the one asked about.
        """
        from neutron.error import bad_request, unauthorized

        # A mix may include companies, whose prices are byo_only Polygon
        # claims -- the same entitlement the companies scanner enforces. An
        # anonymous caller gets the same 401, not a silently smaller universe.
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - a body that is not JSON is a 400, not a 500
            raise bad_request("request body must be JSON")
        raw_positions = body.get("positions") if isinstance(body, dict) else None
        if not isinstance(raw_positions, list) or not 2 <= len(raw_positions) <= 12:
            raise bad_request("positions must be a list of 2 to 12 {symbol, weight} entries")

        positions: list[tuple[str, float]] = []
        for entry in raw_positions:
            if not isinstance(entry, dict):
                raise bad_request(f"each position must be an object, got {entry!r}")
            symbol = entry.get("symbol")
            weight = entry.get("weight", 1.0)
            if not isinstance(symbol, str) or not symbol:
                raise bad_request(f"position symbol must be a non-empty string, got {symbol!r}")
            try:
                weight = float(weight)
            except (TypeError, ValueError):
                raise bad_request(f"weight for {symbol} must be a number")
            if not weight > 0:
                raise bad_request(f"weight for {symbol} must be positive")
            positions.append((symbol, weight))

        prices = _panel()
        if prices.empty:
            raise bad_request("the measured price panel is unavailable; try again shortly")
        companies = await _company_panel_cached(app.db.pool, audience)
        if not companies.empty:
            prices = prices.join(companies, how="outer")

        policy_symbols = [
            REPRESENTATIVE_ASSETS[bucket]["symbol"] for bucket in REPRESENTATIVE_ASSETS
        ]
        for symbol, _ in positions:
            if symbol not in prices.columns:
                raise bad_request(f"{symbol} is not in the measured universe")
            series = prices[symbol].dropna()
            if len(series) < 60:
                raise bad_request(f"{symbol} has too little measured history")
            defects = _feed_defect_reasons(series, census_price=None)
            if defects:
                raise bad_request(
                    f"{symbol}'s price feed failed the defect check: {'; '.join(defects)}"
                )

        custom = _mix_history(prices, positions, window_symbols=policy_symbols)
        policy = _mix_history(
            prices,
            [(symbol, 1.0) for symbol in policy_symbols],
            window_symbols=[symbol for symbol, _ in positions],
        )
        if custom is None or policy is None:
            raise bad_request(
                "the selected assets do not share a measured window with the portfolio"
            )

        # Income and cost for the caller's mix, where every held symbol has a
        # sponsor figure. Companies and crypto carry none by construction -- a
        # mix holding any of them reports partial figures with the absent
        # symbols named, never a guess.
        total_weight = sum(weight for _, weight in positions)
        priced = [
            (symbol, weight / total_weight)
            for symbol, weight in positions
            if symbol in INCOME_AND_COST
        ]
        unpriced = [symbol for symbol, _ in positions if symbol not in INCOME_AND_COST]
        income = None
        if priced:
            income = {
                "yield_pct": round(
                    sum(w * INCOME_AND_COST[s]["yield_pct"] for s, w in priced), 2
                ),
                "expense_ratio_pct": round(
                    sum(w * INCOME_AND_COST[s]["expense_ratio_pct"] for s, w in priced), 3
                ),
                "not_covered": unpriced,
            }
        return {
            "custom": custom,
            "policy": policy,
            "income": income,
            "income_as_of": INCOME_AND_COST_AS_OF,
        }

    return router


__all__ = ["build_router"]
