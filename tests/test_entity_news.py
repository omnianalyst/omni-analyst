"""EntityNewsAdapter -- company-scoped news perception.

Every fixture below is a real feed excerpt copied verbatim from a live response
on 2026-07-28 (CNBC id/15839069 markets, CNBC id/100003114 top news,
MarketWatch mw_topstories, Seeking Alpha market_currents). Titles, links,
guids and pubDates are the wire values; only the surrounding channel wrapper is
trimmed to the items under test, the same way tests/test_news.py trims to one
CNBC item. Nothing here is written from memory.

"AI" is the token ``extract_ticker_entities`` surfaces from six real headlines
on 2026-07-28 (the technology initialism, not the C3.ai listing). It is used
for the aggregation case precisely because it is what the real feeds and the
real heuristic produce -- a cleaner ticker does not appear more than once in the
same day across these feeds, and inventing one would repeat the very mistake
this fixture discipline exists to prevent. "IBM" (one real headline, clean
ticker) covers the unambiguous attribution case.
"""

from datetime import UTC, datetime

import pytest

from omni.credentials.catalog import redistribution_for
from omni.ingest.entity_news import EntityNewsAdapter, attribute
from omni.ingest.news import FEEDS, parse_feed
from omni.ingest.protocol import Unavailable

CNBC_MARKETS_IBM = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>CNBC Markets</title>
  <item>
    <title>Historic IBM stock crash sets up unique options strategy</title>
    <link>https://www.cnbc.com/2026/07/15/historic-ibm-stock-crash-sets-up-unique-options-strategy.html</link>
    <guid isPermaLink="false">108335488</guid>
    <pubDate>Thu, 16 Jul 2026 19:49:17 GMT</pubDate>
  </item>
  <item>
    <title>SpaceX has now lost the equivalent of a full Tesla in market capitalization</title>
    <link>https://www.cnbc.com/2026/07/27/spacex-has-now-lost-the-equivalent-of-a-full-tesla-in-market-capitalization.html</link>
    <guid isPermaLink="false">108340747</guid>
    <pubDate>Tue, 28 Jul 2026 15:38:04 GMT</pubDate>
  </item>
</channel></rss>
"""

# Six real MarketWatch + CNBC top-news headlines from 2026-07-28, each
# mentioning "AI". MarketWatch's apostrophe in headline 3 is the live curly
# form; it is preserved byte-for-byte.
MW_AI = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>MarketWatch Top Stories</title>
  <item>
    <title>Bloom Energy sees sales top $1 billion as AI proves a validation moment for fuel-cell technology</title>
    <link>https://www.marketwatch.com/story/bloom-energy-sees-sales-top-1-billion-as-ai-proves-a-validation-moment-for-fuel-cell-technology-5cb69476?mod=mw_rss_topstories</link>
    <guid isPermaLink="false">WP-MKTW-0005149937</guid>
    <pubDate>Tue, 28 Jul 2026 21:17:00 GMT</pubDate>
  </item>
  <item>
    <title>Alphabet and Tesla took a hit from soaring AI spending. Will Microsoft, Meta and Amazon be next?</title>
    <link>https://www.marketwatch.com/story/alphabet-and-tesla-took-a-hit-from-soaring-ai-spending-will-microsoft-meta-and-amazon-be-next-80ecb30b?mod=mw_rss_topstories</link>
    <guid isPermaLink="false">WP-MKTW-0005148322</guid>
    <pubDate>Tue, 28 Jul 2026 20:52:00 GMT</pubDate>
  </item>
  <item>
    <title>Microsoft is making a $190 billion AI gamble \u2014 and investors will soon see if it\u2019s paying off</title>
    <link>https://www.marketwatch.com/story/microsoft-is-making-a-190-billion-ai-gamble-and-investors-will-soon-see-if-its-paying-off-b53bd89a?mod=mw_rss_topstories</link>
    <guid isPermaLink="false">WP-MKTW-0005149760</guid>
    <pubDate>Tue, 28 Jul 2026 20:33:00 GMT</pubDate>
  </item>
</channel></rss>
"""

CNBC_TOP_AI = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>US Top News and Analysis</title>
  <item>
    <title>Visa is cutting 7% of employees in efficiency push as AI reshapes work</title>
    <link>https://www.cnbc.com/2026/07/28/visa-is-cutting-7percent-of-employees-in-efficiency-push-as-ai-reshapes-work.html</link>
    <guid isPermaLink="false">108341080</guid>
    <pubDate>Tue, 28 Jul 2026 15:35:48 GMT</pubDate>
  </item>
  <item>
    <title>The Dow jumps as the AI trade wobbles. Plus, a portfolio name goes on the M&amp;A hunt</title>
    <link>https://www.cnbc.com/2026/07/28/the-dow-jumps-as-the-ai-trade-wobbles-plus-a-portfolio-name-goes-on-the-ma-hunt.html</link>
    <guid isPermaLink="false">108341285</guid>
    <pubDate>Tue, 28 Jul 2026 19:15:15 GMT</pubDate>
  </item>
  <item>
    <title>The future of Wall Street is here as startups and brokers build AI agents to trade 24/7</title>
    <link>https://www.cnbc.com/2026/07/28/ai-agents-build-to-trade-24/7-the-future-of-wall-street.html</link>
    <guid isPermaLink="false">108333440</guid>
    <pubDate>Tue, 28 Jul 2026 16:16:25 GMT</pubDate>
  </item>
</channel></rss>
"""

SA_NONE = """\
<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Breaking News on Seeking Alpha</title>
  <item>
    <title>Ford beats Q2 estimates, raises outlook despite sales headwinds - update</title>
    <link>https://seekingalpha.com/news/4619761-ford-beats-q2-estimates-raises-outlook-despite-sales-headwinds?utm_source=feed_news_all&amp;utm_medium=referral&amp;feed_item_type=news</link>
    <guid>https://seekingalpha.com/news/4619761-ford-beats-q2-estimates-raises-outlook-despite-sales-headwinds?utm_source=feed_news_all&amp;utm_medium=referral&amp;feed_item_type=news</guid>
    <pubDate>Tue, 28 Jul 2026 17:51:22 -0400</pubDate>
  </item>
</channel></rss>
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


def _router(by_name: dict[str, str]):
    """Build a fetch_fn that serves a fixture per feed name from FEEDS."""

    url_to_fixture = {FEEDS[name]: xml for name, xml in by_name.items()}

    async def fetch_fn(feed_url: str) -> str:
        try:
            return url_to_fixture[feed_url]
        except KeyError as exc:
            raise Unavailable(f"no fixture mapped for {feed_url}") from exc

    return fetch_fn


class TestAttribution:
    def test_a_headline_naming_a_ticker_is_attributed_to_it(self):
        drafts = parse_feed(CNBC_MARKETS_IBM, feed="cnbc_markets")
        ibm = attribute(drafts, "IBM")
        assert len(ibm) == 1
        assert ibm[0].value["title"].startswith("Historic IBM")

    def test_a_headline_that_names_no_ticker_is_not_attributed(self):
        drafts = parse_feed(CNBC_MARKETS_IBM, feed="cnbc_markets")
        # The SpaceX/Tesla item carries no all-caps token, so it must not be
        # swept up when the gap asks for IBM.
        assert attribute(drafts, "IBM")[0].value["title"].startswith(
            "Historic IBM"
        )
        assert attribute(drafts, "SPACEX") == []
        assert attribute(drafts, "TESLA") == []

    def test_attribution_is_case_insensitive_on_the_gap_key(self):
        drafts = parse_feed(CNBC_MARKETS_IBM, feed="cnbc_markets")
        assert {d.value["title"] for d in attribute(drafts, "ibm")} == {
            "Historic IBM stock crash sets up unique options strategy"
        }

    def test_every_mentioning_headline_is_attributed_none_of_the_others(self):
        drafts = parse_feed(MW_AI, feed="marketwatch") + parse_feed(
            CNBC_TOP_AI, feed="cnbc_top"
        )
        assert len(drafts) == 6
        assert len(attribute(drafts, "AI")) == 6
        assert attribute(drafts, "AAPL") == []

    def test_attribution_reads_only_the_title_needs_no_summary_or_body(self):
        # The news draft stores title/url/feed only; summary and body are not
        # carried (and must not be), so attribution has to work without them.
        drafts = parse_feed(CNBC_MARKETS_IBM, feed="cnbc_markets")
        ibm = attribute(drafts, "IBM")[0]
        assert set(ibm.value) == {"title", "url", "feed"}


class TestRollUp:
    async def test_one_claim_per_day_not_one_per_article(self):
        fetch_fn = _router(
            {
                "cnbc_top": CNBC_TOP_AI,
                "cnbc_markets": EMPTY_RSS,
                "marketwatch": MW_AI,
                "seeking_alpha": EMPTY_RSS,
            }
        )
        drafts = await EntityNewsAdapter(fetch_fn=fetch_fn).fetch("AI")
        # Six attributing headlines on 2026-07-28 collapse to a single claim.
        assert len(drafts) == 1
        assert drafts[0].value["article_count"] == 6
        assert drafts[0].value["headline_count"] == 6

    async def test_distinct_days_become_distinct_claims(self):
        # IBM is mentioned on 2026-07-16, AI on 2026-07-28. Each fetch is one
        # ticker; this asserts the day key keeps same-ticker, different-day
        # readings apart rather than merging them.
        fetch_fn = _router(
            {
                "cnbc_top": CNBC_TOP_AI,
                "cnbc_markets": CNBC_MARKETS_IBM,
                "marketwatch": MW_AI,
                "seeking_alpha": EMPTY_RSS,
            }
        )
        ibm = await EntityNewsAdapter(fetch_fn=fetch_fn).fetch("IBM")
        ai = await EntityNewsAdapter(fetch_fn=fetch_fn).fetch("AI")
        assert len(ibm) == 1
        assert len(ai) == 1
        assert ibm[0].event_date == _at("2026-07-16T00:00:00")
        assert ai[0].event_date == _at("2026-07-28T00:00:00")
        assert ibm[0].key != ai[0].key

    async def test_knowledge_date_equals_event_date(self):
        fetch_fn = _router(
            {
                "cnbc_top": CNBC_TOP_AI,
                "cnbc_markets": EMPTY_RSS,
                "marketwatch": MW_AI,
                "seeking_alpha": EMPTY_RSS,
            }
        )
        draft = (await EntityNewsAdapter(fetch_fn=fetch_fn).fetch("AI"))[0]
        assert draft.knowledge_date == draft.event_date

    async def test_value_carries_article_count_to_weigh_a_reading(self):
        fetch_fn = _router(
            {
                "cnbc_top": CNBC_TOP_AI,
                "cnbc_markets": CNBC_MARKETS_IBM,
                "marketwatch": MW_AI,
                "seeking_alpha": EMPTY_RSS,
            }
        )
        # IBM: one attributing headline (the SpaceX sibling does not name a
        # ticker) -> article_count 1. A reader weighs that against the 6-article
        # AI reading below; without the count the two scores would look equal.
        ibm = (await EntityNewsAdapter(fetch_fn=fetch_fn).fetch("IBM"))[0]
        ai = (await EntityNewsAdapter(fetch_fn=fetch_fn).fetch("AI"))[0]
        assert ibm.value["article_count"] == 1
        assert ai.value["article_count"] == 6
        assert ibm.value["article_count"] != ai.value["article_count"]

    async def test_claim_type_and_key_are_perception_news(self):
        fetch_fn = _router(
            {
                "cnbc_top": CNBC_TOP_AI,
                "cnbc_markets": EMPTY_RSS,
                "marketwatch": MW_AI,
                "seeking_alpha": EMPTY_RSS,
            }
        )
        draft = (await EntityNewsAdapter(fetch_fn=fetch_fn).fetch("AI"))[0]
        assert draft.claim_type == "perception_news"
        assert draft.key == "AI:2026-07-28"


class TestUnavailable:
    async def test_a_ticker_with_no_coverage_raises_not_neutral(self):
        fetch_fn = _router(
            {
                "cnbc_top": CNBC_TOP_AI,
                "cnbc_markets": CNBC_MARKETS_IBM,
                "marketwatch": MW_AI,
                "seeking_alpha": SA_NONE,
            }
        )
        # None of the live headlines above mention AAPL, so this is the honest
        # "no reading" case, not a neutral score.
        with pytest.raises(Unavailable, match="no news attributed to ticker AAPL"):
            await EntityNewsAdapter(fetch_fn=fetch_fn).fetch("AAPL")

    async def test_a_dead_feed_among_live_ones_does_not_blank_coverage(self):
        async def fetch_fn(feed_url: str) -> str:
            if feed_url == FEEDS["cnbc_markets"]:
                raise Unavailable("cnbc_markets returned HTTP 503")
            if feed_url == FEEDS["cnbc_top"]:
                return CNBC_TOP_AI
            if feed_url == FEEDS["marketwatch"]:
                return MW_AI
            return EMPTY_RSS

        drafts = await EntityNewsAdapter(fetch_fn=fetch_fn).fetch("AI")
        assert len(drafts) == 1
        assert drafts[0].value["article_count"] == 6

    async def test_every_feed_dead_raises_unavailable_naming_the_ticker(self):
        async def fetch_fn(feed_url: str) -> str:
            raise Unavailable(f"{feed_url} connection reset")

        with pytest.raises(Unavailable, match="no news attributed to ticker AI"):
            await EntityNewsAdapter(fetch_fn=fetch_fn).fetch("AI")

    async def test_an_empty_gap_key_is_unavailable(self):
        with pytest.raises(Unavailable, match="gap key is empty"):
            await EntityNewsAdapter(
                fetch_fn=_router({"cnbc_top": EMPTY_RSS})
            ).fetch("")


class TestLicence:
    def test_the_adapter_declares_rss_for_the_shareable_class(self):
        adapter = EntityNewsAdapter()
        assert adapter.source == "rss"
        assert adapter.provider_key == "rss"
        assert adapter.entity_kinds == ("company",)

    def test_rss_resolves_to_allowed_so_claims_accumulate_shared(self):
        assert redistribution_for("rss") == "allowed"


class TestNoScoreIsFabricated:
    async def test_value_has_no_score_field_only_counts(self):
        fetch_fn = _router(
            {
                "cnbc_top": CNBC_TOP_AI,
                "cnbc_markets": EMPTY_RSS,
                "marketwatch": MW_AI,
                "seeking_alpha": EMPTY_RSS,
            }
        )
        draft = (await EntityNewsAdapter(fetch_fn=fetch_fn).fetch("AI"))[0]
        assert set(draft.value) == {"ticker", "article_count", "headline_count"}
        assert "score" not in draft.value
        assert "sentiment" not in draft.value

    async def test_a_perception_draft_carries_no_unit(self):
        fetch_fn = _router(
            {
                "cnbc_top": CNBC_TOP_AI,
                "cnbc_markets": EMPTY_RSS,
                "marketwatch": MW_AI,
                "seeking_alpha": EMPTY_RSS,
            }
        )
        draft = (await EntityNewsAdapter(fetch_fn=fetch_fn).fetch("AI"))[0]
        assert draft.unit is None
        assert draft.confidence == 1.0
