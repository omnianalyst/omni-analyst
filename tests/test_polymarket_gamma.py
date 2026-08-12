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
            with pytest.raises(Unavailable, match="not a Yes/No binary"):
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


class TestNegRiskDecomposition:
    """Multi-outcome markets flagged `negRisk: true` decompose into N binary
    markets, one per outcome. Each sub-market asks "Will <name> win: <parent>?"
    and resolves YES for the winner, NO for everyone else."""

    def _negrisk_payload(self, **overrides):
        base = {
            "id": "0xnr",
            "question": "Who will win the 2028 nomination?",
            "category": "Politics",
            "outcomes": json.dumps(["Trump", "DeSantis", "Harris"]),
            "outcomePrices": json.dumps(["1.0", "0.0", "0.0"]),
            "clobTokenIds": json.dumps(["tok-t", "tok-d", "tok-h"]),
            "startDate": "2024-01-01T00:00:00Z",
            "endDate": "2028-11-01T00:00:00Z",
            "negRisk": True,
            "slug": "2028-nomination",
            "volume": "500000.0",
        }
        base.update(overrides)
        return base

    async def test_clean_negrisk_decomposes_to_n_markets(self):
        async with _client(lambda r: httpx.Response(200, json=[self._negrisk_payload()])) as c:
            markets = await list_resolved_markets(c, limit=10)
        assert len(markets) == 3
        questions = [m.question for m in markets]
        assert all("Will" in q and "win" in q for q in questions)
        assert any("Trump" in q for q in questions)
        assert any("DeSantis" in q for q in questions)
        assert any("Harris" in q for q in questions)

    async def test_winner_marked_resolved_yes(self):
        async with _client(lambda r: httpx.Response(200, json=[self._negrisk_payload()])) as c:
            markets = await list_resolved_markets(c, limit=10)
        # Trump won (price=1.0)
        winner = next(m for m in markets if "Trump" in m.question)
        loser = next(m for m in markets if "DeSantis" in m.question)
        assert winner.resolved_yes is True
        assert loser.resolved_yes is False

    async def test_unique_condition_ids_per_submarket(self):
        async with _client(lambda r: httpx.Response(200, json=[self._negrisk_payload()])) as c:
            markets = await list_resolved_markets(c, limit=10)
        ids = [m.condition_id for m in markets]
        assert len(set(ids)) == 3
        assert all(cid.startswith("0xnr:") for cid in ids)

    async def test_per_outcome_token_assigned(self):
        async with _client(lambda r: httpx.Response(200, json=[self._negrisk_payload()])) as c:
            markets = await list_resolved_markets(c, limit=10)
        trump = next(m for m in markets if "Trump" in m.question)
        harris = next(m for m in markets if "Harris" in m.question)
        assert trump.yes_token_id == "tok-t"
        assert harris.yes_token_id == "tok-h"
        assert trump.no_token_id is None  # NegRisk sub-markets have no separate NO token

    async def test_negrisk_no_winner_refused(self):
        # All outcomes at 0 — market unresolved or cancelled.
        payload = self._negrisk_payload(outcomePrices=json.dumps(["0.0", "0.0", "0.0"]))
        async with _client(lambda r: httpx.Response(200, json=[payload])) as c:
            with pytest.raises(Unavailable, match="no outcome resolved"):
                await list_resolved_markets(c, limit=10)

    async def test_negrisk_multiple_winners_refused(self):
        payload = self._negrisk_payload(outcomePrices=json.dumps(["1.0", "1.0", "0.0"]))
        async with _client(lambda r: httpx.Response(200, json=[payload])) as c:
            with pytest.raises(Unavailable, match="ambiguous"):
                await list_resolved_markets(c, limit=10)

    async def test_non_negrisk_multi_outcome_refused(self):
        # Multi-outcome but no negRisk flag — refuse rather than guess semantics.
        payload = self._negrisk_payload(negRisk=False)
        async with _client(lambda r: httpx.Response(200, json=[payload])) as c:
            with pytest.raises(Unavailable, match="not a supported NegRisk"):
                await list_resolved_markets(c, limit=10)

    async def test_negrisk_lenient_mode_skips_unresolved_events(self):
        # Unresolved NegRisk event (no winner) in lenient mode: skipped, not fatal.
        bad = self._negrisk_payload(outcomePrices=json.dumps(["0.5", "0.3", "0.2"]))
        good = self._negrisk_payload(id="0xnr2")
        skipped = []
        async with _client(lambda r: httpx.Response(200, json=[good, bad])) as c:
            markets = await list_resolved_markets(
                c, limit=10, strict=False,
                on_skip=lambda raw, exc: skipped.append((raw.get("id"), str(exc))),
            )
        assert len(markets) == 3
        assert len(skipped) == 1
        assert skipped[0][0] == "0xnr"


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
