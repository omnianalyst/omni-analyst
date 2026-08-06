"""RSS/Atom news ingestion.

Not ported from a single v1 module: ``news_sentiment_service.py`` is the
nearest ancestor, but only its RSS feed roster survived -- its VADER path
crashes on a None guard and its NewsAPI credentials always resolve to None,
so the parsing here is built fresh against real wire shapes. v1's roster is
the source of the feed list and nothing more.

A published article is knowable the moment it is published, so
``knowledge_date == event_date`` -- the cleanest bitemporal case in the
system, and the one a backtest can lean on without revision machinery.

Only ``title``, ``url`` and ``feed`` are stored. A headline plus a link is a
reference; reproducing the article body would republish copyrighted work,
which is the same class of mistake as the redistribution rule this layer
already enforces for licensed data.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

from omni.ingest.protocol import ClaimDraft, Unavailable, get_json

SOURCE = "rss"
PROVIDER_KEY = "rss"
CLAIM_TYPE = "news_event"

USER_AGENT = "omni-analyst-news/1.0 (+research)"
REQUEST_TIMEOUT = 30.0

# Feed roster, lifted from v1 ``news_sentiment_service.RSS_FEEDS``. Yahoo is
# dropped: yfinance is ``FALLBACK_PROHIBITED`` in the catalog and these
# endpoints are its scrape target. Reuters and Bloomberg are gone too -- the
# v1 URLs now fail DNS (reuters) and 301 to nothing useful (bloomberg), so
# they are omitted rather than pointed at dead hosts. CNBC, MarketWatch and
# Seeking Alpha still answer.
FEEDS: dict[str, str] = {
    "cnbc_top": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "cnbc_markets": "https://www.cnbc.com/id/15839069/device/rss/rss.html",
    "marketwatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "seeking_alpha": "https://seekingalpha.com/market_currents.xml",
}

FeedFetcher = Callable[[str], Awaitable[Any]]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, name: str) -> str | None:
    for child in el:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return None


def _atom_link(entry: ET.Element) -> str | None:
    # Atom <link> is empty with an href attribute; rel="alternate" (or no rel)
    # is the per-entry permalink. Other rels (self, enclosure) are not.
    fallback: str | None = None
    for child in entry:
        if _local(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if not href:
            continue
        rel = child.attrib.get("rel")
        if rel is None or rel == "alternate":
            return href
        if fallback is None:
            fallback = href
    return fallback


def _to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    # RSS pubDate is RFC 822 ("Tue, 28 Jul 2026 20:11:36 GMT"); Atom is
    # RFC 3339 ("2026-07-28T18:47:00.000000000Z"). Try the wire form first
    # because fromisoformat mis-handles some legacy timezone abbreviations.
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            # An RFC 822 date with no offset is ambiguous; defaulting to UTC
            # is an explicit choice, never to local wall-clock.
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _iter_items(root: ET.Element) -> list[tuple[str, ET.Element]]:
    """Return ``(format, element)`` pairs for every item/entry in the feed.

    RSS 2.0 wraps items under ``<channel>``; Atom lists ``<entry>`` directly
    under ``<feed>``; RSS 1.0 (RDF) leaves items at the root. Local-name
    matching sidesteps the Atom namespace so a real feed's
    ``{http://www.w3.org/2005/Atom}entry`` parses the same as a test fixture
    written without a namespace.
    """
    root_local = _local(root.tag)
    items: list[tuple[str, ET.Element]] = []
    if root_local == "feed":
        for child in root:
            if _local(child.tag) == "entry":
                items.append(("atom", child))
        return items
    if root_local == "rss":
        channel = next((c for c in root if _local(c.tag) == "channel"), None)
        if channel is not None:
            items.extend(
                ("rss", child)
                for child in channel
                if _local(child.tag) == "item"
            )
        return items
    for el in root.iter():
        loc = _local(el.tag)
        if loc == "item":
            items.append(("rss", el))
        elif loc == "entry":
            items.append(("atom", el))
    return items


def _stable_key(guid: str | None, url: str | None) -> str | None:
    if guid:
        return guid
    if url:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return None


def parse_feed(xml_or_dict: Any, *, feed: str) -> list[ClaimDraft]:
    """Flatten an RSS or Atom document into claim drafts.

    Accepts raw XML (``str``/``bytes``) or an already-parsed ``Element``. A
    parsed ``dict`` is not a shape either format produces; passing one is a
    wiring bug and raises rather than returning ``[]`` -- a silent empty list
    here would hide it, the same way a silent default hides a dead source.

    An item without a publication date is skipped: a news claim with no
    ``event_date`` has no place on the bitemporal axis, and stamping ``now()``
    on it would fabricate the moment the event happened.
    """
    if isinstance(xml_or_dict, (bytes, bytearray)):
        xml_or_dict = xml_or_dict.decode("utf-8", errors="replace")
    if isinstance(xml_or_dict, str):
        if not xml_or_dict.strip():
            raise Unavailable(f"feed {feed} returned an empty payload")
        try:
            root = ET.fromstring(xml_or_dict)
        except ET.ParseError as exc:
            raise Unavailable(f"feed {feed} is not parseable XML: {exc}") from exc
    elif isinstance(xml_or_dict, ET.Element):
        root = xml_or_dict
    else:
        raise TypeError(
            f"parse_feed expects XML text or an Element, got "
            f"{type(xml_or_dict).__name__}"
        )

    drafts: list[ClaimDraft] = []
    for fmt, el in _iter_items(root):
        if fmt == "atom":
            title = _child_text(el, "title")
            url = _atom_link(el)
            guid = _child_text(el, "id")
            when = _child_text(el, "published") or _child_text(el, "updated")
        else:
            title = _child_text(el, "title")
            url = _child_text(el, "link")
            guid = _child_text(el, "guid")
            when = _child_text(el, "pubDate")

        event_date = _to_datetime(when)
        if event_date is None:
            continue
        key = _stable_key(guid, url)
        if key is None:
            continue

        drafts.append(
            ClaimDraft(
                claim_type=CLAIM_TYPE,
                key=key,
                value={
                    "title": title or "",
                    "url": url or "",
                    "feed": feed,
                },
                event_date=event_date,
                knowledge_date=event_date,
                confidence=1.0,
            )
        )
    return drafts


async def _fetch_feed(url: str) -> str:
    import httpx

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, follow_redirects=True
    ) as client:
        response = await get_json(client, url, headers={"User-Agent": USER_AGENT})
        if response.status_code != 200:
            raise Unavailable(
                f"feed {url} returned HTTP {response.status_code}"
            )
        return response.text


class NewsAdapter:
    source = SOURCE
    provider_key = PROVIDER_KEY

    def __init__(self, *, fetch_fn: FeedFetcher | None = None) -> None:
        self._fetch_fn = fetch_fn

    async def fetch(self, key: str) -> list[ClaimDraft]:
        fetch_fn = self._fetch_fn or _fetch_feed
        payload = await fetch_fn(key)
        return parse_feed(payload, feed=key)
