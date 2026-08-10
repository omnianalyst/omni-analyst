"""The trading read API: eligibility, portfolio, reconciliation.

What is pinned here is each report's honesty rather than its shape: a method
nobody has measured must appear rather than vanish, an unknown rate must
serialise as null rather than as zero, the same edge must survive a cheap venue
and not an expensive one, a short must stay signed, a portfolio that is not
there must not read as one that is empty, a venue nobody has checked must not
read as one that agreed, and reading any of these pages must not change what it
describes.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from neutron.test import TestClient

from omni.api.trading import build_router as trading_router
from omni.main import create_app
from omni.portfolio.reconcile import (
    Discrepancy,
    Divergence,
    ReconciliationResult,
    record,
)
from omni.portfolio.state import create_portfolio
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


async def _operator(client) -> tuple[str, UUID]:
    """The first-run operator's token and the user id it authenticates as."""
    r = await client.post(
        "/auth/setup", json={"email": "op@example.com", "password": "a" * 16}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["token"], UUID(body["user"]["id"])


async def _second_user(client, token) -> UUID:
    r = await client.post(
        "/auth/register",
        json={"email": "other@example.com", "password": "b" * 16},
        headers={"authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return UUID(r.json()["id"])


async def _read(client, token, path):
    return await client.get(path, headers={"authorization": f"Bearer {token}"})


async def _portfolio(db, user_id, *, base_currency="USD") -> UUID:
    return await db.pool.fetchval(
        """
        INSERT INTO portfolio (user_id, name, base_currency)
        VALUES ($1, $2, $3) RETURNING id
        """,
        user_id,
        f"book-{uuid4().hex[:8]}",
        base_currency,
    )


async def _hold(
    db,
    portfolio_id,
    *,
    venue,
    symbol,
    quantity,
    average_entry,
    market_type="spot",
    updated_at=NOW,
):
    await db.pool.execute(
        """
        INSERT INTO position (portfolio_id, venue, symbol, market_type,
                              quantity, average_entry, updated_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
        """,
        portfolio_id,
        venue,
        symbol,
        market_type,
        Decimal(quantity),
        Decimal(average_entry),
        updated_at,
    )


async def _cash(
    db, portfolio_id, *, venue, asset, free, locked="0", updated_at=NOW
):
    await db.pool.execute(
        """
        INSERT INTO cash_balance (portfolio_id, venue, asset, free, locked,
                                  updated_at)
        VALUES ($1,$2,$3,$4,$5,$6)
        """,
        portfolio_id,
        venue,
        asset,
        Decimal(free),
        Decimal(locked),
        updated_at,
    )


async def _stale_after(db, portfolio_id, *, venue, after, active=True):
    """The operator's own bound on how old a reconciliation may be.

    A `risk_alert` row of kind `reconciliation`, which is where the alerting
    side already reads it from. Written through the real table rather than
    stubbed, so a report that read its freshness bound from somewhere else --
    a constant, a query parameter, a default -- would not see this.
    """
    await db.pool.execute(
        """
        INSERT INTO risk_alert (portfolio_id, kind, threshold, venue, stale_after,
                                active)
        VALUES ($1, 'reconciliation', $2, $3, $4, $5)
        """,
        portfolio_id,
        Decimal("0.01"),
        venue,
        after,
        active,
    )


async def _reconciliation(db, portfolio_id, *, venue, checked_at, discrepancies=()):
    """Store one reconciliation result through the writer production uses."""
    await record(
        db.pool,
        ReconciliationResult(
            reconciled=not discrepancies,
            discrepancies=tuple(discrepancies),
            checked_at=checked_at,
            venue=venue,
        ),
        portfolio_id=portfolio_id,
    )


def _missing_at_venue(venue, symbol, local):
    """We hold it, the venue reports nothing -- so `remote` is genuinely absent."""
    return Discrepancy(
        kind=Divergence.POSITION_MISSING_AT_VENUE,
        venue=venue,
        symbol=symbol,
        local=Decimal(local),
        remote=None,
        detail=f"we hold {local} {symbol} and {venue} reports no such position",
    )


def _held(body, symbol):
    matches = [p for p in body["positions"] if p["symbol"] == symbol]
    assert len(matches) == 1, f"{symbol} appears {len(matches)} times in the book"
    return matches[0]


def _venue_row(body, name):
    matches = [v for v in body["venues"] if v["venue"] == name]
    assert len(matches) == 1, f"{name} appears {len(matches)} times in the report"
    return matches[0]


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


async def _entities(db, n, kind="company"):
    return [await _entity(db, kind) for _ in range(n)]


async def _predict(
    db,
    entity_id,
    *,
    method,
    resolved_at=None,
    hit=True,
    confidence=0.85,
    backfilled=False,
    horizon_days=30,
    upper="103",
    lower="98",
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
        VALUES ($1,$2,'up',$3,100,$8,$9,$4,$5::jsonb,
                $6::prediction_outcome,$7)
        """,
        entity_id,
        method,
        confidence,
        NOW + timedelta(days=horizon_days),
        json.dumps(provenance),
        "pending" if resolved_at is None else ("upper" if hit else "lower"),
        resolved_at,
        Decimal(upper),
        Decimal(lower),
    )


async def _history(db, entities, *, method, n, hits, **kw):
    """`n` resolved predictions over `n` hours and `n` horizon dates, `hits` correct.

    Three things are spread rather than shared, and each one is load-bearing:

    - **Resolution time**, because the endpoint derives its walk-forward windows
      from the span between the first and last outcome, and a method whose whole
      history lands on one instant has no span to split.
    - **Horizon date**, because the gate's sample floor applies to `effective_n`
      -- the count of distinct horizons -- and a fixture resolving everything on
      one date is one observation however many rows it has.
    - **Entity**, because the gate refuses a book carried by a single name, and
      a one-entity fixture is 100% concentrated by definition.
    """
    for i in range(n):
        await _predict(
            db,
            entities[i % len(entities)],
            method=method,
            resolved_at=NOW + timedelta(hours=i),
            hit=i < hits,
            horizon_days=30 + i,
            **kw,
        )


def _find(body, method):
    matches = [m for m in body["methods"] if m["method"] == method]
    assert len(matches) == 1, f"{method} appears {len(matches)} times in the report"
    return matches[0]


def _venue(entry, name, block="expectancy"):
    matches = [v for v in entry[block]["venues"] if v["venue"] == name]
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
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=3, hits=3)

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
        # Nothing has resolved, so there is no realised edge -- and every figure
        # describing it is null rather than zero. A zero here would read as "we
        # measured it and it was flat", which is a different and false claim.
        realised = entry["realised"]
        assert realised["n"] == 0
        assert realised["effective_n"] == 0
        assert realised["gross_bps"] is None
        assert realised["net_bps"] is None
        assert realised["assumed_share"] is None
        assert realised["concentration"] is None
        assert realised["venues"] == []
        assert "unmeasured" in realised["refusal"]

    async def test_the_json_carries_null_and_not_zero(self, db, database_url):
        """A serialiser coercing None to 0.0 would satisfy every assertion
        above that reads the parsed body through a `is None` check on a float
        field only if the raw JSON also says null. Read the raw text."""
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=3, hits=3)

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
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=40, hits=30)

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
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=40, hits=30)

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
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=40, hits=30)

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
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=40, hits=30)

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
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=100, hits=100, backfilled=True)

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
        method, entities = _method(), await _entities(db, 3)
        for i in range(100):
            await _predict(
                db,
                entities[i % len(entities)],
                method=method,
                resolved_at=NOW + timedelta(hours=i),
                hit=i < 20 or ((i - 20) % 20) < 14,
                horizon_days=30 + i,
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

        One instant is the *resolution* time, which is what the walk-forward
        splits. The horizons are still distinct, because a horizon date is a
        property of the prediction rather than of when it was scored, and
        collapsing them would refuse this on sample size before the question
        under test was reached.
        """
        method, entities = _method(), await _entities(db, 3)
        for i in range(30):
            await _predict(
                db,
                entities[i % len(entities)],
                method=method,
                resolved_at=NOW,
                horizon_days=30 + i,
            )

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        entry = _find(r.json(), method)
        assert entry["walk_forward"] is None
        assert entry["hit_rate"] == pytest.approx(1.0)
        paper = next(g for g in entry["gates"] if g["phase"] == "paper")
        assert paper["reason"] == Ineligible.NO_WALK_FORWARD.value


class TestTheRealisedEdgeIsReported:
    """The quantity the gate reads, on the page the gate is read off.

    A verdict whose input is not printed next to it cannot be argued with, and
    the previous gate survived as long as it did precisely because the number it
    barred on was never shown against the number that mattered.
    """

    async def test_every_realised_figure_appears_per_method(self, db, database_url):
        """40 predictions over 3 names, 30 correct, 300bps against 200bps.

            realised gross = (30 * 300 - 10 * 200) / 40 = +175 bps
            net of the CEX taker round trip (20bps)     = +155 bps

        `effective_n` equals `n` here because every prediction carries its own
        horizon date. The figure exists to say when it does not.
        """
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=40, hits=30)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        realised = _find(r.json(), method)["realised"]
        assert realised["n"] == 40
        assert realised["effective_n"] == 40
        assert Decimal(realised["gross_bps"]) == Decimal(175)
        assert Decimal(realised["net_bps"]) == Decimal(155)
        assert Decimal(realised["round_trip_cost_bps"]) == Decimal(20)
        assert realised["cost_venue"] == "cex_taker"
        assert Decimal(realised["assumed_share"]) == 0
        assert round(Decimal(realised["concentration"]), 4) == Decimal("0.3429")
        assert realised["positive_entities"] == 3

    async def test_the_realised_edge_is_priced_at_every_venue(self, db, database_url):
        """The same +175bps against four cost models.

        Identical arithmetic to the modelled block's venue table, because the
        venue is the only thing that differs between these four rows -- nothing
        about the record changed between the first and the last.
        """
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=40, hits=30)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        entry = _find(r.json(), method)
        assert Decimal(_venue(entry, "cex_taker", "realised")["net_bps"]) == Decimal(155)
        assert Decimal(_venue(entry, "cex_maker", "realised")["net_bps"]) == Decimal(163)
        assert Decimal(_venue(entry, "onchain_l1", "realised")["net_bps"]) == Decimal(15)
        assert Decimal(
            _venue(entry, "swap_service", "realised")["net_bps"]
        ) == Decimal(25)
        assert _venue(entry, "onchain_l1", "realised")["survives"] is True

    async def test_realised_and_modelled_agree_when_every_trade_shares_a_geometry(
        self, db, database_url
    ):
        """They are computed from different things and must land on the number.

        The modelled figure is the calibrated hit rate applied to the *average*
        barrier distance; the realised one is each trade's own P&L pooled. When
        every prediction carries the same barriers those are the same
        arithmetic, and a disagreement here would mean one of the two is reading
        the ledger wrongly rather than that the strategy changed.
        """
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=40, hits=30)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        entry = _find(r.json(), method)
        assert Decimal(entry["realised"]["gross_bps"]) == Decimal(
            entry["expectancy"]["gross_bps"]
        )

    async def test_a_method_the_gate_refuses_is_still_on_the_page(
        self, db, database_url
    ):
        """67% correct, and losing money on every trade.

            entry 100, target 101 (+100bps), stop 96 (-400bps)
            45 predictions over 3 names, 30 correct

            realised gross = (30 * 100 - 15 * 400) / 45 = -66.67 bps

        The hit rate clears the 0.6 target that used to be the bar, so the old
        gate would have funded this. It appears, refused, with the reason -- a
        method that vanishes from the report stops being judged rather than
        passing.
        """
        method, entities = _method(), await _entities(db, 3)
        for i in range(45):
            await _predict(
                db,
                entities[i % len(entities)],
                method=method,
                resolved_at=NOW + timedelta(hours=i),
                hit=i < 30,
                horizon_days=30 + i,
                upper="101",
                lower="96",
            )

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        entry = _find(r.json(), method)
        assert entry["status"] == "calibrated"
        assert entry["hit_rate"] == pytest.approx(2 / 3)
        assert entry["hit_rate"] > 0.6
        assert round(Decimal(entry["realised"]["gross_bps"]), 2) == Decimal("-66.67")
        for gate in entry["gates"]:
            assert gate["eligible"] is False, gate
            assert gate["reason"] == Ineligible.BELOW_EXPECTANCY.value, gate

    async def test_the_gate_parameters_are_printed_with_the_verdicts(
        self, db, database_url
    ):
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=40, hits=30)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        params = r.json()["gate_parameters"]
        assert Decimal(params["round_trip_cost_bps"]) == Decimal(20)
        assert params["cost_venue"] == "cex_taker"
        assert Decimal(params["min_expectancy_bps"]) > 0
        assert params["min_effective_n"] == 30
        assert Decimal(params["max_assumed_share"]) == Decimal("0.5")
        assert Decimal(params["max_concentration"]) == Decimal("0.5")

    async def test_the_realised_money_is_serialised_as_a_string(
        self, db, database_url
    ):
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=40, hits=30)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token = await _token(client)
            r = await _get(client, token)

        raw = json.loads(r.text)
        realised = _find(raw, method)["realised"]
        for field in (
            "gross_bps",
            "net_bps",
            "round_trip_cost_bps",
            "assumed_share",
            "concentration",
        ):
            assert isinstance(realised[field], str), field
            assert not isinstance(realised[field], float), field
        for venue in realised["venues"]:
            assert isinstance(venue["net_bps"], str), venue["venue"]


class TestTheReportWritesNothing:
    async def test_reading_the_page_does_not_change_the_ledger(
        self, db, database_url
    ):
        """A report endpoint with a side effect is a footgun on a page someone
        refreshes while deciding whether to commit capital."""
        method, entities = _method(), await _entities(db, 3)
        await _history(db, entities, method=method, n=40, hits=30)

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


class TestCarryCycles:
    async def test_an_anonymous_caller_is_refused(self, db, database_url):
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.get("/trading/cycles")
        assert r.status_code == 401

    async def test_halts_and_abstentions_are_returned_not_filtered(
        self, db, database_url
    ):
        """A halt is the most important thing this endpoint can say.

        A reader shown only successful cycles cannot tell a book that is working
        from one that has been refusing every night for a month.
        """
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user = await _operator(client)
            book = await create_portfolio(
                db.pool, user_id=user, name="carry", base_currency="USD",
                opening_cash=Decimal(1000), cash_venue="paper",
            )
            for day, halted, reason in ((2, True, "venue_disagrees"), (1, False, None)):
                await db.pool.execute(
                    "INSERT INTO carry_cycle (portfolio_id, venue, as_of, "
                    "funding_since, funding_settled_through, halted, halt_reason, "
                    "abstention, funding_collected, fees_paid, "
                    "modelled_turnover_cost, pairs_opened, pairs_closed, pairs_held) "
                    "VALUES ($1,'paper',$2,$3,$4,$5,$6,NULL,0,0,0,0,0,0)",
                    book.portfolio_id, NOW - timedelta(days=day),
                    NOW - timedelta(days=day + 1),
                    None if halted else NOW - timedelta(days=day),
                    halted, reason,
                )
            r = await _read(client, token, "/trading/cycles")

        cycles = r.json()["cycles"]
        assert len(cycles) == 2
        # Newest first, and the halted one is present with its reason.
        assert cycles[0]["halted"] is False
        assert cycles[1]["halted"] is True
        assert cycles[1]["halt_reason"] == "venue_disagrees"
        assert cycles[1]["funding_settled_through"] is None


class TestNavHistory:
    async def test_an_anonymous_caller_is_refused(self, db, database_url):
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.get("/trading/nav-history")
        assert r.status_code == 401

    async def test_a_book_with_no_snapshots_returns_an_empty_series(
        self, db, database_url
    ):
        """Empty, not absent, and not a fabricated starting point.

        A book that has never been marked has no history, and inventing an
        opening point at its cash would draw a valuation nobody took.
        """
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user = await _operator(client)
            book = await create_portfolio(
                db.pool, user_id=user, name="carry", base_currency="USD",
                opening_cash=Decimal(1000), cash_venue="paper",
            )
            r = await _read(client, token, "/trading/nav-history")
        assert r.status_code == 200
        assert r.json()["points"] == []
        assert r.json()["portfolio_id"] == str(book.portfolio_id)

    async def test_recorded_points_come_back_oldest_first_as_strings(
        self, db, database_url
    ):
        """Every monetary value is a JSON string, per the frozen contract: a
        float round trip loses precision on exactly the values that matter."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user = await _operator(client)
            book = await create_portfolio(
                db.pool, user_id=user, name="carry", base_currency="USD",
                opening_cash=Decimal(1000), cash_venue="paper",
            )
            # Inserted newest-first on purpose: the endpoint must order by taken_at,
            # not by insertion. Two days ago is the lower NAV, so a correct
            # response reads as a rising curve.
            for day, nav in ((1, "1002.50"), (2, "1001.25")):
                await db.pool.execute(
                    "INSERT INTO nav_snapshot (portfolio_id, nav, cash, "
                    "gross_exposure, net_exposure, taken_at) "
                    "VALUES ($1,$2,$3,0,0,$4)",
                    book.portfolio_id, Decimal(nav), Decimal(1000),
                    NOW - timedelta(days=day),
                )
            r = await _read(client, token, "/trading/nav-history")

        points = r.json()["points"]
        assert [p["nav"] for p in points] == ["1001.25", "1002.50"]
        assert all(isinstance(p["nav"], str) for p in points)


class TestPortfolioAccess:
    async def test_an_anonymous_caller_is_refused(self, db, database_url):
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.get("/trading/portfolio")
        assert r.status_code == 401

    async def test_a_missing_portfolio_is_a_404_and_not_an_empty_book(
        self, db, database_url
    ):
        """An empty portfolio and a portfolio that is not there are different
        facts. A 200 carrying zeros tells an operator their book is flat when
        what actually happened is that they are looking at the wrong account.
        """
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            r = await _read(client, token, "/trading/portfolio")

        assert r.status_code == 404, r.text
        body = json.loads(r.text)
        # Not merely "the status is 404": a payload that also carried a book
        # would let a client read the body and ignore the code.
        assert "positions" not in body
        assert "nav" not in body

    async def test_another_accounts_portfolio_is_not_served(self, db, database_url):
        """A portfolio names positions, cash and NAV. Serving one across
        accounts is the leak the audience scoping exists to prevent, and a 403
        would still confirm the portfolio is there."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            other = await _second_user(client, token)
            theirs = await _portfolio(db, other)
            await _hold(
                db,
                theirs,
                venue="binance",
                symbol="SECRET/USD",
                quantity="7",
                average_entry="100",
            )

            r = await _read(client, token, f"/trading/portfolio?portfolio_id={theirs}")

        assert r.status_code == 404, r.text
        assert "SECRET/USD" not in r.text

    async def test_an_unparseable_portfolio_id_is_refused(self, db, database_url):
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            await _portfolio(db, user_id)
            r = await _read(client, token, "/trading/portfolio?portfolio_id=not-a-uuid")

        assert r.status_code == 400
        assert "portfolio_id" in r.text

    async def test_two_portfolios_with_nothing_naming_one_is_refused(
        self, db, database_url
    ):
        """Rather than picked. Any rule for picking -- oldest, largest -- would
        answer about one book while it is read as an answer about the other."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            first = await _portfolio(db, user_id)
            second = await _portfolio(db, user_id)
            await _hold(
                db,
                first,
                venue="binance",
                symbol="BTC/USD",
                quantity="1",
                average_entry="100",
            )

            ambiguous = await _read(client, token, "/trading/portfolio")
            named = await _read(
                client, token, f"/trading/portfolio?portfolio_id={second}"
            )

        assert ambiguous.status_code == 400, ambiguous.text
        assert "BTC/USD" not in ambiguous.text
        # And naming one resolves it: the refusal is about ambiguity, not a
        # blanket refusal to serve an account that holds more than one book.
        assert named.status_code == 200, named.text
        assert named.json()["portfolio_id"] == str(second)


class TestThePortfolioIsReportedAsStored:
    async def test_a_short_is_reported_signed(self, db, database_url):
        """`abs(quantity)` would render a 3-unit short as a 3-unit long.

        Every downstream reading of the number -- exposure, the direction an
        operator believes they are carrying, whether a hedge is on -- inverts
        with the sign, and the magnitude is identical either way, so nothing
        else on the page would look wrong.
        """
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(
                db,
                portfolio_id,
                venue="binance",
                symbol="ETH/USD",
                quantity="-3",
                average_entry="50",
            )

            r = await _read(client, token, "/trading/portfolio")

        assert r.status_code == 200, r.text
        held = _held(r.json(), "ETH/USD")
        assert Decimal(held["quantity"]) == Decimal(-3)
        assert Decimal(held["quantity"]) < 0
        assert held["is_short"] is True
        # Notional is a size and stays positive -- it is |quantity| * entry --
        # so it cannot stand in for the sign check above.
        assert Decimal(held["notional"]) == Decimal(150)

    async def test_exposure_distinguishes_a_hedged_book_from_a_directional_one(
        self, db, database_url
    ):
        """Long 200 against short 150.

            gross = |2 * 100| + |-3 * 50| = 350   -- how much is at risk
            net   =   2 * 100 +  -3 * 50  =  50   -- which way it leans

        A net computed over absolute quantities would report 350 for both, and
        a book that is nearly flat would read as one carrying full directional
        risk. The two figures are the `PortfolioState` properties; the endpoint
        must not recompute them, because two implementations of one quantity is
        how they come to disagree.
        """
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(
                db,
                portfolio_id,
                venue="binance",
                symbol="BTC/USD",
                quantity="2",
                average_entry="100",
            )
            await _hold(
                db,
                portfolio_id,
                venue="binance",
                symbol="ETH/USD",
                quantity="-3",
                average_entry="50",
            )

            r = await _read(client, token, "/trading/portfolio")

        body = r.json()
        assert Decimal(body["gross_exposure"]) == Decimal(350)
        assert Decimal(body["net_exposure"]) == Decimal(50)
        assert Decimal(body["net_exposure"]) != Decimal(body["gross_exposure"])

    async def test_cash_is_the_base_currency_total_and_the_split_is_kept(
        self, db, database_url
    ):
        """9,000 free and 1,000 locked is 10,000 of cash, of which 1,000 cannot
        be spent. Reporting only the total hides an order the book has already
        committed capital to, which is the case reconciliation exists to catch.

        The EUR row is in `cash_positions` and not in `cash`: converting it
        would need an FX rate nobody supplied, and inventing one would put a
        fabricated number into NAV.
        """
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id, base_currency="USD")
            await _cash(
                db,
                portfolio_id,
                venue="binance",
                asset="USD",
                free="9000",
                locked="1000",
            )
            await _cash(db, portfolio_id, venue="kraken", asset="EUR", free="500")

            r = await _read(client, token, "/trading/portfolio")

        body = r.json()
        assert Decimal(body["cash"]) == Decimal(10000)
        usd = next(c for c in body["cash_positions"] if c["asset"] == "USD")
        assert Decimal(usd["free"]) == Decimal(9000)
        assert Decimal(usd["locked"]) == Decimal(1000)
        eur = next(c for c in body["cash_positions"] if c["asset"] == "EUR")
        assert Decimal(eur["free"]) == Decimal(500)
        # NAV is cash plus the book at cost basis; with no positions it is the
        # base-currency cash and the EUR is not folded in at some invented rate.
        assert Decimal(body["nav"]) == Decimal(10000)

    async def test_market_type_is_the_lowercase_protocol_value(
        self, db, database_url
    ):
        """Spot and a perpetual on the same symbol are two exposures with
        different funding and different liquidation behaviour."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(
                db,
                portfolio_id,
                venue="binance",
                symbol="BTC/USD",
                quantity="1",
                average_entry="100",
                market_type="perpetual",
            )

            r = await _read(client, token, "/trading/portfolio")

        assert _held(r.json(), "BTC/USD")["market_type"] == "perpetual"

    async def test_every_money_field_is_a_string_in_the_raw_json(
        self, db, database_url
    ):
        """A float round trip loses precision on exactly the values that matter,
        and a parsed body cannot tell a string from a number the parser already
        coerced. Read the text."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(
                db,
                portfolio_id,
                venue="binance",
                symbol="BTC/USD",
                quantity="0.1",
                average_entry="63500.55",
            )
            await _cash(
                db,
                portfolio_id,
                venue="binance",
                asset="USD",
                free="10000.10",
                locked="0.05",
            )

            r = await _read(client, token, "/trading/portfolio")

        raw = json.loads(r.text)
        for field in ("nav", "cash", "gross_exposure", "net_exposure"):
            assert isinstance(raw[field], str), field
        held = _held(raw, "BTC/USD")
        for field in ("quantity", "average_entry", "notional"):
            assert isinstance(held[field], str), field
        assert Decimal(held["average_entry"]) == Decimal("63500.55")
        for cash in raw["cash_positions"]:
            for field in ("free", "locked"):
                assert isinstance(cash[field], str), field
        # is_short is a fact, not an amount, and stays a boolean.
        assert held["is_short"] is False

    async def test_the_timestamps_carry_an_explicit_offset(self, db, database_url):
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(
                db,
                portfolio_id,
                venue="binance",
                symbol="BTC/USD",
                quantity="1",
                average_entry="100",
            )

            r = await _read(client, token, "/trading/portfolio")

        body = r.json()
        assert datetime.fromisoformat(body["as_of"]).tzinfo is not None
        assert datetime.fromisoformat(_held(body, "BTC/USD")["as_of"]) == NOW


class TestReconciliationReportsWhatIsRecorded:
    """Four statuses, and the three that must never be mistaken for the fourth.

    Results are persisted now, so `reconciled`, `diverged` and `stale` are all
    reachable -- which is exactly when `never_run` becomes easy to lose. A venue
    with no stored row is not a venue that agreed, and the difference between
    those two answers is the difference between an operator checking before they
    commit capital and an operator not checking. These tests pin every path to
    `reconciled` as a path that had evidence behind it: a stored pass, at this
    venue, inside a freshness bound the operator stated.
    """

    async def test_an_anonymous_caller_is_refused(self, db, database_url):
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.get("/trading/reconciliation")
        assert r.status_code == 401

    async def test_a_venue_never_checked_is_never_run_and_not_reconciled(
        self, db, database_url
    ):
        """The substitution this whole enum exists to make unrepresentable.

        A venue with no stored check has not been shown to agree with the book.
        It has not been looked at. Reporting it as `reconciled` states a verdict
        no evidence exists for, on the page an operator reads before committing
        capital -- and the two statuses are indistinguishable to anything that
        only asserts a status is present.
        """
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(
                db,
                portfolio_id,
                venue="binance",
                symbol="BTC/USD",
                quantity="1",
                average_entry="100",
            )

            r = await _read(client, token, "/trading/reconciliation")

        assert r.status_code == 200, r.text
        row = _venue_row(r.json(), "binance")
        assert row["status"] == "never_run"
        assert row["status"] != "reconciled"
        # Never checked, so there is no moment at which it was.
        assert row["checked_at"] is None
        assert row["discrepancies"] == []

    async def test_no_venue_anywhere_in_the_report_reads_as_healthy(
        self, db, database_url
    ):
        """Across a book at three venues, held three different ways. A status
        derived per venue rather than stated once is the shape a fail-open
        substitution hides in, so the assertion is over every row."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(
                db,
                portfolio_id,
                venue="binance",
                symbol="BTC/USD",
                quantity="1",
                average_entry="100",
            )
            await _hold(
                db,
                portfolio_id,
                venue="okx",
                symbol="ETH/USD",
                quantity="-2",
                average_entry="50",
            )
            await _cash(db, portfolio_id, venue="kraken", asset="USD", free="1000")

            r = await _read(client, token, "/trading/reconciliation")

        rows = r.json()["venues"]
        assert {v["venue"] for v in rows} == {"binance", "kraken", "okx"}
        assert {v["status"] for v in rows} == {"never_run"}
        for row in rows:
            assert row["checked_at"] is None, row
            assert row["discrepancies"] == [], row

    async def test_a_venue_holding_only_cash_is_still_unreconciled(
        self, db, database_url
    ):
        """Cash parked at a venue with no open position is still a local figure
        nobody has checked against the venue's. A report walking positions alone
        would omit it, and an omitted venue reads as no venue at all."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(
                db,
                portfolio_id,
                venue="binance",
                symbol="BTC/USD",
                quantity="1",
                average_entry="100",
            )
            await _cash(db, portfolio_id, venue="kraken", asset="USD", free="1000")

            r = await _read(client, token, "/trading/reconciliation")

        assert _venue_row(r.json(), "kraken")["status"] == "never_run"

    async def test_the_raw_json_says_null_rather_than_a_timestamp_or_a_zero(
        self, db, database_url
    ):
        """`checked_at: 0` or `""` would parse as falsy and satisfy a client
        testing for absence, while reading as a moment to anything that formats
        it. Absent is null."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cash(db, portfolio_id, venue="binance", asset="USD", free="1000")

            r = await _read(client, token, "/trading/reconciliation")

        assert '"checked_at": null' in r.text or '"checked_at":null' in r.text
        row = _venue_row(json.loads(r.text), "binance")
        assert row["checked_at"] is None
        assert not isinstance(row["checked_at"], int | str)

    async def test_the_report_covers_this_book_and_not_another_accounts(
        self, db, database_url
    ):
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            other = await _second_user(client, token)
            mine = await _portfolio(db, user_id)
            theirs = await _portfolio(db, other)
            await _cash(db, mine, venue="binance", asset="USD", free="1000")
            await _cash(db, theirs, venue="deribit", asset="USD", free="1000")

            r = await _read(client, token, "/trading/reconciliation")

        assert {v["venue"] for v in r.json()["venues"]} == {"binance"}
        assert mine != theirs

    async def test_a_book_with_no_venues_reports_none_rather_than_inventing_one(
        self, db, database_url
    ):
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            await _portfolio(db, user_id)
            r = await _read(client, token, "/trading/reconciliation")

        assert r.status_code == 200, r.text
        assert r.json()["venues"] == []

    async def test_a_stored_pass_inside_its_freshness_bound_reads_as_reconciled(
        self, db, database_url
    ):
        """The only shape that earns `reconciled`: a check, at this venue,
        recently enough that the operator's own bound still covers it."""
        checked_at = datetime.now(UTC) - timedelta(minutes=5)
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cash(db, portfolio_id, venue="binance", asset="USD", free="1000")
            await _stale_after(
                db, portfolio_id, venue="binance", after=timedelta(hours=1)
            )
            await _reconciliation(
                db, portfolio_id, venue="binance", checked_at=checked_at
            )

            r = await _read(client, token, "/trading/reconciliation")

        row = _venue_row(r.json(), "binance")
        assert row["status"] == "reconciled"
        assert datetime.fromisoformat(row["checked_at"]) == checked_at
        assert row["discrepancies"] == []

    async def test_a_pass_older_than_its_freshness_bound_reads_as_stale(
        self, db, database_url
    ):
        """Two hours old against a one-hour bound. The books agreed once; they
        have not been shown to agree now, and the page must not say they do."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cash(db, portfolio_id, venue="binance", asset="USD", free="1000")
            await _stale_after(
                db, portfolio_id, venue="binance", after=timedelta(hours=1)
            )
            await _reconciliation(
                db,
                portfolio_id,
                venue="binance",
                checked_at=datetime.now(UTC) - timedelta(hours=2),
            )

            r = await _read(client, token, "/trading/reconciliation")

        row = _venue_row(r.json(), "binance")
        assert row["status"] == "stale"
        assert row["status"] != "reconciled"
        # It did run, so the moment it ran is still reported -- `stale` is a
        # statement about the age of a real check, not the absence of one.
        assert row["checked_at"] is not None

    async def test_a_pass_with_no_freshness_bound_configured_is_not_fresh(
        self, db, database_url
    ):
        """An unset threshold must not read as permission.

        The result is minutes old and clean. Nothing has stated how old a pass
        at this venue may be, so there is no bound it can be shown to be inside,
        and an endpoint that supplied one -- a default, a constant, a query
        parameter's fallback -- would be answering with a threshold nobody set.
        """
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cash(db, portfolio_id, venue="binance", asset="USD", free="1000")
            await _reconciliation(
                db,
                portfolio_id,
                venue="binance",
                checked_at=datetime.now(UTC) - timedelta(seconds=5),
            )

            r = await _read(client, token, "/trading/reconciliation")

        row = _venue_row(r.json(), "binance")
        assert row["status"] != "reconciled"
        assert row["status"] == "stale"

    async def test_a_deactivated_bound_is_not_a_bound(self, db, database_url):
        """Switching the alert off removes the statement about freshness; it
        does not leave the last one standing."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cash(db, portfolio_id, venue="binance", asset="USD", free="1000")
            await _stale_after(
                db,
                portfolio_id,
                venue="binance",
                after=timedelta(hours=1),
                active=False,
            )
            await _reconciliation(
                db,
                portfolio_id,
                venue="binance",
                checked_at=datetime.now(UTC) - timedelta(seconds=5),
            )

            r = await _read(client, token, "/trading/reconciliation")

        assert _venue_row(r.json(), "binance")["status"] == "stale"

    async def test_the_tighter_of_two_stated_bounds_is_the_one_that_binds(
        self, db, database_url
    ):
        """Two reconciliation alerts on one venue are two bounds the operator
        stated. The reading is outside the tighter one, so it is not current."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cash(db, portfolio_id, venue="binance", asset="USD", free="1000")
            await _stale_after(
                db, portfolio_id, venue="binance", after=timedelta(days=7)
            )
            await _stale_after(
                db, portfolio_id, venue="binance", after=timedelta(minutes=10)
            )
            await _reconciliation(
                db,
                portfolio_id,
                venue="binance",
                checked_at=datetime.now(UTC) - timedelta(hours=1),
            )

            r = await _read(client, token, "/trading/reconciliation")

        assert _venue_row(r.json(), "binance")["status"] == "stale"

    async def test_a_pass_at_one_venue_leaves_the_others_never_run(
        self, db, database_url
    ):
        """The substitution the enum exists for, in the setting where it is
        easiest to make: one venue genuinely did reconcile, and a report that
        derived the status once rather than per venue would carry that verdict
        across to a venue nobody has looked at."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cash(db, portfolio_id, venue="binance", asset="USD", free="1000")
            await _cash(db, portfolio_id, venue="kraken", asset="USD", free="1000")
            await _stale_after(
                db, portfolio_id, venue="binance", after=timedelta(hours=1)
            )
            await _stale_after(
                db, portfolio_id, venue="kraken", after=timedelta(hours=1)
            )
            await _reconciliation(
                db,
                portfolio_id,
                venue="binance",
                checked_at=datetime.now(UTC) - timedelta(minutes=5),
            )

            r = await _read(client, token, "/trading/reconciliation")

        body = r.json()
        assert _venue_row(body, "binance")["status"] == "reconciled"
        kraken = _venue_row(body, "kraken")
        assert kraken["status"] == "never_run"
        assert kraken["checked_at"] is None
        assert kraken["discrepancies"] == []

    async def test_a_divergence_reports_both_sides_and_leaves_absence_absent(
        self, db, database_url
    ):
        """`remote: 0` would assert the venue reported a flat position. It
        reported no position at all, which is why the field is nullable."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(
                db,
                portfolio_id,
                venue="okx",
                symbol="ETH/USD",
                quantity="2",
                average_entry="50",
            )
            await _stale_after(db, portfolio_id, venue="okx", after=timedelta(hours=1))
            await _reconciliation(
                db,
                portfolio_id,
                venue="okx",
                checked_at=datetime.now(UTC) - timedelta(minutes=1),
                discrepancies=(_missing_at_venue("okx", "ETH/USD", "2"),),
            )

            r = await _read(client, token, "/trading/reconciliation")

        row = _venue_row(json.loads(r.text), "okx")
        assert row["status"] == "diverged"
        assert len(row["discrepancies"]) == 1
        found = row["discrepancies"][0]
        assert found["kind"] == "position_missing_at_venue"
        assert found["venue"] == "okx"
        assert found["symbol"] == "ETH/USD"
        assert found["local"] == "2", "money and quantities are strings"
        assert found["remote"] is None
        assert '"remote": null' in r.text or '"remote":null' in r.text

    async def test_a_divergence_is_reported_however_old_it_is(
        self, db, database_url
    ):
        """An old disagreement is still the last thing known about the venue.
        Reporting it as `stale` would replace a statement about the books with a
        statement about the clock, and drop the discrepancies with it."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cash(db, portfolio_id, venue="okx", asset="USD", free="1000")
            await _stale_after(db, portfolio_id, venue="okx", after=timedelta(hours=1))
            await _reconciliation(
                db,
                portfolio_id,
                venue="okx",
                checked_at=datetime.now(UTC) - timedelta(days=3),
                discrepancies=(_missing_at_venue("okx", "ETH/USD", "2"),),
            )

            r = await _read(client, token, "/trading/reconciliation")

        row = _venue_row(r.json(), "okx")
        assert row["status"] == "diverged"
        assert len(row["discrepancies"]) == 1

    async def test_only_the_most_recent_result_per_venue_is_reported(
        self, db, database_url
    ):
        """A cleared divergence must not keep flagging, and a divergence found
        after a pass must not be hidden by it. Both directions are stored here,
        so a read that returned the older row is wrong whichever way it leans."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cash(db, portfolio_id, venue="binance", asset="USD", free="1000")
            await _cash(db, portfolio_id, venue="okx", asset="USD", free="1000")
            for venue in ("binance", "okx"):
                await _stale_after(
                    db, portfolio_id, venue=venue, after=timedelta(hours=1)
                )

            # binance: diverged first, then passed. okx: passed first, then
            # diverged. Written newest-first so insertion order cannot stand in
            # for recency.
            await _reconciliation(
                db,
                portfolio_id,
                venue="binance",
                checked_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            await _reconciliation(
                db,
                portfolio_id,
                venue="binance",
                checked_at=datetime.now(UTC) - timedelta(minutes=30),
                discrepancies=(_missing_at_venue("binance", "BTC/USD", "1"),),
            )
            await _reconciliation(
                db,
                portfolio_id,
                venue="okx",
                checked_at=datetime.now(UTC) - timedelta(minutes=1),
                discrepancies=(_missing_at_venue("okx", "ETH/USD", "2"),),
            )
            await _reconciliation(
                db,
                portfolio_id,
                venue="okx",
                checked_at=datetime.now(UTC) - timedelta(minutes=30),
            )

            r = await _read(client, token, "/trading/reconciliation")

        body = r.json()
        binance = _venue_row(body, "binance")
        assert binance["status"] == "reconciled"
        assert binance["discrepancies"] == []
        okx = _venue_row(body, "okx")
        assert okx["status"] == "diverged"
        assert len(okx["discrepancies"]) == 1

    async def test_a_checked_venue_the_book_no_longer_touches_still_appears(
        self, db, database_url
    ):
        """The venue holds a position we have no row for, so our book has no
        exposure there to derive the venue list from. Listing local exposure
        alone would drop the divergence precisely because our side is empty --
        which is the direction in which our book is most wrong."""
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _cash(db, portfolio_id, venue="binance", asset="USD", free="1000")
            await _reconciliation(
                db,
                portfolio_id,
                venue="kraken",
                checked_at=datetime.now(UTC) - timedelta(minutes=1),
                discrepancies=(
                    Discrepancy(
                        kind=Divergence.POSITION_MISSING_LOCALLY,
                        venue="kraken",
                        symbol="SOL/USD",
                        local=None,
                        remote=Decimal(40),
                        detail="kraken holds 40 SOL/USD and we have no position row",
                    ),
                ),
            )

            r = await _read(client, token, "/trading/reconciliation")

        body = r.json()
        assert {v["venue"] for v in body["venues"]} == {"binance", "kraken"}
        kraken = _venue_row(body, "kraken")
        assert kraken["status"] == "diverged"
        assert kraken["discrepancies"][0]["local"] is None
        assert kraken["discrepancies"][0]["remote"] == "40"

    async def test_another_accounts_result_is_not_reported_here(
        self, db, database_url
    ):
        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            other = await _second_user(client, token)
            mine = await _portfolio(db, user_id)
            theirs = await _portfolio(db, other)
            await _cash(db, mine, venue="binance", asset="USD", free="1000")
            await _cash(db, theirs, venue="binance", asset="USD", free="1000")
            await _stale_after(db, theirs, venue="binance", after=timedelta(hours=1))
            await _reconciliation(
                db,
                theirs,
                venue="binance",
                checked_at=datetime.now(UTC) - timedelta(minutes=1),
            )

            r = await _read(client, token, "/trading/reconciliation")

        assert _venue_row(r.json(), "binance")["status"] == "never_run"


class TestTheReadPathsWriteNothing:
    async def test_neither_endpoint_changes_what_it_describes(
        self, db, database_url
    ):
        """Reading a portfolio must not materialise one, and reading a
        reconciliation report must not record a reconciliation."""

        async def counts():
            row = await db.pool.fetchrow(
                """
                SELECT (SELECT count(*) FROM portfolio)              AS portfolios,
                       (SELECT count(*) FROM position)               AS positions,
                       (SELECT count(*) FROM cash_balance)           AS cash,
                       (SELECT count(*) FROM nav_snapshot)           AS navs,
                       (SELECT count(*) FROM trade_order)            AS orders,
                       (SELECT count(*) FROM reconciliation_result)  AS checks
                """
            )
            return dict(row)

        app = _app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, user_id = await _operator(client)
            portfolio_id = await _portfolio(db, user_id)
            await _hold(
                db,
                portfolio_id,
                venue="binance",
                symbol="BTC/USD",
                quantity="1",
                average_entry="100",
            )
            await _cash(db, portfolio_id, venue="binance", asset="USD", free="1000")
            await _reconciliation(
                db,
                portfolio_id,
                venue="binance",
                checked_at=datetime.now(UTC) - timedelta(minutes=1),
            )

            before = await counts()
            for path in ("/trading/portfolio", "/trading/reconciliation"):
                # Twice: an endpoint writing on first read only would still pass
                # a single-call comparison taken after the write.
                for _ in range(2):
                    r = await _read(client, token, path)
                    assert r.status_code == 200, r.text
            after = await counts()

        assert after == before
        assert before["positions"] > 0, "the comparison must not be over zero rows"
        # The read must not record a check of its own, and must not stamp the
        # stored one as seen.
        assert before["checks"] > 0
