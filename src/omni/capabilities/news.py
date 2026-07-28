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
from collections.abc import Awaitable, Callable
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
    if sentiment == "bullish":
        impact_score = base_impact * (1 + len(affected_tickers) * 0.1)
    elif sentiment == "bearish":
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
