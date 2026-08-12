"""The entity profile computes only what the store supports, and says so.

The defects these guard against all share a shape: a page that looks complete
because it filled a hole. A margin computed across two different filing periods,
a market cap from a stale share count, a correlation from six overlapping days —
each renders as a confident number and none of them means what it appears to.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from neutron.test import TestClient

from omni.api.profile import _derived, _fundamentals, _risk, _series
from omni.main import create_app


class _Lifespan:
    def __init__(self, app):
        self.app = app
        self.receive = asyncio.Queue()
        self.send = asyncio.Queue()

    async def __aenter__(self):
        self.task = asyncio.create_task(
            self.app({"type": "lifespan"}, self.receive.get, self.send.put)
        )
        await self.receive.put({"type": "lifespan.startup"})
        assert (await self.send.get())["type"] == "lifespan.startup.complete"
        return self.app

    async def __aexit__(self, *exc):
        await self.receive.put({"type": "lifespan.shutdown"})
        await self.send.get()
        await self.task


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


def _row(**kwargs):
    """A stand-in for an asyncpg record; the helpers only subscript by name."""
    return kwargs


async def _entity(db, symbol="TSLA", name="Tesla, Inc.", kind="company"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ($1,$2,$3) RETURNING id",
        kind,
        symbol,
        name,
    )


async def _claim(db, entity_id, *, claim_type, key, value, event_date, knowledge_date=None,
                 source="sec_edgar", unit=None, evidence=None):
    await db.pool.execute(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, unit, evidence, source,
                           event_date, knowledge_date, confidence, redistributable)
        VALUES ($1,$2,$3,$4::jsonb,$5,$6::jsonb,$7,$8,$9,1.0,'allowed')
        """,
        entity_id,
        claim_type,
        key,
        json.dumps(value),
        unit,
        json.dumps(evidence) if evidence is not None else None,
        source,
        event_date,
        knowledge_date or event_date,
    )


async def _setup(client):
    response = await client.post(
        "/auth/setup", json={"email": "operator@example.com", "password": "a" * 16}
    )
    assert response.status_code == 200, response.text
    return {"authorization": f"Bearer {response.json()['token']}"}


async def test_profile_requires_authentication(database_url, db):
    entity_id = await _entity(db)
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        response = await client.get(f"/entities/{entity_id}/profile")
    assert response.status_code == 401


async def test_unknown_entity_is_not_found_rather_than_an_empty_profile(database_url, db):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        missing = await client.get(
            "/entities/17f6dfb1-9c1c-4874-9abc-658913e5a118/profile", headers=headers
        )
        malformed = await client.get("/entities/not-a-uuid/profile", headers=headers)
    assert missing.status_code == 404
    assert malformed.status_code == 404


def test_a_margin_is_refused_across_mismatched_filing_periods():
    """Two periods divided together is a number that describes neither."""
    fundamentals = [
        {"key": "GrossProfit", "label": "Gross profit", "value": 5_000.0,
         "period_end": "2026-06-30"},
        {"key": "Revenues", "label": "Revenue", "value": 20_000.0,
         "period_end": "2026-03-31"},
    ]

    derived, limits = _derived(fundamentals, price=100.0)

    assert derived["gross_margin"] is None
    assert any("different" in note for note in limits)


def test_a_margin_is_computed_when_both_sides_share_a_period():
    fundamentals = [
        {"key": "GrossProfit", "label": "Gross profit", "value": 5_000.0,
         "period_end": "2026-06-30"},
        {"key": "Revenues", "label": "Revenue", "value": 20_000.0,
         "period_end": "2026-06-30"},
    ]

    derived, _ = _derived(fundamentals, price=100.0)

    assert derived["gross_margin"] == pytest.approx(25.0)


def test_market_cap_needs_a_share_count_and_says_so_when_absent():
    derived, limits = _derived([], price=250.0)

    assert derived["market_cap"] is None
    assert any("shares-outstanding" in note for note in limits)


def test_a_restatement_supersedes_the_original_it_corrected():
    """Ordering on the fiscal date would keep showing the corrected number.

    Both rows describe the period ending 2026-06-30. The second was filed a
    month later and revises the figure; the profile must show the revision.
    """
    period = datetime(2026, 6, 30, tzinfo=UTC)
    rows = [
        _row(key="Revenues", value={"value": 22_000.0}, unit="USD",
             evidence={"fp": "Q2", "fy": 2026, "form": "10-Q/A"}, source="sec_edgar",
             event_date=period, knowledge_date=datetime(2026, 8, 20, tzinfo=UTC)),
        _row(key="Revenues", value={"value": 20_000.0}, unit="USD",
             evidence={"fp": "Q2", "fy": 2026, "form": "10-Q"}, source="sec_edgar",
             event_date=period, knowledge_date=datetime(2026, 7, 23, tzinfo=UTC)),
    ]

    out = _fundamentals(rows)

    assert len(out) == 1
    assert out[0]["value"] == 22_000.0
    assert out[0]["form"] == "10-Q/A"
    assert out[0]["knowable_from"] == "2026-08-20"


def test_correlation_is_withheld_when_the_series_barely_overlap():
    import pandas as pd

    days = pd.date_range("2026-01-01", periods=200, freq="D", tz=UTC)
    prices = pd.Series([100.0 + i * 0.5 for i in range(200)], index=days)
    # Only ten sessions in common — far below the floor.
    market = pd.Series([400.0 + i for i in range(10)], index=days[:10])

    risk, limits = _risk(prices, market)

    assert risk["correlation_to_market"] is None
    assert risk["market_behavior"] == "unrated"
    assert any("overlapping sessions" in note for note in limits)


def test_risk_is_withheld_entirely_on_a_short_history():
    import pandas as pd

    days = pd.date_range("2026-01-01", periods=12, freq="D", tz=UTC)
    prices = pd.Series([100.0 + i for i in range(12)], index=days)

    risk, limits = _risk(prices, pd.Series(dtype=float))

    assert risk["volatility"] is None
    assert risk["risk_tier"] == "unrated"
    assert risk["sessions"] == 12
    assert any("price observations" in note for note in limits)


def test_non_positive_and_non_finite_prices_never_enter_the_series():
    rows = [
        _row(value={"value": 100.0}, event_date=datetime(2026, 1, 1, tzinfo=UTC)),
        _row(value={"value": 0.0}, event_date=datetime(2026, 1, 2, tzinfo=UTC)),
        _row(value={"value": -5.0}, event_date=datetime(2026, 1, 3, tzinfo=UTC)),
        _row(value={"value": float("inf")}, event_date=datetime(2026, 1, 4, tzinfo=UTC)),
        _row(value={"value": 110.0}, event_date=datetime(2026, 1, 5, tzinfo=UTC)),
    ]

    series = _series(rows)

    assert list(series.values) == [100.0, 110.0]


async def test_profile_reports_price_risk_and_fundamentals_together(database_url, db):
    entity_id = await _entity(db)
    spy_id = await _entity(db, symbol="SPY", name="S&P 500 ETF", kind="etf")

    start = datetime(2026, 1, 1, tzinfo=UTC)
    for day in range(120):
        stamp = start + timedelta(days=day)
        await _claim(db, entity_id, claim_type="price_snapshot", key="close",
                     value={"value": 200.0 + day * 1.5}, event_date=stamp,
                     source="polygon")
        await _claim(db, spy_id, claim_type="price_snapshot", key="close",
                     value={"value": 500.0 + day * 0.8}, event_date=stamp,
                     source="polygon")

    period = datetime(2026, 6, 30, tzinfo=UTC)
    for key, value in (("Revenues", 25_000.0), ("GrossProfit", 5_000.0),
                       ("CommonStockSharesOutstanding", 1_000.0)):
        await _claim(db, entity_id, claim_type="fundamental_metric", key=key,
                     value={"value": value}, unit="USD", event_date=period,
                     knowledge_date=datetime(2026, 7, 23, tzinfo=UTC),
                     evidence={"fp": "Q2", "fy": 2026, "form": "10-Q"})

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        response = await client.get(f"/entities/{entity_id}/profile", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["entity"]["symbol"] == "TSLA"
    assert body["price"]["latest"] == pytest.approx(200.0 + 119 * 1.5)
    assert body["price"]["source"] == "polygon"
    assert body["risk"]["sessions"] == 120
    assert body["risk"]["volatility"] is not None
    assert body["risk"]["correlation_to_market"] is not None

    labels = {item["label"]: item for item in body["fundamentals"]}
    assert labels["Revenue"]["value"] == 25_000.0
    assert labels["Revenue"]["form"] == "10-Q"
    assert labels["Revenue"]["knowable_from"] == "2026-07-23"

    assert body["derived"]["gross_margin"] == pytest.approx(20.0)
    assert body["derived"]["market_cap"] == pytest.approx(1_000.0 * body["price"]["latest"])


async def test_another_users_private_claims_never_reach_the_profile(database_url, db):
    """The audience rule holds here as everywhere: byo_only is owner-only."""
    entity_id = await _entity(db)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for day in range(60):
        await db.pool.execute(
            """
            INSERT INTO claim (entity_id, claim_type, key, value, source,
                               event_date, knowledge_date, confidence,
                               redistributable, audience_user_id)
            VALUES ($1,'price_snapshot','close',$2::jsonb,'polygon',$3,$3,1.0,
                    'byo_only', gen_random_uuid())
            """,
            entity_id,
            json.dumps({"value": 100.0 + day}),
            start + timedelta(days=day),
        )

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        response = await client.get(f"/entities/{entity_id}/profile", headers=headers)

    body = response.json()
    assert body["price"]["latest"] is None
    assert body["price"]["series"] == []
    assert any("No price observations" in note for note in body["limits"])
