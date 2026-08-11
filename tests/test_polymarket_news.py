"""Tests for the news providers: parsing shape, cutoff enforcement, composure.

MockTransport throughout — no network. Each test pins one behaviour of the
provider or its helpers, including the failure paths (API outage, malformed
payload, post-cutoff articles).
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from omni.polymarket.news import (
    GDELT_BASE_URL,
    JINA_READER_BASE_URL,
    _extract_keywords,
    article_body_provider,
    compose_providers,
    derive_category,
    gamma_description_provider,
    gdelt_news_provider,
)
from omni.polymarket.types import Document, ResolvedMarket

UTC_ = UTC


def _market(question: str = "Will Bitcoin close above $100,000 on Dec 31?") -> ResolvedMarket:
    return ResolvedMarket(
        condition_id="0x1",
        question=question,
        category="Crypto",
        resolved_yes=True,
        resolution_date=datetime(2024, 12, 31, tzinfo=UTC_),
        created_at=datetime(2024, 12, 1, tzinfo=UTC_),
        yes_token_id="tok-y",
    )


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestExtractKeywords:
    def test_strips_stopwords(self):
        kw = _extract_keywords("Will the price go above $100,000 by year end?")
        assert "will" not in kw.lower().split()
        assert "above" not in kw.lower().split()

    def test_keeps_proper_nouns(self):
        kw = _extract_keywords("Will Bitcoin close above $100,000 on Dec 31?")
        tokens = kw.split()
        # Numbers are stripped by the word-only regex; proper nouns survive.
        assert "Bitcoin" in tokens
        assert "Dec" in tokens

    def test_empty_question_returns_empty(self):
        assert _extract_keywords("") == ""

    def test_only_stopwords_returns_empty(self):
        assert _extract_keywords("Will the be above or below?") == ""


class TestDeriveCategory:
    @pytest.mark.parametrize("question,expected", [
        ("Will Bitcoin close above $100k?", "Crypto"),
        ("Ethereum above 2,275 on April 21?", "Crypto"),
        ("Will Trump win the 2024 election?", "Politics"),
        ("Will the Fed cut rates in September?", "Economics"),
        ("CPI comes in above 3.0% for June?", "Economics"),
        ("Russia invades Ukraine by year end?", "Geopolitics"),
        ("Lakers vs Celtics — who wins?", "Sports"),
        ("Will it rain in London tomorrow?", "Other"),
    ])
    def test_classification(self, question, expected):
        m = _market(question=question)
        assert derive_category(m) == expected

    def test_unknown_defaults_to_other(self):
        m = _market(question="Some completely unrelated question about art?")
        assert derive_category(m) == "Other"

    def test_case_insensitive(self):
        m = _market(question="will BITCOIN reach $200k?")
        assert derive_category(m) == "Crypto"


class TestGammaDescriptionProvider:
    async def test_clean_description_parsed(self):
        def handler(req):
            return httpx.Response(200, json={
                "description": "This market resolves YES if BTC >= 100k.",
                "groupItemTitle": "Crypto milestones 2024",
            })
        async with _client(handler) as c:
            docs = await gamma_description_provider(c, _market(), datetime(2024, 12, 15, tzinfo=UTC_))
        assert len(docs) == 1
        assert "Crypto milestones" in docs[0].text
        assert "BTC >= 100k" in docs[0].text
        assert docs[0].at < datetime(2024, 12, 15, tzinfo=UTC_)

    async def test_empty_description_returns_empty(self):
        async with _client(lambda r: httpx.Response(200, json={})) as c:
            docs = await gamma_description_provider(c, _market(), datetime(2024, 12, 15, tzinfo=UTC_))
        assert docs == ()

    async def test_http_failure_returns_empty(self):
        async with _client(lambda r: httpx.Response(500)) as c:
            docs = await gamma_description_provider(c, _market(), datetime(2024, 12, 15, tzinfo=UTC_))
        assert docs == ()

    async def test_description_truncated_to_2000_chars(self):
        long_desc = "x" * 5000
        def handler(req):
            return httpx.Response(200, json={"description": long_desc})
        async with _client(handler) as c:
            docs = await gamma_description_provider(c, _market(), datetime(2024, 12, 15, tzinfo=UTC_))
        assert len(docs[0].text) == 2000


class TestGdeltNewsProvider:
    async def test_clean_response_parsed(self):
        body = {
            "articles": [
                {
                    "title": "Bitcoin surges past $99k in late December rally",
                    "seendate": "20241210T120000Z",
                    "domain": "coindesk.com",
                },
                {
                    "title": "Analysts divided on whether BTC will hit $100k",
                    "seendate": "20241212T080000Z",
                    "domain": "bloomberg.com",
                },
            ]
        }
        async with _client(lambda r: httpx.Response(200, json=body)) as c:
            docs = await gdelt_news_provider(
                c, _market(), datetime(2024, 12, 15, tzinfo=UTC_),
            )
        assert len(docs) == 2
        assert "Bitcoin" in docs[0].text or "BTC" in docs[0].text
        assert docs[0].source.startswith("gdelt:")
        assert docs[0].at < datetime(2024, 12, 15, tzinfo=UTC_)

    async def test_post_cutoff_articles_excluded(self):
        cutoff = datetime(2024, 12, 15, tzinfo=UTC_)
        body = {
            "articles": [
                {"title": "pre-cutoff", "seendate": "20241210T120000Z", "domain": "x.com"},
                {"title": "post-cutoff", "seendate": "20241216T120000Z", "domain": "y.com"},
                {"title": "exactly-cutoff", "seendate": "20241215T120000Z", "domain": "z.com"},
            ]
        }
        async with _client(lambda r: httpx.Response(200, json=body)) as c:
            docs = await gdelt_news_provider(c, _market(), cutoff)
        titles = [d.text for d in docs]
        assert "pre-cutoff" in titles
        assert "post-cutoff" not in titles
        assert "exactly-cutoff" not in titles

    async def test_malformed_articles_skipped(self):
        body = {
            "articles": [
                {"title": "ok", "seendate": "20241210T120000Z", "domain": "x.com"},
                {"title": None, "seendate": "20241210T120000Z", "domain": "y.com"},
                "not-a-dict",
                {"title": "bad-date", "seendate": "garbage", "domain": "z.com"},
            ]
        }
        async with _client(lambda r: httpx.Response(200, json=body)) as c:
            docs = await gdelt_news_provider(c, _market(), datetime(2024, 12, 15, tzinfo=UTC_))
        assert len(docs) == 1
        assert docs[0].text == "ok"

    async def test_max_articles_enforced(self):
        body = {
            "articles": [
                {"title": f"article-{i}", "seendate": "20241210T120000Z", "domain": "x.com"}
                for i in range(10)
            ]
        }
        async with _client(lambda r: httpx.Response(200, json=body)) as c:
            docs = await gdelt_news_provider(
                c, _market(), datetime(2024, 12, 15, tzinfo=UTC_), max_articles=3,
            )
        assert len(docs) == 3

    async def test_no_keywords_returns_empty(self):
        market = _market(question="Will the thing be above or below?")
        async with _client(lambda r: httpx.Response(200, json={"articles": []})) as c:
            docs = await gdelt_news_provider(c, market, datetime(2024, 12, 15, tzinfo=UTC_))
        assert docs == ()

    async def test_http_error_returns_empty(self):
        async with _client(lambda r: httpx.Response(503)) as c:
            docs = await gdelt_news_provider(c, _market(), datetime(2024, 12, 15, tzinfo=UTC_))
        assert docs == ()

    async def test_non_json_response_returns_empty(self):
        async with _client(lambda r: httpx.Response(200, content=b"not json")) as c:
            docs = await gdelt_news_provider(c, _market(), datetime(2024, 12, 15, tzinfo=UTC_))
        assert docs == ()

    async def test_request_uses_gdelt_endpoint(self):
        captured = {}
        def handler(req: httpx.Request):
            captured["url"] = str(req.url)
            return httpx.Response(200, json={"articles": []})
        async with _client(handler) as c:
            await gdelt_news_provider(c, _market(), datetime(2024, 12, 15, tzinfo=UTC_))
        assert GDELT_BASE_URL in captured["url"]
        assert "maxrecords" in captured["url"]


class TestArticleBodyProvider:
    """End-to-end via mock GDELT + mock Jina Reader. The body provider queries
    GDELT first (same path as the title provider), then fetches each article's
    body via Jina."""

    def _gdelt_handler(self, articles):
        return lambda req: httpx.Response(200, json={"articles": articles})

    def _jina_handler(self, body_by_url):
        def handler(req: httpx.Request):
            for url, body in body_by_url.items():
                if url in str(req.url):
                    return httpx.Response(200, text=body)
            return httpx.Response(404)
        return handler

    def _combined_handler(self, gdelt_articles, body_by_url):
        """One handler that routes by URL: GDELT for the article list, Jina
        for individual article fetches. Avoids needing two separate clients."""
        def handler(req: httpx.Request):
            url = str(req.url)
            if GDELT_BASE_URL in url:
                return httpx.Response(200, json={"articles": gdelt_articles})
            if JINA_READER_BASE_URL in url:
                for article_url, body in body_by_url.items():
                    if article_url in url:
                        return httpx.Response(200, text=body)
                return httpx.Response(404)
            return httpx.Response(404)
        return handler

    async def test_clean_body_fetched(self):
        gdelt_articles = [{
            "title": "Bitcoin surges",
            "url": "https://example.com/btc-surges",
            "seendate": "20241210T120000Z",
            "domain": "example.com",
        }]
        body_by_url = {"https://example.com/btc-surges": "Full article body text."}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            self._combined_handler(gdelt_articles, body_by_url)
        )) as c:
            docs = await article_body_provider(
                c, _market(), datetime(2024, 12, 15, tzinfo=UTC_),
            )
        assert len(docs) == 1
        assert "Full article body" in docs[0].text
        assert docs[0].source.startswith("jina:")

    async def test_truncates_to_max_chars(self):
        gdelt_articles = [{
            "title": "Long article",
            "url": "https://example.com/long",
            "seendate": "20241210T120000Z",
            "domain": "example.com",
        }]
        body_by_url = {"https://example.com/long": "x" * 5000}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            self._combined_handler(gdelt_articles, body_by_url)
        )) as c:
            docs = await article_body_provider(
                c, _market(), datetime(2024, 12, 15, tzinfo=UTC_),
                max_chars_per_article=800,
            )
        assert len(docs[0].text) == 800

    async def test_jina_404_skipped(self):
        gdelt_articles = [
            {"title": "ok", "url": "https://ok.com/x", "seendate": "20241210T120000Z", "domain": "ok.com"},
            {"title": "missing", "url": "https://missing.com/x", "seendate": "20241210T120000Z", "domain": "missing.com"},
        ]
        body_by_url = {"https://ok.com/x": "ok body"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            self._combined_handler(gdelt_articles, body_by_url)
        )) as c:
            docs = await article_body_provider(
                c, _market(), datetime(2024, 12, 15, tzinfo=UTC_),
            )
        assert len(docs) == 1
        assert docs[0].text == "ok body"

    async def test_no_gdelt_articles_returns_empty(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            self._combined_handler([], {})
        )) as c:
            docs = await article_body_provider(
                c, _market(), datetime(2024, 12, 15, tzinfo=UTC_),
            )
        assert docs == ()

    async def test_max_articles_enforced(self):
        gdelt_articles = [
            {"title": f"a{i}", "url": f"https://x.com/{i}", "seendate": "20241210T120000Z", "domain": "x.com"}
            for i in range(5)
        ]
        body_by_url = {f"https://x.com/{i}": f"body-{i}" for i in range(5)}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            self._combined_handler(gdelt_articles, body_by_url)
        )) as c:
            docs = await article_body_provider(
                c, _market(), datetime(2024, 12, 15, tzinfo=UTC_),
                max_articles=2,
            )
        assert len(docs) == 2


class TestComposeProviders:
    async def test_concatenates_documents_in_order(self):
        async def p1(client, market, cutoff):
            return (Document(at=market.created_at, source="p1", text="first"),)
        async def p2(client, market, cutoff):
            return (
                Document(at=market.created_at + timedelta(days=1), source="p2", text="second"),
                Document(at=market.created_at + timedelta(days=2), source="p2b", text="third"),
            )
        composed = compose_providers(p1, p2)
        async with _client(lambda r: httpx.Response(200)) as c:
            docs = await composed(c, _market(), datetime(2024, 12, 15, tzinfo=UTC_))
        assert [d.text for d in docs] == ["first", "second", "third"]

    async def test_unavailable_provider_skipped(self):
        from omni.ingest.protocol import Unavailable
        async def failing(client, market, cutoff):
            raise Unavailable("down")
        async def ok(client, market, cutoff):
            return (Document(at=market.created_at, source="ok", text="good"),)
        composed = compose_providers(failing, ok)
        async with _client(lambda r: httpx.Response(200)) as c:
            docs = await composed(c, _market(), datetime(2024, 12, 15, tzinfo=UTC_))
        assert len(docs) == 1
        assert docs[0].text == "good"

    async def test_empty_compose_returns_empty(self):
        composed = compose_providers()
        async with _client(lambda r: httpx.Response(200)) as c:
            docs = await composed(c, _market(), datetime(2024, 12, 15, tzinfo=UTC_))
        assert docs == ()
