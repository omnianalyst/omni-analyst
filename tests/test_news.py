"""News adapter (RSS + Atom).

Fixtures are real feed excerpts, copied from live responses on 2026-07-28, not
written to match the parser. The CNBC item is verbatim from
``https://www.cnbc.com/id/100003114/device/rss/rss.html`` (its ``<description>``
is the article summary, which the adapter must not store). The Atom entry is
verbatim from Google News'
``.../rss/search?q=stock+market&output=atom``; the opaque article URL is
truncated to a real prefix for readability -- every structural byte (Atom
namespace, ``<link href>``, nanosecond RFC-3339 timestamp, escaped-HTML
``<content>``) is the live shape.
"""

from datetime import UTC, datetime

import pytest

from omni.credentials.catalog import redistribution_for
from omni.ingest.news import FEEDS, NewsAdapter, parse_feed
from omni.ingest.protocol import Unavailable

CNBC_ITEM = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>US Top News and Analysis</title>
  <item>
    <title>Ford raises guidance after Q2 earnings beat, says F-Series recovery is on track</title>
    <link>https://www.cnbc.com/2026/07/28/ford-motor-f-earnings-q2-2026.html</link>
    <guid isPermaLink="false">108340522</guid>
    <pubDate>Tue, 28 Jul 2026 20:11:36 GMT</pubDate>
    <description><![CDATA[Ford cited operational improvements, resilient vehicle pricing and a high sales mix of profitable products for its performance and the improved guidance.]]></description>
  </item>
</channel></rss>
"""

# Two items: one with a guid, one without (forces the URL-hash key path).
SEEKING_ALPHA = """\
<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Breaking News on Seeking Alpha</title>
  <item>
    <title>Huron Consulting Non-GAAP EPS of $0.57 misses by $1.60</title>
    <link>https://seekingalpha.com/news/4619808-huron-consulting</link>
    <guid>https://seekingalpha.com/news/4619808-huron-consulting</guid>
    <pubDate>Tue, 28 Jul 2026 16:16:07 -0400</pubDate>
  </item>
  <item>
    <title>BXP FFO meets estimates</title>
    <link>https://seekingalpha.com/news/4619811-bxp-ffo-meets</link>
    <pubDate>Tue, 28 Jul 2026 15:02:00 -0400</pubDate>
  </item>
</channel></rss>
"""

GNEWS_ENTRY = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title type="html">"stock market" - Google News</title>
  <updated>2026-07-28T20:18:16.000000000Z</updated>
  <entry>
    <id>https://news.google.com/atom/articles/CBMi0wFBVV95cUxPdWcyMXVZYm1uU214UnVmVUNz?oc=5</id>
    <title type="html">Stock Market Today: Dow rises 575 points after strong earnings from Coca-Cola and Sherwin-Williams</title>
    <updated>2026-07-28T18:47:00.000000000Z</updated>
    <link href="https://news.google.com/atom/articles/CBMi0wFBVV95cUxPdWcyMXVZYm1uU214UnVmVUNz?oc=5" type="text/html"/>
    <content type="html">&lt;ol&gt;&lt;li&gt;&lt;a href="https://news.google.com/"&gt;full article&lt;/a&gt;&lt;/li&gt;&lt;/ol&gt;</content>
  </entry>
</feed>
"""

EMPTY_RSS = """\
<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>A feed that has stopped publishing</title>
  <description>no items</description>
</channel></rss>
"""


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


class TestParsing:
    def test_an_rss_item_parses_into_one_draft(self):
        drafts = parse_feed(CNBC_ITEM, feed="cnbc_top")
        assert len(drafts) == 1
        d = drafts[0]
        assert d.claim_type == "news_event"
        assert d.value["title"].startswith("Ford raises guidance")
        assert d.value["url"] == (
            "https://www.cnbc.com/2026/07/28/ford-motor-f-earnings-q2-2026.html"
        )
        assert d.value["feed"] == "cnbc_top"
        assert d.event_date == _at("2026-07-28T20:11:36")

    def test_an_atom_entry_parses_into_one_draft(self):
        drafts = parse_feed(GNEWS_ENTRY, feed="google_news")
        assert len(drafts) == 1
        d = drafts[0]
        assert d.value["title"].startswith("Stock Market Today")
        # Atom's <link href> is the URL, not the element's (empty) text.
        assert d.value["url"].startswith("https://news.google.com/atom/articles/")
        assert d.event_date == _at("2026-07-28T18:47:00")

    def test_knowledge_date_equals_event_date(self):
        # A published article is knowable when it is published.
        for fixture, feed in [(CNBC_ITEM, "cnbc_top"), (GNEWS_ENTRY, "google_news")]:
            drafts = parse_feed(fixture, feed=feed)
            for d in drafts:
                assert d.knowledge_date == d.event_date

    def test_the_guid_is_the_claim_key(self):
        drafts = parse_feed(CNBC_ITEM, feed="cnbc_top")
        assert drafts[0].key == "108340522"

    def test_the_atom_id_is_the_claim_key(self):
        drafts = parse_feed(GNEWS_ENTRY, feed="google_news")
        assert drafts[0].key == drafts[0].value["url"]

    def test_a_missing_guid_falls_back_to_a_deterministic_url_hash(self):
        drafts = parse_feed(SEEKING_ALPHA, feed="seeking_alpha")
        no_guid = next(d for d in drafts if d.value["url"].endswith("bxp-ffo-meets"))
        # Same input -> same hash, every run.
        again = parse_feed(SEEKING_ALPHA, feed="seeking_alpha")
        twin = next(d for d in again if d.value["url"].endswith("bxp-ffo-meets"))
        assert no_guid.key == twin.key
        assert len(no_guid.key) == 16

    def test_a_missing_guid_never_collides_with_a_present_guid(self):
        drafts = parse_feed(SEEKING_ALPHA, feed="seeking_alpha")
        keys = {d.key for d in drafts}
        assert len(keys) == 2

    def test_the_same_item_has_a_stable_key_across_two_parses(self):
        first = parse_feed(CNBC_ITEM, feed="cnbc_top")[0]
        second = parse_feed(CNBC_ITEM, feed="cnbc_top")[0]
        assert first.key == second.key

    def test_an_item_without_a_publication_date_is_skipped_not_dated_now(self):
        xml = (
            '<rss version="2.0"><channel>'
            "<item><title>undated</title><link>https://x/y</link><guid>g</guid></item>"
            "<item><title>dated</title><link>https://x/z</link>"
            "<guid>h</guid><pubDate>Tue, 28 Jul 2026 20:11:36 GMT</pubDate></item>"
            "</channel></rss>"
        )
        drafts = parse_feed(xml, feed="f")
        assert len(drafts) == 1
        assert drafts[0].value["title"] == "dated"

    def test_no_article_body_text_is_stored_in_value(self):
        # CNBC <description> and Atom <content> both carry body text that must
        # never reach the claim -- reproducing it is republication.
        rss = parse_feed(CNBC_ITEM, feed="cnbc_top")[0]
        atom = parse_feed(GNEWS_ENTRY, feed="google_news")[0]
        for d in (rss, atom):
            assert set(d.value) == {"title", "url", "feed"}
        assert "resilient vehicle pricing" not in str(rss.value)
        assert "full article" not in str(atom.value)

    def test_an_rss_offset_timestamp_is_normalised_to_utc(self):
        drafts = parse_feed(SEEKING_ALPHA, feed="seeking_alpha")
        huron = next(
            d for d in drafts if "Huron" in d.value["title"]
        )
        assert huron.event_date == _at("2026-07-28T20:16:07")

    def test_a_feed_with_no_items_returns_an_empty_list(self):
        assert parse_feed(EMPTY_RSS, feed="stale") == []

    def test_bytes_payload_parse_like_text(self):
        assert parse_feed(CNBC_ITEM.encode("utf-8"), feed="cnbc_top") == parse_feed(
            CNBC_ITEM, feed="cnbc_top"
        )

    def test_unparseable_xml_raises_unavailable(self):
        with pytest.raises(Unavailable, match="not parseable XML"):
            parse_feed("<rss><channel><item><oops></channel></rss>", feed="broken")

    def test_an_empty_payload_is_unavailable_not_an_empty_list(self):
        # An honest empty feed is valid XML with no items. An empty payload is
        # a source that returned nothing -- conflating the two would mask a
        # transport failure as 'nothing happened today'.
        with pytest.raises(Unavailable, match="empty payload"):
            parse_feed("", feed="dead")
        with pytest.raises(Unavailable, match="empty payload"):
            parse_feed("   \n  ", feed="dead")


class TestAdapter:
    async def test_an_injected_fetcher_needs_no_network(self):
        async def fake(feed_url: str) -> str:
            assert feed_url == FEEDS["cnbc_top"]
            return CNBC_ITEM

        drafts = await NewsAdapter(fetch_fn=fake).fetch(FEEDS["cnbc_top"])
        assert len(drafts) == 1
        assert drafts[0].value["feed"] == FEEDS["cnbc_top"]

    async def test_a_non_200_response_raises_unavailable_carrying_the_status(self):
        async def broken(feed_url: str) -> str:
            raise Unavailable(f"feed {feed_url} returned HTTP 503")

        with pytest.raises(Unavailable, match="503"):
            await NewsAdapter(fetch_fn=broken).fetch(FEEDS["cnbc_markets"])

    async def test_a_source_error_propagates_rather_than_returning_nothing(self):
        async def dead(feed_url: str) -> str:
            raise Unavailable(f"connection reset for {feed_url}")

        with pytest.raises(Unavailable, match="connection reset"):
            await NewsAdapter(fetch_fn=dead).fetch(FEEDS["marketwatch"])

    async def test_an_empty_feed_yields_no_drafts(self):
        async def empty(feed_url: str) -> str:
            return EMPTY_RSS

        assert await NewsAdapter(fetch_fn=empty).fetch(FEEDS["seeking_alpha"]) == []

    def test_the_adapter_declares_rss_for_licence_lookup(self):
        adapter = NewsAdapter()
        assert adapter.source == "rss"
        assert adapter.provider_key == "rss"

    def test_every_advertised_feed_has_a_named_entry_in_the_roster(self):
        assert set(FEEDS) == {
            "cnbc_top",
            "cnbc_markets",
            "marketwatch",
            "seeking_alpha",
        }
        for name, url in FEEDS.items():
            assert url.startswith("https://"), name

    def test_yahoo_is_not_in_the_roster(self):
        # yfinance is FALLBACK_PROHIBITED; its scrape target must not appear
        # here even though v1 listed it.
        assert "yahoo" not in FEEDS
        assert not any("yahoo" in n or "yahoo" in u for n, u in FEEDS.items())


class TestRedistribution:
    def test_rss_resolves_to_allowed(self):
        assert redistribution_for("rss") == "allowed"

    def test_rss_needs_no_credential(self):
        from omni.credentials.catalog import PROVIDER_CATALOG

        entry = PROVIDER_CATALOG["rss"]
        assert entry["key_required"] is False
        assert entry["fallback"] == "allowed"


class TestDraftInvariants:
    def test_a_news_draft_carries_no_unit_or_evidence_by_default(self):
        drafts = parse_feed(CNBC_ITEM, feed="cnbc_top")
        assert drafts[0].unit is None
        assert drafts[0].evidence is None

    def test_confidence_is_recorded(self):
        # The feed is authoritative about what it published; the claim is the
        # existence of the coverage, not the truth of the article's allegations.
        assert parse_feed(CNBC_ITEM, feed="cnbc_top")[0].confidence == 1.0
