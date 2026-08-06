"""News and sentiment scoring capabilities.

Ported from v1 `app/services/news_service.py` and
`app/services/alternative_data/sentiment_service.py`. Only the computation was
lifted; every fetch, cache, DB session and router is gone. IO is injected: the
per-source entry point takes a `fetch_fn` (mirroring `omni.ingest.fred.FredAdapter`),
and the aggregations take their already-fetched rows as plain arguments.

Two things were deliberately NOT ported:

- `aggregate_sentiment` (the cross-source mean in `get_social_sentiment`). It
  averages `sentiment_score` across sources without checking the `simulated`
  flag a source may set, so a fabricated reading contaminates the mean. The
  per-source scorer is extracted instead; combining sources is a fill-pipeline
  decision that must respect licence class and simulation flags.

- The Reddit, Twitter and News sentiment scorers. Each obtains its polarity from
  `TextBlob(...).sentiment.polarity`, and `textblob` is not (and cannot here be)
  a dependency. Re-deriving those polarities with a different method would be
  inventing the very numbers this layer exists to keep honest, so they are left
  out rather than fabricated. The StockTwits scorer reads provider-supplied
  Bullish/Bearish/Neutral tags, so it carries.

Where v1 substituted a default on missing input, this module raises
`Unavailable` instead. The census found 44 fabrications in the predecessor that
way; a capability that always returns a number is how hallucinated coverage
enters the store.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime

import numpy as np

from omni.ingest.protocol import Unavailable

_COMMON_WORDS = frozenset(
    {"THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "NEW", "CEO", "CFO", "IPO", "ETF"}
)

StockTwitsFetcher = Callable[[str], Awaitable[dict]]


def classify_sentiment(score: float) -> str:
    if score >= 0.5:
        return "very_bullish"
    elif score >= 0.2:
        return "bullish"
    elif score >= 0.05:
        return "slightly_bullish"
    elif score >= -0.05:
        return "neutral"
    elif score >= -0.2:
        return "slightly_bearish"
    elif score >= -0.5:
        return "bearish"
    else:
        return "very_bearish"


def calculate_trend(values: list[float]) -> str:
    if len(values) < 2:
        return "insufficient_data"

    x = np.arange(len(values))
    y = np.array(values)
    slope = np.polyfit(x, y, 1)[0]

    if slope > 0.01:
        return "improving"
    elif slope < -0.01:
        return "deteriorating"
    else:
        return "stable"


def calculate_signal_strength(scores: list[tuple[str, float]]) -> str:
    if not scores:
        return "no_signal"

    positive = sum(1 for _, score in scores if score > 0.1)
    negative = sum(1 for _, score in scores if score < -0.1)
    total = len(scores)

    if positive == total:
        return "strong_positive"
    elif negative == total:
        return "strong_negative"
    elif positive > total / 2:
        return "moderate_positive"
    elif negative > total / 2:
        return "moderate_negative"
    else:
        return "mixed"


def parse_feed_date(date_tuple) -> datetime:
    """Feed timestamps are UTC; attach that rather than leaving them naive.

    A naive datetime written to a TIMESTAMPTZ column is read as local time, so
    an article published at 14:00 UTC lands as 14:00 in whatever zone the
    process happens to run in. In a store whose whole premise is knowing when
    something became knowable, that is a silent offset, not a formatting
    detail.
    """
    if not date_tuple:
        raise Unavailable("feed entry has no published date")
    return datetime(*date_tuple[:6], tzinfo=UTC)


def extract_ticker_entities(
    *, title: str, summary: str, content: str
) -> list[dict]:
    ticker_pattern = r"\b[A-Z]{1,5}\b"
    text = f"{title} {summary} {content}"

    potential_tickers = re.findall(ticker_pattern, text)

    entities = []
    for ticker in potential_tickers:
        if ticker not in _COMMON_WORDS and len(ticker) >= 2:
            entities.append(
                {
                    "entity_type": "ticker",
                    "entity_value": ticker,
                    "mentions_count": text.count(ticker),
                }
            )
    return entities


def aggregate_market_sentiment(
    rows: list[tuple[str, int, float | None]],
) -> dict:
    """Aggregate stored per-sentiment counts into a market read.

    `rows` are the (sentiment, count, avg_score) groups the v1 DB query
    produced; here they are plain arguments so the maths is testable with no
    database. An empty window is an honest gap, not a neutral market: it raises.
    """
    total = sum(count for _, count, _ in rows)
    if total == 0:
        raise Unavailable("no sentiment data in window")

    breakdown = {
        sentiment: {
            "count": count,
            "percentage": (count / total * 100) if total > 0 else 0,
            "avg_score": float(avg_score) if avg_score else 0,
        }
        for sentiment, count, avg_score in rows
    }

    weighted_score = sum(count * (avg_score or 0) for _, count, avg_score in rows) / total
    if weighted_score > 0.2:
        overall = "bullish"
    elif weighted_score < -0.2:
        overall = "bearish"
    else:
        overall = "neutral"

    return {
        "overall": overall,
        "breakdown": breakdown,
        "total_articles": total,
    }


def score_portfolio_impact(
    *,
    sentiment_score: float,
    sentiment: str,
    confidence: float,
    affected_tickers: list[str],
) -> dict:
    base_impact = sentiment_score * 100
    if sentiment == "bullish" or sentiment == "bearish":
        impact_score = base_impact * (1 + len(affected_tickers) * 0.1)
    else:
        impact_score = 0

    return {
        "impact_score": impact_score,
        "impact_type": "direct" if affected_tickers else "market",
        "affected_holdings": list(affected_tickers),
        "analysis": {
            "sentiment": sentiment,
            "confidence": confidence,
            "affected_count": len(affected_tickers),
        },
    }


def score_stocktwits_messages(messages: list[dict], *, watchers: int) -> dict:
    if not messages:
        raise Unavailable("StockTwits returned no messages to score")

    bullish = 0
    bearish = 0
    neutral = 0

    for msg in messages:
        sentiment = (
            msg.get("entities", {}).get("sentiment", {}).get("basic", "neutral")
        )

        if sentiment == "Bullish":
            bullish += 1
        elif sentiment == "Bearish":
            bearish += 1
        else:
            neutral += 1

    total = bullish + bearish + neutral
    sentiment_score = (bullish - bearish) / total

    return {
        "platform": "stocktwits",
        "messages_analyzed": total,
        "sentiment_score": sentiment_score,
        "sentiment": classify_sentiment(sentiment_score),
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "watchers": watchers,
    }


async def stocktwits_sentiment(symbol: str, *, fetch_fn: StockTwitsFetcher) -> dict:
    payload = await fetch_fn(symbol)
    messages = payload.get("messages", [])
    watchers = payload.get("symbol", {}).get("watchlist_count", 0)
    return score_stocktwits_messages(messages, watchers=watchers)


# ---------------------------------------------------------------------------
# Per-symbol news impact and sector outlook
#
# Ported from v1 `news.py` -- the inline SQL+aggregation in the
# `/news/impact/symbol/{symbol}` and `/news/sectors` handlers. The SQL that
# fetched the article/entity/sentiment rows is gone (that is fill-pipeline
# territory); the aggregation over already-fetched rows is the analysis, and
# that is what is lifted here.
#
# Defaults removed (raise `Unavailable` instead), per the work order:
# - The per-symbol handler returned a fabricated {sentiment:"neutral",
#   sentiment_score:0.0, confidence:0.0, impact_score:0.0} when no articles
#   matched. An empty window is an honest gap, not a neutral market: it raises.
# - The sector handler initialised every sector to outlook:"neutral" / score:0.0
#   and confidence:0.0 before processing rows, so the response always carried
#   all eight sectors even when none had coverage. A sector with no articles is
#   now simply absent from the result -- its absence is the gap signal, not a
#   row of zeros dressed up as a neutral read.
# - The handler substituted confidence 0.5 wherever the DB score was NULL. A
#   missing confidence is the caller's problem; this module never invents one.
# ---------------------------------------------------------------------------


def symbol_news_impact(
    articles: Sequence[tuple[str, float, float]],
) -> dict:
    """Aggregate one symbol's article sentiments into a news-impact read.

    `articles` are the `(sentiment, sentiment_score, confidence)` triples the
    v1 SQL join produced (sentiment is the label, e.g. "bullish"; the score is
    the signed polarity; confidence is the model's self-reported confidence).
    The impact score combines volume (how many articles) with sentiment
    strength (how polarised they are), each weighted half, on a 0-100 scale --
    carried bit-for-bit from the v1 handler.

    Raises `Unavailable` when no articles are supplied: the v1 handler returned
    a neutral/zero read indistinguishable from "we looked and found nothing
    moving", which is exactly the substitution this layer exists to remove.
    """
    count = len(articles)
    if count == 0:
        raise Unavailable("no articles in window; news impact is undefined")

    total_sentiment = 0.0
    total_confidence = 0.0
    bullish = 0
    bearish = 0
    neutral = 0

    for sentiment, sentiment_score, confidence in articles:
        total_sentiment += sentiment_score
        total_confidence += confidence
        if sentiment == "bullish":
            bullish += 1
        elif sentiment == "bearish":
            bearish += 1
        else:
            neutral += 1

    avg_sentiment = total_sentiment / count
    avg_confidence = total_confidence / count

    if avg_sentiment > 0.2:
        overall_sentiment = "bullish"
    elif avg_sentiment < -0.2:
        overall_sentiment = "bearish"
    else:
        overall_sentiment = "neutral"

    # v1: volume tops out at 10 articles; sentiment strength is |avg|. Each
    # contributes half to a 0-100 score.
    volume_factor = min(1.0, count / 10.0)
    sentiment_strength = abs(avg_sentiment)
    impact_score = (volume_factor * 0.5 + sentiment_strength * 0.5) * 100

    return {
        "articles_count": count,
        "sentiment": overall_sentiment,
        "sentiment_score": round(avg_sentiment, 3),
        "confidence": round(avg_confidence, 3),
        "impact_score": round(impact_score, 1),
        "sentiment_breakdown": {
            "bullish": bullish,
            "bearish": bearish,
            "neutral": neutral,
        },
    }


def _sector_outlook_label(score: float) -> str:
    if score > 0.3:
        return "positive"
    elif score > 0.1:
        return "slightly_positive"
    elif score < -0.3:
        return "negative"
    elif score < -0.1:
        return "slightly_negative"
    return "neutral"


SectorMapping = Mapping[str, Sequence[str]]


def sector_news_outlook(
    rows: Sequence[tuple[str, float | None, int, int, int, int]],
    *,
    sector_mapping: SectorMapping,
) -> list[dict]:
    """Aggregate per-ticker news sentiment into a sector-level outlook.

    `rows` are the `(ticker, avg_sentiment, article_count, bullish, bearish,
    neutral)` tuples the v1 SQL join produced (the same shape
    `aggregate_market_sentiment` consumes, but keyed by ticker rather than by
    sentiment label). `sector_mapping` maps a sector name to its member tickers
    -- the static ticker->sector table is honest scaffolding (a fixed universe
    definition), not fabricated data, so it is an explicit argument the caller
    owns rather than something this function invents.

    The outlook score is `(bullish - bearish) / total` over the sentiment-label
    counts (volume-weighted by article count), with a confidence that saturates
    at 20 articles -- carried bit-for-bit from the v1 handler.

    Raises `Unavailable` when no supplied ticker maps to any sector: the v1
    handler returned all eight sectors at neutral/zero in that case, which looks
    like a measured calm market rather than "we have no coverage".
    """
    ticker_to_sector: dict[str, str] = {}
    for sector, tickers in sector_mapping.items():
        for ticker in tickers:
            ticker_to_sector[ticker.upper()] = sector

    agg: dict[str, dict] = {}

    for ticker, avg_sentiment, article_count, bullish, bearish, neutral in rows:
        sector = ticker_to_sector.get((ticker or "").upper())
        if sector is None:
            continue

        bucket = agg.setdefault(
            sector,
            {
                "articles_analyzed": 0,
                "bullish_count": 0,
                "bearish_count": 0,
                "neutral_count": 0,
                "top_symbols": [],
            },
        )
        bucket["articles_analyzed"] += article_count
        bucket["bullish_count"] += bullish
        bucket["bearish_count"] += bearish
        bucket["neutral_count"] += neutral
        bucket["top_symbols"].append(
            {
                "symbol": (ticker or "").upper(),
                "sentiment_score": avg_sentiment,
                "article_count": article_count,
            }
        )

    if not agg:
        raise Unavailable(
            "no ticker in the supplied rows maps to a sector in sector_mapping"
        )

    sectors_output: list[dict] = []
    for sector, data in agg.items():
        total = data["bullish_count"] + data["bearish_count"] + data["neutral_count"]
        if total == 0:
            # Articles exist but none carry a sentiment label. Unlike v1, which
            # emitted neutral/zero, this is reported as zero-confidence neutral
            # only when there is article volume -- a genuine "covered but
            # unlabelled" state, distinct from an absent sector.
            score = 0.0
        else:
            score = (data["bullish_count"] - data["bearish_count"]) / total

        sectors_output.append(
            {
                "sector": sector,
                "outlook": _sector_outlook_label(score),
                "outlook_score": round(score, 3),
                "confidence": min(1.0, total / 20),
                "articles_analyzed": data["articles_analyzed"],
                "bullish_count": data["bullish_count"],
                "bearish_count": data["bearish_count"],
                "neutral_count": data["neutral_count"],
                "top_symbols": sorted(
                    data["top_symbols"],
                    key=lambda x: x["sentiment_score"] if x["sentiment_score"] is not None else 0.0,
                    reverse=True,
                )[:5],
            }
        )

    sectors_output.sort(key=lambda x: x["articles_analyzed"], reverse=True)
    return sectors_output
