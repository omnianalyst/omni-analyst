"""Company-scoped news perception.

Sibling of ``omni.ingest.news``. The flagship ``perception_divergence`` finding
compares fundamentals against perception for one entity; until this module
existed the only perception coverage was ``perception_macro`` on the macro
entity, so an AAPL gap had 1,379 fundamental inputs and zero perception inputs
and refused with "insufficient inputs".

This adapter attributes the same RSS headlines ``omni.ingest.news`` already
fetches to a single ticker (the gap key) and rolls them into one
``perception_news`` claim per (ticker, day). A perception reading is a summary
of a period; emitting one claim per article would make the gap engine treat
every headline as separate coverage of the same day, and a per-article
freshness decay would let one busy feed drown the rest.

Attribution is ``omni.capabilities.news.extract_ticker_entities`` verbatim.
Its regex matches any 2-5 letter all-caps token, so it surfaces real tickers
(NOV, IBM, PJT) and acronyms that merely look like them (AI, GAAP, EPS). A
smarter model is out of scope here -- the work order forbids a new sentiment
algorithm -- and any rewrite of attribution belongs in capabilities, not in an
adapter. The news draft carries only ``title`` (``url``/``feed`` aside); no
summary or body is stored, because reproducing either is republication, so
those arguments are passed empty rather than invented.

No sentiment score is written. Every scorer in ``omni.capabilities.news``
(``classify_sentiment``, ``aggregate_market_sentiment``, ``calculate_trend``)
takes a per-article polarity as input, and the module that produced those
polarities in v1 (TextBlob) is deliberately absent -- so feeding these helpers
would mean inventing the very numbers they run on. The claim carries article
and headline counts only: an honest weight a reader uses to gauge the reading,
not a fabricated score.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime

from omni.ingest.news import FEEDS, FeedFetcher, _fetch_feed, parse_feed
from omni.ingest.protocol import ClaimDraft, Unavailable

SOURCE = "rss"
PROVIDER_KEY = "rss"
CLAIM_TYPE = "perception_news"


def _utc_day(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def attribute(
    drafts: Iterable[ClaimDraft], ticker: str, aliases: tuple[str, ...] = ()
) -> list[ClaimDraft]:
    """Return the drafts whose headline refers to this company."""
    wanted = ticker.strip().upper()
    names = [a.strip() for a in (aliases or ()) if a and a.strip()]
    attributed: list[ClaimDraft] = []
    for d in drafts:
        if _mentions(d.value.get("title", ""), wanted, names):
            attributed.append(d)
    return attributed


def _mentions(title: str, ticker: str, aliases: list[str]) -> bool:
    """Does this headline refer to the company we are asking about?

    Deliberately not "extract every ticker, then filter". That approach reads
    any 2-5 letter capitalised token as a ticker, so a headline about the FAA
    or about GAAP earnings gets attributed to a company of that name, and
    sentiment is recorded against an entity nobody mentioned. Asking a narrow
    question instead of a broad one removes the whole class of false positive.

    Aliases matter because financial headlines say "Apple", not "AAPL". Without
    them this finds almost nothing, which is how it first behaved.
    """
    if re.search(rf"\b{re.escape(ticker)}\b", title):
        return True
    return any(
        re.search(rf"\b{re.escape(name)}\b", title, re.IGNORECASE)
        for name in aliases
    )


def _roll_up(attributed: list[ClaimDraft], ticker: str) -> list[ClaimDraft]:
    by_day: dict[datetime, list[ClaimDraft]] = defaultdict(list)
    for d in attributed:
        by_day[_utc_day(d.event_date)].append(d)

    rolled: list[ClaimDraft] = []
    for day, group in sorted(by_day.items()):
        with_headline = [
            d for d in group if d.value.get("title", "").strip()
        ]
        rolled.append(
            ClaimDraft(
                claim_type=CLAIM_TYPE,
                key=f"{ticker}:{day.date().isoformat()}",
                value={
                    "ticker": ticker,
                    "article_count": len(group),
                    "headline_count": len(with_headline),
                },
                event_date=day,
                knowledge_date=day,
                confidence=1.0,
            )
        )
    return rolled


class EntityNewsAdapter:
    source = SOURCE
    provider_key = PROVIDER_KEY
    entity_kinds = ("company",)

    def __init__(
        self,
        *,
        fetch_fn: FeedFetcher | None = None,
        aliases: tuple[str, ...] = (),
    ) -> None:
        #: Names the company is called in headlines. Financial press writes
        #: "Apple", not "AAPL"; without these this finds almost nothing.
        self._aliases = aliases
        self._fetch_fn = fetch_fn

    async def fetch(self, key: str) -> list[ClaimDraft]:
        ticker = key.strip().upper()
        if not ticker:
            raise Unavailable("entity-news gap key is empty; expected a ticker")
        fetch_fn = self._fetch_fn or _fetch_feed

        attributed: list[ClaimDraft] = []
        feed_failures: list[str] = []
        for feed_name, feed_url in FEEDS.items():
            try:
                payload = await fetch_fn(feed_url)
                drafts = parse_feed(payload, feed=feed_url)
            except Unavailable as exc:
                # One dead feed among several that do answer is not "no
                # reading" -- it is a thinner reading, and the article count
                # already records how thin. Only an empty union is unfillable.
                feed_failures.append(f"{feed_name}: {exc}")
                continue
            attributed.extend(attribute(drafts, ticker, self._aliases))

        if not attributed:
            tail = (
                f" (dead feeds: {'; '.join(feed_failures)})"
                if feed_failures
                else ""
            )
            raise Unavailable(f"no news attributed to ticker {ticker}{tail}")

        return _roll_up(attributed, ticker)
