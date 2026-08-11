import json
from datetime import UTC, datetime

import httpx
import pytest

from omni.ingest.protocol import Unavailable
from omni.polymarket.gamma import (
    fetch_price_history,
    list_resolved_markets,
    list_resolved_markets_until,
)


def _client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def _market_payload(**overrides):
    base = {
        "id": "0x1",
        "question": "Will X happen?",
        "category": "Politics",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["1.00", "0.00"]),
        "clobTokenIds": json.dumps(["tok-y", "tok-n"]),
        "startDate": "2024-05-01T00:00:00Z",
        "endDate": "2024-06-01T00:00:00Z",
        "negRisk": False,
        "slug": "will-x-happen",
        "volume": "12345.0",
    }
    base.update(overrides)
    return base


class TestListResolvedMarkets:
    async def test_clean_response_parsed(self):
        async with _client(lambda req: httpx.Response(200, json=[_market_payload()])) as c:
            markets = await list_resolved_markets(c, limit=10)
        assert len(markets) == 1
        m = markets[0]
        assert m.condition_id == "0x1"
        assert m.resolved_yes is True
        assert m.yes_token_id == "tok-y"
        assert m.no_token_id == "tok-n"
        assert m.resolution_date == datetime(2024, 6, 1, tzinfo=UTC)
        assert m.created_at == datetime(2024, 5, 1, tzinfo=UTC)
        assert m.volume == 12345.0

    async def test_resolved_no_parsed(self):
        payload = _market_payload(outcomePrices=json.dumps(["0.00", "1.00"]))
        async with _client(lambda req: httpx.Response(200, json=[payload])) as c:
            markets = await list_resolved_markets(c, limit=10)
        assert markets[0].resolved_yes is False

    async def test_ambiguous_resolution_refused_per_market(self):
        payload = _market_payload(outcomePrices=json.dumps(["1.00", "1.00"]))
        async with _client(lambda req: httpx.Response(200, json=[payload])) as c:
            with pytest.raises(Unavailable, match="ambiguous"):
                await list_resolved_markets(c, limit=10)

    async def test_wrong_yes_label_refused(self):
        payload = _market_payload(outcomes=json.dumps(["Up", "Down"]))
        async with _client(lambda req: httpx.Response(200, json=[payload])) as c:
            with pytest.raises(Unavailable, match="outcome\\[0\\]"):
                await list_resolved_markets(c, limit=10)

    async def test_http_error_raises_unavailable(self):
        async with _client(lambda req: httpx.Response(500)) as c:
            with pytest.raises(Unavailable, match="Gamma"):
                await list_resolved_markets(c, limit=10)

    async def test_non_list_payload_raises(self):
        async with _client(lambda req: httpx.Response(200, json={"oops": True})) as c:
            with pytest.raises(Unavailable, match="expected a list"):
                await list_resolved_markets(c, limit=10)

    async def test_category_filter(self):
        payload = [
            _market_payload(category="Politics"),
            _market_payload(id="0x2", category="Sports"),
        ]
        async with _client(lambda req: httpx.Response(200, json=payload)) as c:
            markets = await list_resolved_markets(c, limit=10, categories=["politics"])
        assert len(markets) == 1
        assert markets[0].category == "Politics"

    async def test_min_volume_filter(self):
        payload = [
            _market_payload(volume="1000"),
            _market_payload(id="0x2", volume="100"),
        ]
        async with _client(lambda req: httpx.Response(200, json=payload)) as c:
            markets = await list_resolved_markets(c, limit=10, min_volume=500)
        assert len(markets) == 1
        assert markets[0].volume == 1000

    async def test_invalid_limit_refused(self):
        async with _client(lambda req: httpx.Response(200, json=[])) as c:
            with pytest.raises(ValueError):
                await list_resolved_markets(c, limit=0)
            with pytest.raises(ValueError):
                await list_resolved_markets(c, limit=1000)

    async def test_strict_mode_raises_on_first_bad_market(self):
        payload = [
            _market_payload(),
            _market_payload(id="0x2", outcomePrices=json.dumps(["1.00", "1.00"])),
        ]
        async with _client(lambda req: httpx.Response(200, json=payload)) as c:
            with pytest.raises(Unavailable, match="ambiguous"):
                await list_resolved_markets(c, limit=10)

    async def test_lenient_mode_skips_bad_markets_and_keeps_good(self):
        skipped: list[str] = []
        good = _market_payload()
        bad = _market_payload(id="0x2", outcomePrices=json.dumps(["1.00", "1.00"]))
        also_bad = _market_payload(id="0x3", outcomes=json.dumps(["Over", "Under"]))

        def on_skip(raw, exc):
            skipped.append(f"{raw.get('id')}: {exc}")

        async with _client(lambda req: httpx.Response(200, json=[good, bad, also_bad])) as c:
            markets = await list_resolved_markets(c, limit=10, strict=False, on_skip=on_skip)

        assert len(markets) == 1
        assert markets[0].condition_id == "0x1"
        assert len(skipped) == 2
        assert any("0x2" in s and "ambiguous" in s for s in skipped)
        assert any("0x3" in s and "Over" in s for s in skipped)

    async def test_lenient_mode_still_raises_on_http_error(self):
        async with _client(lambda req: httpx.Response(500)) as c:
            with pytest.raises(Unavailable, match="Gamma"):
                await list_resolved_markets(c, limit=10, strict=False)

    async def test_lenient_mode_still_raises_on_non_list_payload(self):
        async with _client(lambda req: httpx.Response(200, json={"oops": True})) as c:
            with pytest.raises(Unavailable, match="expected a list"):
                await list_resolved_markets(c, limit=10, strict=False)


class TestListResolvedMarketsUntil:
    """Multi-page pagination: stops at target, empty page, or max_pages."""

    def _pages_handler(self, pages: dict[int, list]):
        """Returns a handler that serves different payloads per offset."""
        def handler(req: httpx.Request):
            offset = int(req.url.params.get("offset", "0"))
            page_index = offset // 100
            payload = pages.get(page_index, [])
            return httpx.Response(200, json=payload)
        return handler

    async def test_stops_when_target_reached(self):
        pages = {
            0: [_market_payload(id=f"0x{i}") for i in range(100)],
            1: [_market_payload(id=f"1x{i}") for i in range(100)],
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(self._pages_handler(pages))) as c:
            markets = await list_resolved_markets_until(c, target_count=150)
        assert len(markets) == 150
        assert markets[0].condition_id == "0x0"
        assert markets[149].condition_id == "1x49"

    async def test_stops_on_empty_page(self):
        pages = {0: [_market_payload(id=f"0x{i}") for i in range(50)]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(self._pages_handler(pages))) as c:
            markets = await list_resolved_markets_until(c, target_count=200)
        assert len(markets) == 50

    async def test_stops_at_max_pages(self):
        pages = {i: [_market_payload(id=f"p{i}x{j}") for j in range(100)] for i in range(20)}
        async with httpx.AsyncClient(transport=httpx.MockTransport(self._pages_handler(pages))) as c:
            markets = await list_resolved_markets_until(c, target_count=10000, max_pages=3)
        assert len(markets) == 300

    async def test_on_page_callback_invoked(self):
        pages = {0: [_market_payload(id=f"0x{i}") for i in range(100)]}
        calls = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(self._pages_handler(pages))) as c:
            await list_resolved_markets_until(
                c, target_count=50,
                on_page=lambda idx, n: calls.append((idx, n)),
            )
        assert calls == [(0, 100)]

    async def test_invalid_args_refused(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))) as c:
            with pytest.raises(ValueError):
                await list_resolved_markets_until(c, target_count=0)
            with pytest.raises(ValueError):
                await list_resolved_markets_until(c, target_count=10, page_size=0)
            with pytest.raises(ValueError):
                await list_resolved_markets_until(c, target_count=10, max_pages=0)


class TestFetchPriceHistory:
    async def test_clean_history_parsed(self):
        body = {"history": [{"t": 1716336000, "p": 0.65}, {"t": 1716422400, "p": 0.70}]}
        async with _client(lambda req: httpx.Response(200, json=body)) as c:
            points = await fetch_price_history(
                c,
                token_id="tok-y",
                start=datetime(2024, 5, 1, tzinfo=UTC),
                end=datetime(2024, 6, 1, tzinfo=UTC),
            )
        assert len(points) == 2
        assert points[0].yes_price == 0.65
        assert points[1].yes_price == 0.70
        assert points[0].at < points[1].at

    async def test_empty_history_returns_empty(self):
        async with _client(lambda req: httpx.Response(200, json={"history": []})) as c:
            points = await fetch_price_history(
                c,
                token_id="tok-y",
                start=datetime(2024, 5, 1, tzinfo=UTC),
                end=datetime(2024, 6, 1, tzinfo=UTC),
            )
        assert points == []

    async def test_missing_history_key_returns_empty(self):
        async with _client(lambda req: httpx.Response(200, json={})) as c:
            points = await fetch_price_history(
                c,
                token_id="tok-y",
                start=datetime(2024, 5, 1, tzinfo=UTC),
                end=datetime(2024, 6, 1, tzinfo=UTC),
            )
        assert points == []

    async def test_history_dict_shape_refused(self):
        body = {"history": {"1716336000": "0.65"}}
        async with _client(lambda req: httpx.Response(200, json=body)) as c:
            with pytest.raises(Unavailable, match="expected a list"):
                await fetch_price_history(
                    c,
                    token_id="tok-y",
                    start=datetime(2024, 5, 1, tzinfo=UTC),
                    end=datetime(2024, 6, 1, tzinfo=UTC),
                )

    async def test_non_numeric_price_refused(self):
        body = {"history": [{"t": 1716336000, "p": "not-a-number"}]}
        async with _client(lambda req: httpx.Response(200, json=body)) as c:
            with pytest.raises(Unavailable, match="parseable"):
                await fetch_price_history(
                    c,
                    token_id="tok-y",
                    start=datetime(2024, 5, 1, tzinfo=UTC),
                    end=datetime(2024, 6, 1, tzinfo=UTC),
                )

    async def test_missing_keys_refused(self):
        body = {"history": [{"timestamp": 1716336000, "price": 0.5}]}
        async with _client(lambda req: httpx.Response(200, json=body)) as c:
            with pytest.raises(Unavailable, match="parseable"):
                await fetch_price_history(
                    c,
                    token_id="tok-y",
                    start=datetime(2024, 5, 1, tzinfo=UTC),
                    end=datetime(2024, 6, 1, tzinfo=UTC),
                )

    async def test_http_error_raises_unavailable(self):
        async with _client(lambda req: httpx.Response(503)) as c:
            with pytest.raises(Unavailable, match="CLOB"):
                await fetch_price_history(
                    c,
                    token_id="tok-y",
                    start=datetime(2024, 5, 1, tzinfo=UTC),
                    end=datetime(2024, 6, 1, tzinfo=UTC),
                )

    async def test_naive_timestamp_refused(self):
        async with _client(lambda req: httpx.Response(200, json={"history": []})) as c:
            with pytest.raises(ValueError):
                await fetch_price_history(
                    c,
                    token_id="tok-y",
                    start=datetime(2024, 5, 1),  # noqa: DTZ001
                    end=datetime(2024, 6, 1),  # noqa: DTZ001
                )
