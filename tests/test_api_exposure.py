"""Tests for POST /exposure/overlap.

The endpoint takes a portfolio composition in the body (which ETFs at what
allocations), reads holdings from the store, and returns the overlap and
concentration analysis. These tests verify both the happy path (verifiable
arithmetic against small seeded data) and the failure paths (missing entity,
empty body, negative allocation, audience scoping).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from neutron.test import TestClient

from omni.api.exposure import build_router
from omni.main import create_app

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _make_app(database_url):
    app = create_app(database_url)
    app.include_router(build_router(app))
    return app


class _Lifespan:
    def __init__(self, app):
        self._app = app
        self._receive = asyncio.Queue()
        self._send = asyncio.Queue()
        self._task = None

    async def __aenter__(self):
        self._task = asyncio.create_task(
            self._app({"type": "lifespan"}, self._receive.get, self._send.put)
        )
        await self._receive.put({"type": "lifespan.startup"})
        message = await self._send.get()
        assert message["type"] == "lifespan.startup.complete", message
        return self._app

    async def __aexit__(self, *exc):
        await self._receive.put({"type": "lifespan.shutdown"})
        await self._send.get()
        await self._task


def _auth(user_id):
    os.environ.setdefault("OMNI_JWT_SECRET", "t" * 48)
    from neutron.auth.jwt import create_token

    token = create_token({"sub": str(user_id)}, os.environ["OMNI_JWT_SECRET"])
    return {"Authorization": f"Bearer {token}"}


async def _etf(db, symbol, name, bucket):
    identifiers = json.dumps({"polygon": symbol, "bucket": bucket})
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name, identifiers) "
        "VALUES ('etf', $1, $2, $3::jsonb) RETURNING id",
        symbol, name, identifiers,
    )


async def _holding(db, entity_id, ticker, weight, *, audience=None):
    redistributable = "allowed" if audience is None else "byo_only"
    value = json.dumps({"weight": str(weight)})
    await db.pool.execute(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1, 'holding', $2, $3::jsonb, 'test', $4, $4, 1.0, $5, $6)
        """,
        entity_id, ticker, value, NOW, redistributable, audience,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestOverlapEndpoint:
    async def test_returns_concentration_and_overlap(self, db, database_url):
        vti = await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")
        qqq = await _etf(db, "QQQ", "Invesco QQQ Trust", "growth")
        await _holding(db, vti, "AAPL", Decimal("0.06"))
        await _holding(db, vti, "MSFT", Decimal("0.05"))
        await _holding(db, qqq, "AAPL", Decimal("0.12"))
        await _holding(db, qqq, "NVDA", Decimal("0.04"))

        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post("/exposure/overlap", json={
                "positions": [
                    {"symbol": "VTI", "allocation": "0.60"},
                    {"symbol": "QQQ", "allocation": "0.20"},
                ],
                "overlap_threshold": "0.01",
                "concentration_threshold": "0.01",
            })

        assert r.status_code == 200, r.text
        body = r.json()

        aapl = next(c for c in body["concentration"] if c["ticker"] == "AAPL")
        # 0.06 * 0.60 + 0.12 * 0.20 = 0.036 + 0.024 = 0.060
        assert Decimal(aapl["total_weight"]) == Decimal("0.060")
        assert set(aapl["source_etfs"]) == {"VTI", "QQQ"}

        assert len(body["overlaps"]) == 1
        overlap = body["overlaps"][0]
        assert overlap["etf_a"] == "VTI"
        assert overlap["etf_b"] == "QQQ"
        # min(0.06, 0.12) = 0.06
        assert Decimal(overlap["shared_weight"]) == Decimal("0.06")

        bucket = {
            b["bucket"]: Decimal(b["allocation"])
            for b in body["bucket_exposure"]
        }
        assert bucket["growth"] == Decimal("0.80")

    async def test_bucket_exposure_aggregates_allocation(self, db, database_url):
        await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")
        await _etf(db, "TLT", "iShares 20+ Year Treasury", "deflation_rally")
        await _etf(db, "GLD", "SPDR Gold Shares", "currency_debasement")

        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post("/exposure/overlap", json={
                "positions": [
                    {"symbol": "VTI", "allocation": "0.50"},
                    {"symbol": "TLT", "allocation": "0.30"},
                    {"symbol": "GLD", "allocation": "0.20"},
                ]
            })

        assert r.status_code == 200
        bucket = {
            b["bucket"]: Decimal(b["allocation"])
            for b in r.json()["bucket_exposure"]
        }
        assert bucket == {
            "growth": Decimal("0.50"),
            "deflation_rally": Decimal("0.30"),
            "currency_debasement": Decimal("0.20"),
        }

    async def test_empty_positions_is_400(self, db, database_url):
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post("/exposure/overlap", json={"positions": []})

        assert r.status_code == 400
        assert r.headers["content-type"].startswith("application/problem+json")

    async def test_missing_entity_is_400(self, db, database_url):
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post("/exposure/overlap", json={
                "positions": [{"symbol": "NONEXIST", "allocation": "1.0"}]
            })

        assert r.status_code == 400
        body = r.json()
        assert "NONEXIST" in body["detail"]

    async def test_negative_allocation_is_400(self, db, database_url):
        await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")

        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post("/exposure/overlap", json={
                "positions": [{"symbol": "VTI", "allocation": "-0.5"}]
            })

        assert r.status_code == 400
        assert "negative" in r.json()["detail"].lower()

    async def test_non_numeric_allocation_is_400(self, db, database_url):
        await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")

        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post("/exposure/overlap", json={
                "positions": [{"symbol": "VTI", "allocation": "abc"}]
            })

        assert r.status_code == 400

    async def test_byo_holdings_scoped_to_owner(self, db, database_url):
        from uuid import uuid4

        vti = await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")
        owner = uuid4()
        other = uuid4()
        await _holding(db, vti, "AAPL", Decimal("0.06"), audience=owner)
        await _holding(db, vti, "MSFT", Decimal("0.05"))

        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r_anon = await client.post("/exposure/overlap", json={
                "positions": [{"symbol": "VTI", "allocation": "1.0"}]
            })
            r_owner = await client.post(
                "/exposure/overlap",
                json={"positions": [{"symbol": "VTI", "allocation": "1.0"}]},
                headers=_auth(owner),
            )
            r_other = await client.post(
                "/exposure/overlap",
                json={"positions": [{"symbol": "VTI", "allocation": "1.0"}]},
                headers=_auth(other),
            )

        assert r_anon.status_code == 200
        assert r_owner.status_code == 200
        assert r_other.status_code == 200

        anon_tickers = {h["ticker"] for h in r_anon.json()["top_holdings"]}
        owner_tickers = {h["ticker"] for h in r_owner.json()["top_holdings"]}
        other_tickers = {h["ticker"] for h in r_other.json()["top_holdings"]}

        assert anon_tickers == {"MSFT"}
        assert owner_tickers == {"AAPL", "MSFT"}
        assert other_tickers == {"MSFT"}

    async def test_fund_with_no_holdings_returns_empty_lists(self, db, database_url):
        await _etf(db, "TLT", "iShares 20+ Year Treasury", "deflation_rally")

        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post("/exposure/overlap", json={
                "positions": [{"symbol": "TLT", "allocation": "1.0"}]
            })

        assert r.status_code == 200
        body = r.json()
        assert body["concentration"] == []
        assert body["overlaps"] == []
        assert body["top_holdings"] == []
