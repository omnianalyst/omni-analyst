"""Tests for the exposure DB query layer.

These create ETF entities, write holding claims, and verify that
``load_positions`` resolves them into the ``ETFPosition`` structures
``overlap.analyze`` expects. Every assertion checks the specific weight and
ticker values an operator would compute by hand.

The audience test (BYO vs shared) mirrors the rule every claim-read path is
tested against: a private claim fetched under one user's credential must never
appear in another user's exposure view.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from omni.exposure.query import CompositionEntry, CompositionError, load_positions

NOW = datetime(2026, 8, 1, tzinfo=UTC)


async def _etf(db, symbol, name, bucket):
    identifiers = json.dumps({"polygon": symbol, "bucket": bucket})
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name, identifiers) "
        "VALUES ('etf', $1, $2, $3::jsonb) RETURNING id",
        symbol, name, identifiers,
    )


async def _holding(
    db,
    entity_id,
    ticker,
    weight,
    *,
    source="etf_holdings",
    event_date=NOW,
    knowledge_date=None,
    audience=None,
):
    kd = knowledge_date or event_date
    redistributable = "allowed" if audience is None else "byo_only"
    value = json.dumps({"weight": str(weight), "fund": "TEST"})
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1, 'holding', $2, $3::jsonb, $4, $5, $6, 1.0, $7, $8)
        RETURNING id
        """,
        entity_id, ticker, value, source, event_date, kd,
        redistributable, audience,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestLoadPositions:
    async def test_resolves_holdings_into_etf_positions(self, db):
        vti = await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")
        qqq = await _etf(db, "QQQ", "Invesco QQQ Trust", "growth")
        await _holding(db, vti, "AAPL", Decimal("0.06"))
        await _holding(db, vti, "MSFT", Decimal("0.05"))
        await _holding(db, qqq, "AAPL", Decimal("0.12"))
        await _holding(db, qqq, "NVDA", Decimal("0.04"))

        positions = await load_positions(
            db.pool,
            composition=[
                CompositionEntry("VTI", Decimal("0.60")),
                CompositionEntry("QQQ", Decimal("0.20")),
            ],
        )

        assert len(positions) == 2
        vti_pos = next(p for p in positions if p.symbol == "VTI")
        assert vti_pos.bucket == "growth"
        assert vti_pos.allocation == Decimal("0.60")
        vti_holdings = {h.ticker: h.weight for h in vti_pos.holdings}
        assert vti_holdings == {"AAPL": Decimal("0.06"), "MSFT": Decimal("0.05")}

        qqq_pos = next(p for p in positions if p.symbol == "QQQ")
        qqq_holdings = {h.ticker: h.weight for h in qqq_pos.holdings}
        assert qqq_holdings == {"AAPL": Decimal("0.12"), "NVDA": Decimal("0.04")}

    async def test_bucket_override(self, db):
        vti = await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")
        await _holding(db, vti, "AAPL", Decimal("0.06"))

        positions = await load_positions(
            db.pool,
            composition=[CompositionEntry("VTI", Decimal("1.0"), bucket="concentration")],
        )
        assert positions[0].bucket == "concentration"

    async def test_fund_with_no_holdings_gets_empty_tuple(self, db):
        await _etf(db, "TLT", "iShares 20+ Year Treasury", "deflation_rally")

        positions = await load_positions(
            db.pool,
            composition=[CompositionEntry("TLT", Decimal("0.30"))],
        )
        assert positions[0].holdings == ()

    async def test_raises_on_missing_entity(self, db):
        with pytest.raises(CompositionError, match="no etf entity.*NONEXIST"):
            await load_positions(
                db.pool,
                composition=[CompositionEntry("NONEXIST", Decimal("1.0"))],
            )

    async def test_empty_composition_returns_empty_list(self, db):
        positions = await load_positions(db.pool, composition=[])
        assert positions == []

    async def test_latest_holdings_win_when_multiple_filings(self, db):
        vti = await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")
        old = NOW - timedelta(days=90)
        await _holding(db, vti, "AAPL", Decimal("0.05"), event_date=old, knowledge_date=old)
        await _holding(db, vti, "AAPL", Decimal("0.07"))

        positions = await load_positions(
            db.pool,
            composition=[CompositionEntry("VTI", Decimal("1.0"))],
        )
        aapl = next(h for h in positions[0].holdings if h.ticker == "AAPL")
        assert aapl.weight == Decimal("0.07")

    async def test_as_of_filter_excludes_future_knowledge(self, db):
        vti = await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")
        future = NOW + timedelta(days=30)
        await _holding(db, vti, "AAPL", Decimal("0.06"), event_date=future, knowledge_date=future)

        positions = await load_positions(
            db.pool,
            composition=[CompositionEntry("VTI", Decimal("1.0"))],
            as_of=NOW,
        )
        assert positions[0].holdings == ()

    async def test_weight_field_named_percentage_of_net_assets(self, db):
        vti = await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")
        value = json.dumps({"percentage_of_net_assets": "0.035"})
        await db.pool.execute(
            """
            INSERT INTO claim (entity_id, claim_type, key, value, source,
                               event_date, knowledge_date, confidence,
                               redistributable)
            VALUES ($1, 'holding', 'GOOGL', $2::jsonb, 'test', $3, $3, 1.0, 'allowed')
            """,
            vti, value, NOW,
        )
        positions = await load_positions(
            db.pool,
            composition=[CompositionEntry("VTI", Decimal("1.0"))],
        )
        googl = next(h for h in positions[0].holdings if h.ticker == "GOOGL")
        assert googl.weight == Decimal("0.035")

    async def test_null_weight_is_skipped(self, db):
        vti = await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")
        value = json.dumps({"weight": None})
        await db.pool.execute(
            """
            INSERT INTO claim (entity_id, claim_type, key, value, source,
                               event_date, knowledge_date, confidence,
                               redistributable)
            VALUES ($1, 'holding', 'BAD', $2::jsonb, 'test', $3, $3, 1.0, 'allowed')
            """,
            vti, value, NOW,
        )
        await _holding(db, vti, "GOOD", Decimal("0.05"))

        positions = await load_positions(
            db.pool,
            composition=[CompositionEntry("VTI", Decimal("1.0"))],
        )
        tickers = {h.ticker for h in positions[0].holdings}
        assert "GOOD" in tickers
        assert "BAD" not in tickers


class TestAudienceScoping:
    async def test_byo_holdings_invisible_to_other_audience(self, db):
        vti = await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")
        owner = uuid4()
        other = uuid4()
        await _holding(db, vti, "AAPL", Decimal("0.06"), audience=owner)
        await _holding(db, vti, "MSFT", Decimal("0.05"))

        positions_other = await load_positions(
            db.pool,
            composition=[CompositionEntry("VTI", Decimal("1.0"))],
            audience=other,
        )
        tickers_other = {h.ticker for h in positions_other[0].holdings}
        assert "MSFT" in tickers_other
        assert "AAPL" not in tickers_other

    async def test_byo_holdings_visible_to_owner(self, db):
        vti = await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")
        owner = uuid4()
        await _holding(db, vti, "AAPL", Decimal("0.06"), audience=owner)
        await _holding(db, vti, "MSFT", Decimal("0.05"))

        positions_owner = await load_positions(
            db.pool,
            composition=[CompositionEntry("VTI", Decimal("1.0"))],
            audience=owner,
        )
        tickers = {h.ticker for h in positions_owner[0].holdings}
        assert tickers == {"AAPL", "MSFT"}

    async def test_anonymous_sees_shared_only(self, db):
        vti = await _etf(db, "VTI", "Vanguard Total Stock Market", "growth")
        owner = uuid4()
        await _holding(db, vti, "AAPL", Decimal("0.06"), audience=owner)
        await _holding(db, vti, "MSFT", Decimal("0.05"))

        positions = await load_positions(
            db.pool,
            composition=[CompositionEntry("VTI", Decimal("1.0"))],
            audience=None,
        )
        tickers = {h.ticker for h in positions[0].holdings}
        assert tickers == {"MSFT"}
