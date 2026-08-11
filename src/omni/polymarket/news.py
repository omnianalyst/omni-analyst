"""Document providers for Stage A: feed the LLM pre-cutoff evidence beyond the
market question alone.

The default provider in `calibrate.prepare_snapshot` returns just the question
as a single document — which is why test_run_8 saw the LLM default to 0.5 on
7/15 markets. There was nothing to reason about.

This module provides three composable sources:

1. `gamma_description_provider` — fetches the market's full `description` and
   `groupItemTitle` from Gamma. Polymarket's descriptions vary wildly in
   quality (some are templated boilerplate, some have actual background) but
   they are free, keyless, and per-market. Always-on base layer.

2. `gdelt_news_provider` — queries the GDELT Project's free DOC API for news
   articles whose titles mention the market's keywords, dated strictly before
   the cutoff. GDELT is free, requires no key, and covers global news in
   English. It returns article *metadata* (title, url, date, domain), not
   full text — full-text fetching is a follow-up that requires per-publisher
   scraping and is out of scope for the first run.

3. `compose_providers` — runs a list of providers in order and concatenates
   their documents. The runner uses this to wire description + news as the
   default.

All providers honour the cutoff: a document dated at-or-after cutoff is never
returned. `MarketAtCutoff.__post_init__` would refuse it anyway; the filter
here keeps the failure from being a per-construction crash.

Honest limitations:
- GDELT's title-only surface misses nuance. A real Stage A would add full-text
  fetching from a subset of high-quality open publishers (Reuters, AP, etc.).
- Keyword extraction is naive: question minus stopwords. A real Stage A would
  use entity extraction. Both are POLY4 territory.
- GDELT rate-limits at ~1 req/sec. With 200 markets that is ~3 minutes of news
  fetching before the LLM calls even start.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx

from omni.ingest.protocol import Unavailable
from omni.polymarket.gamma import GAMMA_BASE_URL
from omni.polymarket.types import Document, ResolvedMarket

GDELT_BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
JINA_READER_BASE_URL = "https://r.jina.ai"

# Jina Reader returns clean markdown for any URL. Free tier is rate-limited
# (~20 req/min) and slow (1-3s/req). For a Stage A run with 30 markets x 2-3
# bodies each, this is 60-90 sequential fetches = 2-5 minutes of additional
# wall time. Worth it per-market for the richer context; not worth it for
# every article. The body provider therefore fetches AT MOST max_articles
# bodies, picked from the top of the GDELT relevance ranking.
JINA_READER_TIMEOUT = 20.0

_STOPWORDS = frozenset({
    "a", "an", "the", "will", "is", "are", "be", "of", "on", "in", "at",
    "by", "or", "and", "to", "for", "with", "above", "below", "before",
    "after", "this", "that", "it", "its", "as", "from", "more", "less",
    "than", "did", "does", "do", "has", "have", "had", "not", "but",
    "yes", "no", "if", "then", "when", "year", "month", "day", "week",
    "end", "first", "last", "next", "prior", "vs",
})

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9']+")


def _extract_keywords(question: str, *, max_terms: int = 6) -> str:
    """Pull the most-likely-informative terms out of a Polymarket question.

    GDELT's query parser handles `"exact phrase"` AND `keyword`. We do not try
    to build a structured query; we hand it a space-joined bag of capitalised
    proper-noun candidates (longer words, stopwords removed) and let its own
    relevance ranker handle the rest. A real implementation would do NER; this
    is a one-pass heuristic and is honest about being one.
    """
    words = _WORD_RE.findall(question)
    candidates = [
        w for w in words
        if w.lower() not in _STOPWORDS and len(w) >= 3
    ]
    # Prefer capitalised words (proper nouns, tickers) but fall back to all.
    proper = [w for w in candidates if w[0].isupper()]
    chosen = proper if len(proper) >= 2 else candidates
    return " ".join(chosen[:max_terms])


def _gdelt_datetime(dt: datetime) -> str:
    """GDELT's `startdatetime`/`enddatetime` format: YYYYMMDDHHMMSS."""
    return dt.strftime("%Y%m%d%H%M%S")


async def gamma_description_provider(
    client: httpx.AsyncClient,
    market: ResolvedMarket,
    cutoff: datetime,
) -> tuple[Document, ...]:
    """Pull the market's full description from Gamma. Falls back to empty if
    the description is empty or the request fails — the harness's default
    market-question document already covers the trivial case.
    """
    try:
        resp = await client.get(f"{GAMMA_BASE_URL}/markets/{market.condition_id}")
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError:
        return ()
    if not isinstance(payload, dict):
        return ()

    description = (payload.get("description") or "").strip()
    group_title = (payload.get("groupItemTitle") or payload.get("groupTitle") or "").strip()
    parts = []
    if group_title:
        parts.append(f"group: {group_title}")
    if description:
        parts.append(description)
    if not parts:
        return ()

    text = "\n".join(parts)
    return (
        Document(
            at=market.created_at,
            source=f"polymarket:description:{market.condition_id}",
            text=text[:2000],
        ),
    )


async def _fetch_gdelt_articles(
    client: httpx.AsyncClient,
    market: ResolvedMarket,
    cutoff: datetime,
    *,
    max_articles: int,
    lookback_days: int = 14,
) -> tuple[dict, ...]:
    """Shared GDELT fetch+parse for the title provider and the body provider.

    Returns the raw GDELT article dicts (with `title`, `url`, `seendate`,
    `domain`) so each consumer can build the Document shape it needs without
    re-fetching. Both providers where this is used filter on `seendate < cutoff`
    the same way; a single source of truth for the cutoff filter keeps them
    from drifting apart.
    """
    keywords = _extract_keywords(market.question)
    if not keywords:
        return ()

    start = cutoff - timedelta(days=lookback_days)
    params = {
        "query": keywords,
        "mode": "ArtList",
        "format": "json",
        "sort": "HybridRel",
        "maxrecords": str(max_articles),
        "startdatetime": _gdelt_datetime(start),
        "enddatetime": _gdelt_datetime(cutoff),
    }
    try:
        resp = await client.get(GDELT_BASE_URL, params=params, timeout=15.0)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError):
        return ()

    articles = payload.get("articles") if isinstance(payload, dict) else None
    if not isinstance(articles, list):
        return ()

    out: list[dict] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        seen = article.get("seendate")
        try:
            at = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        if at >= cutoff:
            continue
        article["_at"] = at
        out.append(article)
    return tuple(out[:max_articles])


async def gdelt_news_provider(
    client: httpx.AsyncClient,
    market: ResolvedMarket,
    cutoff: datetime,
    *,
    max_articles: int = 5,
    lookback_days: int = 14,
) -> tuple[Document, ...]:
    """Query GDELT for news articles about the market's topic, pre-cutoff.

    Returns up to `max_articles` documents, each the article's title + domain.
    The cutoff is enforced both in the query (`enddatetime`) and on the
    returned articles (`seendate`), because GDELT occasionally returns articles
    whose indexed date is later than the requested end.

    No article text — title only. Use `article_body_provider` for full bodies.
    """
    articles = await _fetch_gdelt_articles(
        client, market, cutoff, max_articles=max_articles, lookback_days=lookback_days,
    )
    docs: list[Document] = []
    for article in articles:
        title = (article.get("title") or "").strip()
        if not title:
            continue
        domain = article.get("domain") or "unknown"
        docs.append(
            Document(
                at=article["_at"],
                source=f"gdelt:{domain}",
                text=title[:500],
            )
        )
    return tuple(docs)


async def article_body_provider(
    client: httpx.AsyncClient,
    market: ResolvedMarket,
    cutoff: datetime,
    *,
    max_articles: int = 3,
    max_chars_per_article: int = 1500,
    lookback_days: int = 14,
) -> tuple[Document, ...]:
    """Fetch full article bodies via Jina Reader for the top GDELT hits.

    Jina Reader (`https://r.jina.ai/<url>`) returns clean markdown for any
    URL via a single GET. Free tier, no key. Slow (~1-3s/req) and rate-limited
    (~20/min). For a Stage A run this provider is the dominant wall-time cost
    after the LLM itself — kept opt-in via `--article-body`.

    Failures (HTTP error, non-text response, empty body) are silent: the
    caller still has the title-only documents from `gdelt_news_provider`. The
    composite chain in the runner places titles before bodies, so an article
    that fails body fetch still contributes its title.
    """
    articles = await _fetch_gdelt_articles(
        client, market, cutoff, max_articles=max_articles, lookback_days=lookback_days,
    )
    if not articles:
        return ()

    docs: list[Document] = []
    for article in articles:
        url = article.get("url") or article.get("url_mobile")
        if not url:
            continue
        try:
            resp = await client.get(
                f"{JINA_READER_BASE_URL}/{url}",
                timeout=JINA_READER_TIMEOUT,
                headers={"Accept": "text/plain"},
            )
            resp.raise_for_status()
            body = resp.text
        except httpx.HTTPError:
            continue
        if not body.strip():
            continue
        domain = article.get("domain") or "unknown"
        docs.append(
            Document(
                at=article["_at"],
                source=f"jina:{domain}",
                text=body[:max_chars_per_article],
            )
        )
    return tuple(docs)


DocumentProvider = Callable[
    [httpx.AsyncClient, ResolvedMarket, datetime],
    Awaitable[tuple[Document, ...]],
]


def compose_providers(
    *providers: DocumentProvider,
) -> DocumentProvider:
    """Run providers in order, concatenate their documents.

    A provider that raises `Unavailable` is treated as having no documents —
    a news outage on one source should not abort the run, and the harness's
    exclusions list will surface the failure separately.
    """
    async def _composed(
        client: httpx.AsyncClient,
        market: ResolvedMarket,
        cutoff: datetime,
    ) -> tuple[Document, ...]:
        all_docs: list[Document] = []
        for provider in providers:
            try:
                docs = await provider(client, market, cutoff)
            except Unavailable:
                continue
            all_docs.extend(docs)
        return tuple(all_docs)
    return _composed


def default_news_provider() -> DocumentProvider:
    """The composite provider the runner uses: Gamma description + GDELT news.

    The order matters for prompt readability — the market's own description
    comes first (it grounds the topic), then news articles build on it.
    """
    return compose_providers(gamma_description_provider, gdelt_news_provider)


# Curated keyword sets used by `derive_category`. Polymarket's `category` field
# on /markets is `None` for ~all rows (verified empirically), so a per-market
# classifier is the only practical way to slice the universe. The sets are
# deliberately conservative: misclassification would silently dilute a
# per-category run, so a market with no signal defaults to "Other" rather
# than to a best-guess bucket.
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Crypto", (
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
        "crypto", "binance", "coinbase", "xrp", "ripple", "dogecoin",
        "cardano", "ada", "polygon", "matic",
    )),
    ("Politics", (
        "trump", "biden", "harris", "desantis", "obama", "president",
        "election", "senate", "congress", "governor", "primary",
        "democrat", "republican", "nominee", "gop", "dnc", "rnc",
    )),
    ("Economics", (
        "fed ", "fomc", "cpi", "gdp", "unemployment", "inflation",
        "recession", "interest rate", "powell", "treasury", "yield",
        "nonfarm", "payroll", "pmi", "consumer confidence",
    )),
    ("Sports", (
        "nfl", "nba", "mlb", "nhl", "epl", "uefa", "champions league",
        "premier league", "lakers", "celtics", "warriors", "knicks",
        "yankees", "red sox", "astros", "braves", " chargers",
        "over ", "under ", "vs ", "completed match",
    )),
    ("Geopolitics", (
        "putin", "russia", "ukraine", "kiev", "kyiv",
        "china", "xi jinping", "beijing", "taiwan",
        "israel", "gaza", "hamas", "hezbollah", "netanyahu",
        "iran", "ayatollah",
    )),
)


def derive_category(market: ResolvedMarket) -> str:
    """Best-effort category from the market question. Returns 'Other' when no
    keyword set matches — preferred over a wrong guess, because a wrong guess
    silently dilutes a per-category calibration run.

    Uses the question only; the Gamma-supplied `category` field is null for
    nearly every resolved market we have observed and is not consulted.
    """
    text = market.question.lower()
    for label, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return label
    return "Other"


__all__ = [
    "GDELT_BASE_URL",
    "JINA_READER_BASE_URL",
    "article_body_provider",
    "compose_providers",
    "default_news_provider",
    "derive_category",
    "gamma_description_provider",
    "gdelt_news_provider",
]