"""GET /trading/eligibility -- the report GATE A is read off.

The router is not mounted in `omni.main` yet (registering it is an orchestrator
step), so these tests mount it onto a real app the same way `create_app` would.

What is pinned here is the report's honesty rather than its shape: a method
nobody has measured must appear rather than vanish, an unknown rate must
serialise as null rather than as zero, the same edge must survive a cheap venue
and not an expensive one, and reading the page must not change the ledger it
describes.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from neutron.test import TestClient

from omni.api.trading import build_router as trading_router
from omni.main import create_app
from omni.trading.policy import Ineligible, TradingPhase

GOOD_SECRET = "x" * 48
NOW = datetime(2026, 8, 7, tzinfo=UTC)

# The report's notional. Chosen to match AUTOTRADE_PLAN.md section 12's on-chain
# row so the gas arithmetic here is the same arithmetic the plan states.
NOTIONAL = "5000"


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
        msg = await self._send.get()
        assert msg["type"] == "lifespan.startup.complete", msg
        return self._app

    async def __aexit__(self, *exc):
        await self._receive.put({"type": "lifespan.shutdown"})
        await self._send.get()
        await self._task


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("OMNI_JWT_SECRET", GOOD_SECRET)
    yield


@pytest.fixture(autouse=True)
async def _clean_users(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


def _app(database_url):
    app = create_app(database_url)
    app.include_router(trading_router(app))
    return app


async def _token(client) -> str:
    r = await client.post(
        "/auth/setup", json={"email": "op@example.com", "password": "a" * 16}
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def _get(client, token, query=f"notional={NOTIONAL}"):
    return await client.get(
        f"/trading/eligibility?{query}",
        headers={"authorization": f"Bearer {token}"},
    )


def _method() -> str:
    return f"apitrading.test.{uuid4().hex[:12]}"


async def _entity(db, kind="company"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ($1,$2,$2) RETURNING id",
        kind,
        uuid4().hex[:12],
    )


async def _predict(
    db,
    entity_id,
    *,
    method,
    resolved_at=None,
    hit=True,
    confidence=0.85,
    backfilled=False,
):
    """Entry 100, target 103, stop 98 -- 300bps up against 200bps down, the
    barrier geometry AUTOTRADE_PLAN.md section 12 prices."""
    provenance = {"capability": method, "input_claims": [], "assumptions": {}}
    if backfilled:
        provenance["assumptions"]["backfill"] = True
    await db.pool.execute(
        """
        INSERT INTO prediction (entity_id, method, direction, confidence,
                                entry_price, upper_barrier, lower_barrier,
                                horizon_ends_at, provenance, outcome, resolved_at)
        VALUES ($1,$2,'up',$3,100,103,98,$4,$5::jsonb,
                $6::prediction_outcome,$7)
        """,
        entity_id,
        method,
        confidence,
        NOW + timedelta(days=30),
        json.dumps(provenance),
        "pending" if resolved_at is None else ("upper" if hit else "lower"),
        resolved_at,
    )


async def _history(db, entity, *, method, n, hits, **kw):
    """`n` resolved predictions spread over `n` hours, `hits` of them correct.

    Spread rather than simultaneous because the endpoint derives its
    walk-forward windows from the span between the first and last outcome, and
    a method whose whole history lands on one instant has no span to split.
    """
    for i in range(n):
        await _predict(
            db,
            entity,
            method=method,
            resolved_at=NOW + timedelta(hours=i),
            hit=i < hits,
            **kw,
        )


def _find(body, method):
    matches = [m for m in body["methods"] if m["method"] == method]
    assert len(matches) == 1, f"{method} appears {len(matches)} times in the report"
    return matches[0]


def _venue(entry, name):
    matches = [v for v in entry["expectancy"]["venues"] if v["venue"] == name]
    assert len(matches) == 1, f"venue {name} missing from {entry['method']}"
    return matches[0]


class TestAccess:
    async def test_an_anonymous_caller_is_refused(self, db, database_url):
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.get(f"/trading/eligibility?notional={NOTIONAL}")
        assert r.status_code == 401

    async def test_the_notional_is_required_rather_than_assumed(self, db, database_url):
        """Gas is a fixed amount per transaction, so its cost in bps is a
        function of trade size; defaulting the size would invent the number the
        on-chain row turns on."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await client.get(
                "/trading/eligibility",
                headers={"authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400
        assert "notional" in r.text

    async def test_a_non_positive_notional_is_refused(self, db, database_url):
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token, query="notional=0")
        assert r.status_code == 400


class TestAnUncalibratedMethodIsReportedNotOmitted:
    async def test_a_method_below_the_calibration_floor_appears_with_a_null_rate(
        self, db, database_url
    ):
        """Silence is how a strategy gets forgotten rather than judged."""
        method, entity = _method(), await _entity(db)
        await _history(db, entity, method=method, n=3, hits=3)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)
        assert r.status_code == 200, r.text

        entry = _find(r.json(), method)
        assert entry["status"] == "uncalibrated"
        assert entry["hit_rate"] is None
        assert entry["hit_rate_interval"] is None
        assert entry["resolved_n"] == 3
        assert entry["measured_n"] == 0
        # Not 0.0: three outcomes have no rate, and reporting one would be a
        # statement the ledger does not support.
        assert entry["expectancy"]["gross_bps"] is None
        assert entry["expectancy"]["venues"] == []
        paper = next(g for g in entry["gates"] if g["phase"] == "paper")
        assert paper["eligible"] is False
        assert paper["reason"] == Ineligible.UNCALIBRATED.value

    async def test_a_method_with_only_pending_predictions_still_appears(
        self, db, database_url
    ):
        method, entity = _method(), await _entity(db)
        for _ in range(5):
            await _predict(db, entity, method=method, resolved_at=None)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        entry = _find(r.json(), method)
        assert entry["total_n"] == 5
        assert entry["resolved_n"] == 0
        assert entry["hit_rate"] is None
        assert entry["status"] == "uncalibrated"
        # Never run, not run and failed.
        assert entry["walk_forward"] is None

    async def test_the_json_carries_null_and_not_zero(self, db, database_url):
        """A serialiser coercing None to 0.0 would satisfy every assertion
        above that reads the parsed body through a `is None` check on a float
        field only if the raw JSON also says null. Read the raw text."""
        method, entity = _method(), await _entity(db)
        await _history(db, entity, method=method, n=3, hits=3)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        raw = json.loads(r.text)
        entry = _find(raw, method)
        assert entry["hit_rate"] is None
        assert not isinstance(entry["hit_rate"], float)


class TestPerVenueExpectancy:
    async def test_the_same_edge_survives_a_cheap_venue_and_not_an_expensive_one(
        self, db, database_url
    ):
        """30 hits in 40 at 300bps target against a 200bps stop.

            gross = 0.75 * 300 - 0.25 * 200 = 225 - 50 = 175bps

            cex_taker   10bps entry + 10bps exit  =  20bps -> net 155
            cex_maker    2bps entry + 10bps exit  =  12bps -> net 163
            onchain_l1  40 quote gas per leg on a 5,000 notional
                        = 80bps x 2               = 160bps -> net  15
            swap_service 75bps x 2                = 150bps -> net  25

        The venue is the only thing that differs. Nothing about the signal
        changed between the first row and the last.
        """
        method, entity = _method(), await _entity(db)
        await _history(db, entity, method=method, n=40, hits=30)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        entry = _find(r.json(), method)
        assert entry["status"] == "calibrated"
        assert entry["measured_n"] == 40
        assert entry["hit_rate"] == pytest.approx(0.75)
        assert Decimal(entry["expectancy"]["target_bps"]) == Decimal(300)
        assert Decimal(entry["expectancy"]["stop_bps"]) == Decimal(200)
        assert Decimal(entry["expectancy"]["gross_bps"]) == Decimal(175)

        assert Decimal(_venue(entry, "cex_taker")["net_bps"]) == Decimal(155)
        assert Decimal(_venue(entry, "cex_maker")["net_bps"]) == Decimal(163)
        assert Decimal(_venue(entry, "onchain_l1")["net_bps"]) == Decimal(15)
        assert Decimal(_venue(entry, "swap_service")["net_bps"]) == Decimal(25)

        assert Decimal(_venue(entry, "onchain_l1")["gas_bps"]) == Decimal(160)
        assert Decimal(_venue(entry, "cex_taker")["gas_bps"]) == Decimal(0)

    async def test_gas_is_a_function_of_size_so_the_ranking_changes_with_it(
        self, db, database_url
    ):
        """At 5,000 the on-chain leg costs 160bps; at 500,000 it costs 1.6bps.

        This is the fact that decides whether a thin edge can go on-chain at
        all, and a cost model treating gas as a constant bps would report the
        same number twice.
        """
        method, entity = _method(), await _entity(db)
        await _history(db, entity, method=method, n=40, hits=30)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            small = await _get(client, token, query="notional=5000")
            large = await _get(client, token, query="notional=500000")

        small_net = Decimal(_venue(_find(small.json(), method), "onchain_l1")["net_bps"])
        large_net = Decimal(_venue(_find(large.json(), method), "onchain_l1")["net_bps"])
        assert small_net == Decimal(15)
        assert large_net == Decimal("173.4")

        # The CEX rows do not move: their cost is proportional to notional.
        assert Decimal(
            _venue(_find(small.json(), method), "cex_taker")["net_bps"]
        ) == Decimal(_venue(_find(large.json(), method), "cex_taker")["net_bps"])

    async def test_a_notional_below_a_venues_minimum_is_a_refusal_not_a_zero(
        self, db, database_url
    ):
        method, entity = _method(), await _entity(db)
        await _history(db, entity, method=method, n=40, hits=30)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token, query="notional=5")

        entry = _find(r.json(), method)
        cex = _venue(entry, "cex_taker")
        assert cex["net_bps"] is None
        assert cex["survives"] is None
        assert "minimum" in cex["refusal"]
        # The on-chain venue has no minimum, so it still prices -- and at a
        # 5-unit notional the gas swamps the edge.
        onchain = _venue(entry, "onchain_l1")
        assert Decimal(onchain["net_bps"]) < 0
        assert onchain["survives"] is False

    async def test_money_is_serialised_as_a_string(self, db, database_url):
        method, entity = _method(), await _entity(db)
        await _history(db, entity, method=method, n=40, hits=30)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        entry = _find(r.json(), method)
        for field in ("gross_bps", "target_bps", "stop_bps"):
            assert isinstance(entry["expectancy"][field], str), field
        for venue in entry["expectancy"]["venues"]:
            assert isinstance(venue["net_bps"], str), venue["venue"]
            assert isinstance(venue["cost_bps"], str), venue["venue"]


class TestGateVerdicts:
    async def test_every_phase_reports_a_verdict_with_a_reason(
        self, db, database_url
    ):
        """A method calibrated and validated for paper, refused for scale.

        100 backfilled outcomes, all correct, spread over 100 hours. The
        calibration and the walk-forward both pass on them -- the outcomes are
        real -- and the scale phase still refuses, because a backfill risked
        nothing and thirty free outcomes are not thirty live ones.
        """
        method, entity = _method(), await _entity(db)
        await _history(db, entity, method=method, n=100, hits=100, backfilled=True)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        entry = _find(r.json(), method)
        assert entry["resolved_n"] == 100
        assert entry["live_resolved_n"] == 0

        forward = entry["walk_forward"]
        assert forward["positive"] is True
        assert forward["pooled_n"] == 80
        assert forward["backfilled_pooled_n"] == 80
        assert forward["live_pooled_n"] == 0

        verdicts = {g["phase"]: g for g in entry["gates"]}
        assert set(verdicts) == {p.value for p in
                                 (TradingPhase.PAPER, TradingPhase.MICRO,
                                  TradingPhase.SCALE)}
        assert verdicts["paper"]["eligible"] is True
        assert verdicts["paper"]["reason"] is None
        assert verdicts["micro"]["eligible"] is True
        assert verdicts["scale"]["eligible"] is False
        assert verdicts["scale"]["reason"] == Ineligible.BACKFILL_ONLY.value
        assert "backfilled outcomes calibrate but risked nothing" in (
            verdicts["scale"]["detail"]
        )

    async def test_a_walk_forward_that_does_not_clear_the_target_closes_paper(
        self, db, database_url
    ):
        """In-sample 76%, forward 70%, and still refused at every phase.

        100 outcomes over 100 hours: the seed slice resolves perfectly, each of
        the four forward slices at 70%. The pooled hit rate is above the 60%
        target, so a verdict read off the point estimate would admit this to
        paper. Its Wilson interval is [0.592, 0.789] -- the sample cannot
        distinguish a 70% edge from a 59% one, and 59% loses money against a
        300/200 barrier after any venue's costs. The gate must refuse, and the
        refusal must be NEGATIVE_EXPECTANCY (tested, failed) rather than
        NO_WALK_FORWARD (never tested).
        """
        method, entity = _method(), await _entity(db)
        for i in range(100):
            await _predict(
                db,
                entity,
                method=method,
                resolved_at=NOW + timedelta(hours=i),
                hit=i < 20 or ((i - 20) % 20) < 14,
            )

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        entry = _find(r.json(), method)
        assert entry["hit_rate"] == pytest.approx(0.76)

        forward = entry["walk_forward"]
        assert forward["pooled_n"] == 80
        assert forward["pooled_hits"] == 56
        assert forward["pooled_hit_rate"] == pytest.approx(0.7)
        assert forward["pooled_hit_rate"] > 0.6
        assert forward["interval"][0] == pytest.approx(0.5923184607866026, abs=1e-12)
        assert forward["positive"] is False

        for gate in entry["gates"]:
            assert gate["eligible"] is False, gate
            assert gate["reason"] == Ineligible.NEGATIVE_EXPECTANCY.value, gate

    async def test_a_method_with_no_walk_forward_is_untested_not_failed(
        self, db, database_url
    ):
        """30 outcomes at one instant: calibrated, but with no span to split.

        The gate must answer NO_WALK_FORWARD -- never tested -- rather than
        NEGATIVE_EXPECTANCY, which would retire a strategy on evidence nobody
        gathered.
        """
        method, entity = _method(), await _entity(db)
        for _ in range(30):
            await _predict(db, entity, method=method, resolved_at=NOW)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        entry = _find(r.json(), method)
        assert entry["walk_forward"] is None
        assert entry["hit_rate"] == pytest.approx(1.0)
        paper = next(g for g in entry["gates"] if g["phase"] == "paper")
        assert paper["reason"] == Ineligible.NO_WALK_FORWARD.value


class TestTheReportWritesNothing:
    async def test_reading_the_page_does_not_change_the_ledger(
        self, db, database_url
    ):
        """A report endpoint with a side effect is a footgun on a page someone
        refreshes while deciding whether to commit capital."""
        method, entity = _method(), await _entity(db)
        await _history(db, entity, method=method, n=40, hits=30)

        async def counts():
            row = await db.pool.fetchrow(
                """
                SELECT (SELECT count(*) FROM prediction)  AS predictions,
                       (SELECT count(*) FROM claim)       AS claims,
                       (SELECT count(*) FROM trade_order) AS orders,
                       (SELECT count(*) FROM portfolio)   AS portfolios,
                       (SELECT count(*) FROM position)    AS positions
                """
            )
            return dict(row)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            before = await counts()
            r = await _get(client, token)
            assert r.status_code == 200, r.text
            # Twice: an endpoint that writes on first read only would still
            # pass a single-call comparison taken after the write.
            again = await _get(client, token)
            assert again.status_code == 200, again.text
            after = await counts()

        assert after == before
        assert before["predictions"] > 0, "the comparison must not be over zero rows"

    async def test_the_counter_would_notice_a_write(self, db, database_url):
        """Proves the comparison above discriminates: the same counter, taken
        around a deliberate insert, changes."""

        async def counts():
            row = await db.pool.fetchrow(
                "SELECT (SELECT count(*) FROM prediction) AS predictions"
            )
            return dict(row)

        entity = await _entity(db)
        before = await counts()
        await _predict(db, entity, method=_method(), resolved_at=NOW)
        assert await counts() != before
