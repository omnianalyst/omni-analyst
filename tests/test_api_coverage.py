"""HTTP read API over coverage.

The redistribution rule is what these tests exist to defend: a private
(byo_only) claim owned by A must not reach B through any endpoint. The other
cases cover the shape of the summary, point-in-time semantics, honest 404s, and
the page cap.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from neutron.test import TestClient

from omni.api.coverage import build_router
from omni.coverage.gaps import detect_gaps, persist_gaps
from omni.main import create_app

NOW = datetime(2026, 7, 27, tzinfo=UTC)
DAWN = datetime(2000, 1, 1, tzinfo=UTC)


async def _entity(db, symbol="AAPL", name=None):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company', $1, $2) "
        "RETURNING id",
        symbol,
        name or symbol,
    )


async def _claim(
    db,
    entity_id,
    *,
    key="Revenues",
    value='{"amount": 1000}',
    source="sec_edgar",
    confidence=0.9,
    knowledge_date=NOW,
    event_date=None,
    audience=None,
    claim_type="fundamental_metric",
):
    redistributable = "allowed" if audience is None else "byo_only"
    ed = event_date or (knowledge_date - timedelta(days=1))
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10)
        RETURNING id
        """,
        entity_id,
        claim_type,
        key,
        value,
        source,
        ed,
        knowledge_date,
        confidence,
        redistributable,
        audience,
    )


async def _demand(
    db,
    entity_id,
    *,
    key="EPS",
    requested_by=None,
    max_staleness=None,
    claim_type="fundamental_metric",
):
    return await db.pool.fetchval(
        "INSERT INTO demand (entity_id, claim_type, key, channel, requested_by, "
        "weight, max_staleness) VALUES ($1, $2::claim_type, $3, 'test', $4, "
        "1.0, $5) RETURNING id",
        entity_id,
        claim_type,
        key,
        requested_by,
        max_staleness,
    )


class _Lifespan:
    """Drive the ASGI lifespan protocol, which httpx's ASGITransport skips."""

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


def _make_app(database_url):
    app = create_app(database_url)
    app.include_router(build_router(app))
    return app


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def test_coverage_summary_reports_per_type_counts_and_newest_knowledge_date(
    db, database_url
):
    entity_id = await _entity(db)
    await _claim(
        db, entity_id, key="Revenues", source="sec_edgar",
        confidence=0.9, knowledge_date=NOW,
    )
    await _claim(
        db, entity_id, key="EPS", source="fred",
        confidence=0.5, knowledge_date=NOW - timedelta(days=400),
    )
    await _claim(
        db, entity_id, key="Revenues", source="fred",
        confidence=0.8, knowledge_date=NOW - timedelta(days=400),
        claim_type="price_snapshot",
    )

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get(f"/coverage/{entity_id}")

    assert r.status_code == 200
    groups = {g["claim_type"]: g for g in r.json()["groups"]}

    fund = groups["fundamental_metric"]
    assert fund["count"] == 2
    assert fund["source_count"] == 2
    assert set(fund["sources"]) == {"sec_edgar", "fred"}
    assert fund["mean_confidence"] == pytest.approx(0.7)
    # Freshness is the point: the group reports the age of its newest vintage.
    assert fund["newest_knowledge_date"].startswith("2026-07-27")
    assert fund["age_seconds"] >= 0

    price = groups["price_snapshot"]
    assert price["count"] == 1
    # The stale group is honestly older.
    assert price["age_seconds"] > fund["age_seconds"]


async def test_a_byo_only_claim_is_not_visible_to_another_user(db, database_url):
    """The leak test: A's private claim and gap must not reach B or anonymous."""
    entity_id = await _entity(db, symbol="PRIV")
    owner_a = uuid4()
    owner_b = uuid4()

    # One shared claim everyone may see, one private claim only A may see.
    await _claim(
        db, entity_id, key="Revenues", source="pubsrc",
        value='{"amount": 1}', audience=None,
    )
    await _claim(
        db, entity_id, key="Revenues", source="privsrc",
        value='{"amount": 999}', audience=owner_a,
    )

    # A private gap for A (stale, single-source) plus a shared gap, persisted so
    # the gaps endpoint reads them from the table.
    await _demand(db, entity_id, key="EPS", requested_by=owner_a,
                  max_staleness=timedelta(days=1))
    await _claim(db, entity_id, key="EPS", source="privsrc2",
                 knowledge_date=DAWN, audience=owner_a)
    await _demand(db, entity_id, key="Sales", requested_by=None)
    await persist_gaps(db.pool, await detect_gaps(db.pool))

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        # --- A sees its own private coverage ---
        r_a = await client.get(
            f"/coverage/{entity_id}/claims", headers={"X-User-Id": str(owner_a)}
        )
        r_a_sum = await client.get(
            f"/coverage/{entity_id}", headers={"X-User-Id": str(owner_a)}
        )
        r_a_gaps = await client.get(
            f"/gaps/{entity_id}", headers={"X-User-Id": str(owner_a)}
        )
        # --- B must not see any of A's private data ---
        r_b = await client.get(
            f"/coverage/{entity_id}/claims", headers={"X-User-Id": str(owner_b)}
        )
        r_b_sum = await client.get(
            f"/coverage/{entity_id}", headers={"X-User-Id": str(owner_b)}
        )
        r_b_gaps = await client.get(
            f"/gaps/{entity_id}", headers={"X-User-Id": str(owner_b)}
        )
        # --- anonymous sees only the shared network ---
        r_anon = await client.get(f"/coverage/{entity_id}/claims")
        r_anon_gaps = await client.get(f"/gaps/{entity_id}")

    assert r_a.status_code == 200
    a_sources = {c["source"] for c in r_a.json()["claims"]}
    assert "privsrc" in a_sources  # A can see its own private claim

    # B: no private source, no private value, anywhere.
    b_claims = r_b.json()["claims"]
    assert {c["source"] for c in b_claims} == {"pubsrc"}
    assert all(c["value"] != {"amount": 999} for c in b_claims)
    b_group = r_b_sum.json()["groups"][0]
    assert b_group["count"] == 1
    assert b_group["sources"] == ["pubsrc"]

    # Anonymous: same view as B -- only the shared network.
    anon_sources = {c["source"] for c in r_anon.json()["claims"]}
    assert anon_sources == {"pubsrc"}
    assert all(c["value"] != {"amount": 999} for c in r_anon.json()["claims"])

    # A's summary sees its private claims; B's sees only the shared network.
    a_group = r_a_sum.json()["groups"][0]
    assert a_group["count"] == 3  # shared + Revenues-private + EPS-private
    assert "privsrc" in a_group["sources"]

    # Gaps: A's private gaps never reach B or anonymous.
    def _gap_audiences(resp):
        return {g["audience_user_id"] for g in resp.json()["gaps"]}

    assert str(owner_a) in _gap_audiences(r_a_gaps)        # A sees its own
    assert str(owner_a) not in _gap_audiences(r_b_gaps)    # B does not
    assert str(owner_a) not in _gap_audiences(r_anon_gaps) # neither does anon


async def test_as_of_returns_the_older_vintage_not_the_latest(db, database_url):
    """Two vintages of one period: as_of before the revision returns the first."""
    entity_id = await _entity(db)
    period = datetime(2024, 1, 1, tzinfo=UTC)
    await _claim(
        db, entity_id, key="Revenues", source="sec",
        value='{"amount": 100}', event_date=period,
        knowledge_date=datetime(2024, 2, 1, tzinfo=UTC),
    )
    # A later revision of the same period.
    await _claim(
        db, entity_id, key="Revenues", source="sec",
        value='{"amount": 150}', event_date=period,
        knowledge_date=datetime(2024, 3, 1, tzinfo=UTC),
    )

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        # Snapshot taken between the two vintages knows only the first.
        r_pit = await client.get(
            f"/coverage/{entity_id}/claims"
            "?as_of=2024-02-15T00:00:00%2B00:00"
        )
        # Without as_of, both current vintages are visible.
        r_now = await client.get(f"/coverage/{entity_id}/claims")

    pit_claims = r_pit.json()["claims"]
    assert len(pit_claims) == 1
    assert pit_claims[0]["value"] == {"amount": 100}  # the older vintage
    assert pit_claims[0]["knowledge_date"].startswith("2024-02-01")

    assert len(r_now.json()["claims"]) == 2
    assert r_now.json()["claims"][0]["value"] == {"amount": 150}  # newest first


async def test_unknown_entity_returns_404_problem_detail(db, database_url):
    app = _make_app(database_url)
    unknown = uuid4()
    async with _Lifespan(app), TestClient(app) as client:
        for path in (
            f"/coverage/{unknown}",
            f"/coverage/{unknown}/claims",
            f"/gaps/{unknown}",
        ):
            r = await client.get(path)
            assert r.status_code == 404, path
            assert r.headers["content-type"].startswith(
                "application/problem+json"
            ), path
            body = r.json()
            assert body["status"] == 404
            assert "type" in body and "title" in body and "detail" in body


async def test_page_cap_is_enforced(db, database_url):
    entity_id = await _entity(db)
    # 1001 distinct periods, all shared/visible.
    await db.pool.execute(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable)
        SELECT $1, 'fundamental_metric', 'K', '{"a":1}', 's',
               ('2020-01-01'::timestamptz + (n || ' days')::interval),
               ('2020-01-02'::timestamptz + (n || ' days')::interval),
               0.5, 'allowed'
        FROM generate_series(1, 1001) AS n
        """,
        entity_id,
    )

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        # A request far past the cap is clamped, not rejected or expanded.
        r = await client.get(
            f"/coverage/{entity_id}/claims?limit=100000"
        )

    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 1000  # effective cap
    assert len(body["claims"]) == 1000


async def test_entities_search_by_symbol_or_name(db, database_url):
    await _entity(db, symbol="AAPL", name="Apple")
    await _entity(db, symbol="MSFT", name="Microsoft")
    await _entity(db, symbol="GOOG", name="Alphabet")

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r_sym = await client.get("/entities?q=AA")
        r_name = await client.get("/entities?q=Micro")

    syms = {e["symbol"] for e in r_sym.json()["entities"]}
    assert syms == {"AAPL"}
    names = {e["symbol"] for e in r_name.json()["entities"]}
    assert names == {"MSFT"}


async def test_claims_can_be_filtered_to_one_series(db, database_url):
    """Without this, a point-in-time question pages through everything else.

    The revision of GDP Q1-1994 knowable in 2000 sat at rank 210 of a
    100-row page, so the honest answer was unreachable through the API.
    """
    entity_id = await _entity(db, symbol="US", name="United States")
    await _claim(db, entity_id, key="GDP", claim_type="macro_series_point")
    await _claim(db, entity_id, key="UNRATE", claim_type="macro_series_point")

    app = _make_app(database_url)
    async with _Lifespan(app):
        async with TestClient(app) as client:
            both = await client.get(f"/coverage/{entity_id}/claims")
            one = await client.get(f"/coverage/{entity_id}/claims?key=GDP")

    assert {c["key"] for c in both.json()["claims"]} == {"GDP", "UNRATE"}
    assert {c["key"] for c in one.json()["claims"]} == {"GDP"}
