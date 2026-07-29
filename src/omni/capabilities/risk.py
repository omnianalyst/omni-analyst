"""Integrated market / economic / correlation / geopolitical risk as pure
capabilities.

Ported from v1 `app/services/integrated_risk_analyzer.py` (1,491 lines). Only
the risk *scoring* was lifted. Everything that fetched, cached, held a DB
session, spawned a `ProcessPoolExecutor`, or delegated to another service is
gone: the constructor and its six injected services, `_gather_*` (market /
economic / news / alternative), `_detect_integrated_black_swans` (delegated to
`BlackSwanDetector`), and the entire Monte-Carlo scenario simulation
(`_simulate_future_scenarios`, `_run_scenario_simulation`,
`_monte_carlo_simulation`, `_project_economic_indicators`, `_project_sentiment`).
The simulation layer is stochastic and the projection helpers fall back to
`np.random.normal` random walks -- fabrication, by this repo's rule -- so they
were left behind rather than ported.

Per the work order, `_analyze_sentiment_risks` and `_calculate_fear_greed` were
*not* ported: they substitute 0.5 on missing sentiment and score on hardcoded
0.2 / 0.8 / 0.3 thresholds plus a hand-tuned VIX nudge. `calculate_overall_risk`
therefore takes the sentiment score as a plain argument -- whoever produces a
defensible sentiment score supplies it.

Where v1 substituted a default on missing input -- a VIX of 20, a 2Y/10Y of
4.0/4.5, an inflation of 3.0, a credit spread of 100/400, a neutral risk score
of 50, a depth score of 50 -- this module raises `Unavailable` instead. A
capability that returns a plausible-looking number on no data is how
hallucinated coverage enters the store.

Input shape: v1 threaded a `market_data` / `economic_data` god-dict through
every scorer. That dict was assembled by the (now-removed) fetch layer and is
the framework tangle PORTING.md says to drop. Each scorer here takes exactly
the scalars or sequences it needs, matching `macro.py` / `crossasset.py`.

Entry points (the analyses the orchestrator calls) are async. The leaf
mathematical helpers are sync because they do no IO.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from omni.ingest.protocol import Unavailable

# Historical-average credit spreads used as model constants by
# `analyze_credit_risk`. These are calibration anchors (not input defaults):
# v1 compares the supplied spread against 1.2x / 1.5x of these averages.
_IG_AVG = 120.0
_HY_AVG = 450.0

# Static keyword catalogs ported verbatim from v1.
_RISK_KEYWORDS = ["war", "sanctions", "tariff", "crisis", "conflict", "tension"]
_HOTSPOT_KEYWORDS: dict[str, list[str]] = {
    "Middle East": ["iran", "iraq", "saudi", "israel", "yemen", "syria"],
    "Eastern Europe": ["ukraine", "russia", "belarus", "crimea"],
    "Asia Pacific": ["taiwan", "north korea", "south china sea", "myanmar"],
    "Trade War": ["tariff", "trade war", "sanctions", "embargo"],
    "Cyber": ["cyberattack", "ransomware", "data breach", "hack"],
}


# ---------------------------------------------------------------------------
# Market risk
# ---------------------------------------------------------------------------

def analyze_volatility_regime(
    vix_level: float,
    vix_change_pct: float,
    term_structure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify the volatility regime and flag a regime transition risk.

    `vix_change_pct` is the VIX's own percent change over the lookback; v1
    defaulted it to 0 (which silently reports no transition risk). It is
    required here -- an unknown change cannot be reported as a safe zero.
    """
    if vix_level < 15:
        regime, risk_score = "low_vol", 20.0
    elif vix_level < 25:
        regime, risk_score = "normal_vol", 40.0
    elif vix_level < 35:
        regime, risk_score = "elevated_vol", 70.0
    else:
        regime, risk_score = "high_vol", 90.0

    transition_risk = abs(vix_change_pct) > 20

    return {
        "regime": regime,
        "score": risk_score,
        "vix_level": vix_level,
        "transition_risk": transition_risk,
        "term_structure": term_structure or {},
    }


def analyze_liquidity_risk(quotes: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """Wide-spread ratio across a set of (bid, ask) quotes.

    v1 returned a neutral 50 and left `wide_spread_ratio` unbound (a latent
    NameError) when no quote carried both sides; both are gone. At least one
    quote is required.
    """
    quotes = list(quotes)
    if not quotes:
        raise Unavailable("no quotes; cannot assess liquidity")

    wide_spread_count = 0
    for bid, ask in quotes:
        if bid == 0:
            raise Unavailable("zero bid; spread percentage undefined")
        if (ask - bid) / bid * 100 > 0.5:
            wide_spread_count += 1

    wide_spread_ratio = wide_spread_count / len(quotes)
    return {
        "score": min(100, wide_spread_ratio * 200),
        "wide_spread_ratio": wide_spread_ratio,
    }


def calculate_depth_score(depths: Sequence[tuple[float, float]]) -> float:
    """Mean bid/ask depth imbalance, scaled to 0-100.

    Each element is the summed bid depth and summed ask depth for one symbol.
    v1 returned a neutral 50 when no level had any depth; that is gone.
    """
    imbalances: list[float] = []
    for bid_depth, ask_depth in depths:
        if bid_depth + ask_depth > 0:
            imbalances.append(abs(bid_depth - ask_depth) / (bid_depth + ask_depth))

    if not imbalances:
        raise Unavailable("no non-zero depth levels; cannot score depth")
    return float(np.mean(imbalances) * 100)


def calculate_top_concentration(market_caps: Sequence[float], n: int) -> float:
    """Share of total market cap held by the largest `n` entries."""
    market_caps = list(market_caps)
    if not market_caps:
        return 0.0
    sorted_caps = sorted(market_caps, reverse=True)
    top_n = sorted_caps[:n]
    return sum(top_n) / sum(market_caps)


def analyze_concentration_risk(market_caps: Sequence[float]) -> dict[str, Any]:
    """Herfindahl-Hirschman concentration, scaled to a 0-100 score.

    v1 returned a neutral 50 (and an HHI of 0) when no market cap was present;
    that is gone.
    """
    market_caps = list(market_caps)
    if not market_caps:
        raise Unavailable("no market caps; cannot assess concentration")

    total_cap = sum(market_caps)
    hhi = sum((cap / total_cap) ** 2 for cap in market_caps) * 10000
    return {
        "score": min(100, hhi / 100),
        "herfindahl_index": hhi,
        "top_5_concentration": calculate_top_concentration(market_caps, 5),
    }


def calculate_put_call_skew(
    calls: Sequence[dict[str, Any]],
    puts: Sequence[dict[str, Any]],
    spot: float,
) -> float | None:
    """OTM put IV minus OTM call IV.

    Returns None when the skew cannot be measured (no spot, or no OTM IVs),
    matching v1's "not computable" signal. OTM thresholds are spot*0.95 (puts)
    and spot*1.05 (calls), ported verbatim.
    """
    if spot == 0:
        return None

    otm_put_ivs: list[float] = []
    for put in puts:
        strike = put.get("strike", 0)
        iv = put.get("implied_volatility", 0)
        if strike < spot * 0.95 and iv > 0:
            otm_put_ivs.append(iv)

    otm_call_ivs: list[float] = []
    for call in calls:
        strike = call.get("strike", 0)
        iv = call.get("implied_volatility", 0)
        if strike > spot * 1.05 and iv > 0:
            otm_call_ivs.append(iv)

    if otm_put_ivs and otm_call_ivs:
        return float(np.mean(otm_put_ivs) - np.mean(otm_call_ivs))
    if otm_put_ivs:
        return float(np.mean(otm_put_ivs))
    return None


def analyze_options_skew(skews: Sequence[float | None]) -> dict[str, Any]:
    """Average put-call skew mapped to a 0-100 score.

    `(avg + 20) * 2.5`: a skew of -20 maps to 0 (call-skew / complacency), +20
    maps to 100 (put-skew / fear). `None` skews (unmeasurable) are dropped; if
    none remain, v1 returned a neutral 50 -- gone here.
    """
    present = [s for s in skews if s is not None]
    if not present:
        raise Unavailable("no put-call skew observations")

    avg_skew = float(np.mean(present))
    return {
        "score": min(100, max(0, (avg_skew + 20) * 2.5)),
        "average_skew": avg_skew,
    }


def find_extreme_skew(
    options_by_symbol: dict[str, dict[str, Any]],
    threshold: float = 0.3,
) -> list[str]:
    """Symbols whose put-call skew exceeds `threshold` in magnitude.

    v1 read `market_data.get("options", {})`, but `market_data` was keyed by
    symbol with a per-symbol `options` field -- so the top-level lookup never
    matched anything and this always returned `[]`. Reshaped to take the
    `{symbol: options}` dict directly so it actually works.
    """
    extreme: list[str] = []
    for symbol, options_data in options_by_symbol.items():
        skew = calculate_put_call_skew(
            options_data.get("calls", []),
            options_data.get("puts", []),
            options_data.get("underlying_price", 0),
        )
        if skew is not None and abs(skew) > threshold:
            extreme.append(symbol)
    return extreme


def analyze_market_breadth(
    *,
    advance_decline_ratio: float,
    percent_above_50ma: float,
    percent_above_200ma: float,
    new_highs: int,
    new_lows: int,
) -> dict[str, Any]:
    """Market-breadth score from advance/decline, % above MAs, new highs/lows.

    Every input is required: v1 defaulted each to a neutral value (ratio 1.0,
    50% above MA, 0 highs / 0 lows) and so reported a neutral breadth on no
    data. The 50 score returned when no extreme condition holds is a computed
    outcome, not a missing-data default.
    """
    if advance_decline_ratio < 0.5 or percent_above_50ma < 30 or new_lows > new_highs * 2:
        breadth_score = 80.0
    elif (
        advance_decline_ratio > 2.0
        and percent_above_50ma > 70
        and new_highs > new_lows * 2
    ):
        breadth_score = 20.0
    else:
        breadth_score = 50.0

    return {
        "score": breadth_score,
        "advance_decline_ratio": advance_decline_ratio,
        "percent_above_50ma": percent_above_50ma,
        "percent_above_200ma": percent_above_200ma,
        "new_highs": new_highs,
        "new_lows": new_lows,
    }


# ---------------------------------------------------------------------------
# Economic risk
# ---------------------------------------------------------------------------

def analyze_yield_curve_risk(two_year: float, ten_year: float) -> dict[str, Any]:
    """Recession risk from the 2s10s spread.

    v1 defaulted 2Y to 4.0 and 10Y to 4.5 (a fabricated +0.5 normal curve) when
    either leg was missing; both are required here.
    """
    spread_2s10s = ten_year - two_year
    is_inverted = spread_2s10s < 0

    if is_inverted:
        risk_score = 80 + min(20, abs(spread_2s10s) * 40)
    elif spread_2s10s < 0.5:
        risk_score = 60
    else:
        risk_score = 30

    return {
        "score": risk_score,
        "spread_2s10s": spread_2s10s,
        "is_inverted": is_inverted,
        "curve_shape": "inverted" if is_inverted else "normal",
    }


def analyze_inflation_risk(
    current_inflation: float,
    expected_inflation: float,
    breakevens: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inflation risk from current and 5y5y-forward expectations.

    v1 defaulted current to 3.0 and expected to 2.5 (both below their >3 / >5
    thresholds, silently reporting low risk); both are required here.
    """
    if current_inflation > 5:
        risk_score = 80
    elif current_inflation > 3:
        risk_score = 60
    else:
        risk_score = 30

    if expected_inflation > 3:
        risk_score = min(100, risk_score + 20)

    return {
        "score": risk_score,
        "current_inflation": current_inflation,
        "expected_inflation": expected_inflation,
        "breakevens": breakevens or {},
    }


def estimate_recession_probability(
    gdp_growth: float, unemployment: float
) -> float:
    """Heuristic recession probability in [0.15, 0.95].

    Ported verbatim: a 0.15 base, +0.5/+0.3/+0.1 by GDP band, +0.2/+0.1 by
    unemployment band, capped at 0.95.
    """
    base_prob = 0.15
    if gdp_growth < 0:
        base_prob += 0.5
    elif gdp_growth < 1:
        base_prob += 0.3
    elif gdp_growth < 2:
        base_prob += 0.1

    if unemployment > 5:
        base_prob += 0.2
    elif unemployment > 4:
        base_prob += 0.1

    return min(0.95, base_prob)


def analyze_growth_risk(
    gdp_growth: float, unemployment: float, job_growth: float
) -> dict[str, Any]:
    """Growth risk from GDP nowcast and employment.

    v1 defaulted GDP growth to 2.0, unemployment to 4.0, job growth to 150000
    (all benign) on missing input; all three are required here.
    """
    if gdp_growth < 0:
        risk_score = 90
    elif gdp_growth < 1:
        risk_score = 70
    elif gdp_growth < 2:
        risk_score = 50
    else:
        risk_score = 30

    if unemployment > 5 or job_growth < 50000:
        risk_score = min(100, risk_score + 20)

    return {
        "score": risk_score,
        "gdp_growth": gdp_growth,
        "unemployment": unemployment,
        "job_growth": job_growth,
        "recession_probability": estimate_recession_probability(
            gdp_growth, unemployment
        ),
    }


def analyze_credit_risk(ig_spread: float, hy_spread: float) -> dict[str, Any]:
    """Credit-spread risk against historical-average anchors (IG 120, HY 450).

    v1 defaulted IG to 100 and HY to 400 (both below average, silently reporting
    tight conditions); both are required here. The averages themselves are
    calibration constants, not input defaults.
    """
    if ig_spread > _IG_AVG * 1.5 or hy_spread > _HY_AVG * 1.5:
        risk_score = 80
    elif ig_spread > _IG_AVG * 1.2 or hy_spread > _HY_AVG * 1.2:
        risk_score = 60
    else:
        risk_score = 40

    return {
        "score": risk_score,
        "ig_spread": ig_spread,
        "hy_spread": hy_spread,
        "spread_widening": ig_spread > _IG_AVG,
    }


# ---------------------------------------------------------------------------
# Correlation risk
# ---------------------------------------------------------------------------

def calculate_correlation_stability(returns_df: pd.DataFrame) -> float:
    """Std-dev of the rolling 60-day mean pairwise correlation.

    Returns 0.0 when fewer than one full window is available (ported verbatim):
    with no observable windows there is no measured instability, which is
    distinct from asserting stability.
    """
    window = 60
    correlations: list[float] = []
    for i in range(window, len(returns_df)):
        window_data = returns_df.iloc[i - window: i]
        corr_matrix = window_data.corr()
        correlations.append(
            float(
                corr_matrix.values[np.triu_indices_from(corr_matrix.values, k=1)].mean()
            )
        )
    return float(np.std(correlations)) if correlations else 0.0


def identify_correlation_clusters(corr_matrix: pd.DataFrame) -> list[list[str]]:
    """Greedy single-link clustering of symbols with |corr| > 0.7."""
    clusters: list[list[str]] = []
    threshold = 0.7
    symbols = list(corr_matrix.columns)
    assigned: set[str] = set()

    for i, sym1 in enumerate(symbols):
        if sym1 in assigned:
            continue
        cluster = [sym1]
        for j, sym2 in enumerate(symbols):
            if (
                i != j
                and sym2 not in assigned
                and abs(corr_matrix.iloc[i, j]) > threshold
            ):
                cluster.append(sym2)
                assigned.add(sym2)
        if len(cluster) > 1:
            clusters.append(cluster)
            assigned.add(sym1)

    return clusters


async def analyze_correlation_risks(
    returns_data: dict[str, Sequence[float]],
) -> dict[str, Any]:
    """Mean pairwise correlation, its rolling stability, and cluster structure.

    `returns_data` maps each symbol to its daily-return series; the caller owns
    the price->return conversion and lookback (matching `crossasset.py`). v1
    returned a neutral 50 when fewer than two symbols had history; that is gone.
    """
    if len(returns_data) < 2:
        raise Unavailable(
            f"need >=2 symbols for correlation analysis, got {len(returns_data)}"
        )

    returns_df = pd.DataFrame(returns_data).dropna()
    if len(returns_df) < 2:
        raise Unavailable("fewer than 2 aligned return observations after dropna")

    corr_matrix = returns_df.corr()
    avg_correlation = float(
        corr_matrix.values[np.triu_indices_from(corr_matrix.values, k=1)].mean()
    )

    if avg_correlation > 0.8:
        risk_score = 80
    elif avg_correlation < 0.2:
        risk_score = 70
    else:
        risk_score = 40

    stability = calculate_correlation_stability(returns_df)

    return {
        "score": risk_score,
        "average_correlation": avg_correlation,
        "correlation_stability": stability,
        "breakdown_risk": stability > 0.2,
        "correlation_clusters": identify_correlation_clusters(corr_matrix),
    }


# ---------------------------------------------------------------------------
# Geopolitical risk
# ---------------------------------------------------------------------------

def identify_geopolitical_hotspots(
    articles: Sequence[dict[str, Any]],
) -> list[str]:
    """Regions / themes whose keyword catalog appears in any article.

    v1 iterated `news_data.values()` filtering for dicts carrying an `articles`
    list; reshaped to take a flat article list (the per-symbol grouping was an
    artefact of the fetch layer). Order preserved, capped at 10.
    """
    hotspots: list[str] = []
    for article in articles:
        title = article.get("title", "").lower()
        summary = article.get("summary", "").lower()
        text = f"{title} {summary}"
        for region, keywords in _HOTSPOT_KEYWORDS.items():
            if any(kw in text for kw in keywords) and region not in hotspots:
                hotspots.append(region)
    return hotspots[:10]


async def analyze_geopolitical_risks(
    articles: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Geopolitical risk score from risk-keyword density in article titles.

    v1 sampled up to 10 titles *per symbol* (a rate-limit guard tied to the
    per-symbol news fetch, now gone) and returned a neutral 50 when nothing was
    scanned. Here the score is computed over all supplied titles, and at least
    one article is required -- an unscanned news feed cannot be reported as
    low-risk.
    """
    articles = list(articles)
    if not articles:
        raise Unavailable("no articles; cannot assess geopolitical risk")

    keyword_count = 0
    total_articles = 0
    for article in articles:
        title = article.get("title", "").lower()
        if any(keyword in title for keyword in _RISK_KEYWORDS):
            keyword_count += 1
        total_articles += 1

    risk_ratio = keyword_count / total_articles
    geopolitical_score = min(100, risk_ratio * 500)

    return {
        "score": geopolitical_score,
        "risk_mentions": keyword_count,
        "total_articles": total_articles,
        "hotspots": identify_geopolitical_hotspots(articles),
    }


# ---------------------------------------------------------------------------
# Overall risk
# ---------------------------------------------------------------------------

def classify_risk_level(score: float) -> str:
    if score >= 80:
        return "extreme"
    if score >= 60:
        return "high"
    if score >= 40:
        return "moderate"
    if score >= 20:
        return "low"
    return "minimal"


def calculate_overall_risk_score(
    *,
    market_score: float,
    economic_score: float,
    sentiment_score: float,
    correlation_score: float,
    geopolitical_score: float,
) -> dict[str, Any]:
    """Weighted overall risk score and black-swan probability.

    Weights are ported verbatim: market 0.30, economic 0.25, sentiment /
    correlation / geopolitical 0.15 each. The sentiment score is supplied as a
    plain argument because the v1 sentiment analyzer (`_analyze_sentiment_risks`)
    was deliberately not ported -- it fabricated a 0.5 reading on missing input
    and scored on hardcoded thresholds.

    v1 read `market_risks["overall_score"]` and `economic_risks["overall_score"]`
    (the composite of each sub-analyzer) but `["score"]` for the other three;
    the same weighting is preserved by taking each composite as a float here.
    """
    overall_score = (
        market_score * 0.30
        + economic_score * 0.25
        + sentiment_score * 0.15
        + correlation_score * 0.15
        + geopolitical_score * 0.15
    )

    if overall_score > 80:
        black_swan_prob = 0.1 + (overall_score - 80) / 100
    elif overall_score > 60:
        black_swan_prob = 0.02 + (overall_score - 60) / 200
    else:
        black_swan_prob = overall_score / 3000

    return {
        "score": overall_score,
        "black_swan_prob": min(0.5, black_swan_prob),
        "risk_level": classify_risk_level(overall_score),
    }


# ---------------------------------------------------------------------------
# Static helpers (scenario triggers, sector concentration) -- ported verbatim
# ---------------------------------------------------------------------------

def identify_scenario_triggers(scenario_id: str) -> dict[str, Any]:
    """Static trigger catalog for a named scenario (ported verbatim).

    v1 took the full scenario `template` dict and read `template["id"]`; reshaped
    to take the id directly. Returns `{}` for unknown ids, matching v1.
    """
    if scenario_id == "recession":
        return {
            "yield_curve_inversion": True,
            "unemployment_rise": ">5%",
            "gdp_growth": "<0%",
            "credit_spread_threshold": "IG>200bps",
        }
    if scenario_id == "black_swan":
        return {
            "vix_spike": ">40",
            "correlation_breakdown": True,
            "liquidity_crisis": True,
            "systemic_event": "bank_failure or sovereign_default",
        }
    if scenario_id == "recovery":
        return {
            "gdp_growth": ">3%",
            "unemployment": "<3.5%",
            "earnings_growth": ">10%",
            "sentiment": "extreme_bullish",
        }
    return {}


_SECTOR_MAP: dict[str, str] = {
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology",
    "META": "Technology", "NVDA": "Technology", "AMD": "Technology",
    "INTC": "Technology", "ORCL": "Technology",
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials",
    "GS": "Financials", "V": "Financials", "MA": "Financials",
    "JNJ": "Healthcare", "PFE": "Healthcare", "UNH": "Healthcare",
    "MRK": "Healthcare", "ABBV": "Healthcare",
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary", "NKE": "Consumer Discretionary",
    "PG": "Consumer Staples", "KO": "Consumer Staples", "PEP": "Consumer Staples",
    "BA": "Industrials", "CAT": "Industrials", "GE": "Industrials",
}


def identify_concentrated_sectors(
    symbols: Sequence[str], threshold: float = 0.3
) -> list[str]:
    """Sectors whose symbol share exceeds `threshold` (ported verbatim)."""
    symbols = list(symbols)
    total = len(symbols)
    if total == 0:
        return []

    sector_counts: dict[str, int] = {}
    for symbol in symbols:
        sector = _SECTOR_MAP.get(symbol, "Other")
        sector_counts[sector] = sector_counts.get(sector, 0) + 1

    return [
        sector
        for sector, count in sector_counts.items()
        if count / total > threshold
    ]
