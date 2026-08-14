"""HTTP read API over the findings pipeline.

What these tests defend:

* a surfaced finding appears with the threshold it cleared and both evidence
  lists -- disconfirming evidence is not optional in the response;
* a finding private to one user never reaches another user's feed or the shared
  feed (the redistribution rule, reaching the thing that speaks unprompted);
* refused findings never appear in ``/briefing``;
* a method below the ten-resolution sample floor reports ``hit_rate: null``,
  not a number;
* ``/briefing/refusals`` counts by reason;
* an empty feed returns ``[]`` with 200 -- silence is a real answer, not a
  placeholder.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
from neutron.test import TestClient

from omni.api.briefing import build_router
from omni.conviction.gate import Calibration, Candidate, Refusal, assess
from omni.conviction.publish import record
from omni.main import create_app

NOW = datetime(2026, 7, 28, tzinfo=UTC)

def _auth(user_id):
    """A real bearer token. These tests used to pass X-User-Id, which the API
    trusted; identity is now verified, so a leak test that names a user in a
    header would prove nothing about the rule it is checking."""
    import os

    from neutron.auth.jwt import create_token

    os.environ.setdefault("OMNI_JWT_SECRET", "t" * 48)
    token = create_token({"sub": str(user_id)}, os.environ["OMNI_JWT_SECRET"])
    return {"Authorization": f"Bearer {token}"}


async def _user(db) -> uuid4:
    """A real principal. The auth middleware checks the token subject against
    the users table, so a minted token for an id with no row is anonymous."""
    uid = uuid4()
    return await db.pool.fetchval(
        "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'x') "
        "RETURNING id",
        uid, f"{uid}@example.com",
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


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company',$1,$1) "
        "RETURNING id",
        symbol,
    )


async def _claim(db, entity_id, *, owner=None):
    shared = owner is None
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1,'manipulation_signal','sig','{}'::jsonb,$2,$3,$4,0.9,$5,$6)
        RETURNING id
        """,
        entity_id, "internal" if shared else "polygon",
        NOW - timedelta(days=1), NOW,
        "allowed" if shared else "byo_only", owner,
    )


async def _prediction(db, entity_id, *, direction="up", outcome=None):
    pid = await db.pool.fetchval(
        """
        INSERT INTO prediction (entity_id, method, direction, confidence,
                                entry_price, upper_barrier, lower_barrier,
                                horizon_ends_at, provenance)
        VALUES ($1,'detect',$2,0.85,100,110,90,$3,'{}'::jsonb) RETURNING id
        """,
        entity_id, direction, NOW + timedelta(days=5),
    )
    if outcome:
        await db.pool.execute(
            "UPDATE prediction SET outcome=$1::prediction_outcome, "
            "resolved_at=now() WHERE id=$2",
            outcome, pid,
        )
    return pid


def _bucket(low, n, hits):
    return Calibration("manipulation_signal", "detect", low,
                       round(low + 0.1, 2), n, hits)


def _candidate(claim_id, confidence=0.85, **kw):
    kw.setdefault("searched_for_disconfirming", True)
    kw.setdefault("falsifiable", True)
    return Candidate(claim_id=claim_id, claim_type="manipulation_signal",
                     method="detect", confidence=confidence, **kw)


async def _surface(db, *, entity_id, claim_id, owner=None, outcome=None,
                   direction="up", supporting=("volume z=4.2",),
                   disconfirming=("earnings due",)):
    """Record a surfaced finding through the real gate + publisher.

    Going through assess()/record() rather than INSERTing a finding by hand
    means the row carries the threshold and calibrated_hit_rate the gate
    actually produced -- which is what the response is meant to expose. Two
    buckets are used so the candidate's confidence (0.85) falls inside an
    observed one; a single bucket would honestly report no rate at this
    confidence, which is a different test.
    """
    p = await _prediction(db, entity_id, direction=direction, outcome=outcome)
    verdict = assess(
        _candidate(claim_id, supporting=supporting,
                   disconfirming=disconfirming),
        [_bucket(0.7, 40, 34), _bucket(0.8, 40, 36)],
    )
    assert verdict.surfaced, verdict.detail
    await record(db.pool, verdict, entity_id=entity_id,
                 audience_user_id=owner, prediction_id=p)


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def test_a_surfaced_finding_carries_its_threshold_and_both_evidence_lists(
    db, database_url
):
    e = await _entity(db)
    c = await _claim(db, e)
    await _surface(
        db, entity_id=e, claim_id=c,
        supporting=("volume z=4.2",),
        disconfirming=("earnings due tomorrow",),
    )

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/briefing")

    assert r.status_code == 200, r.text
    feed = r.json()
    assert len(feed) == 1
    item = feed[0]
    assert item["method"] == "detect"
    assert item["confidence"] == pytest.approx(0.85)
    # The threshold it cleared, recorded at decision time.
    assert item["threshold"] == pytest.approx(0.7)
    # Calibrated hit rate where known: 0.85 falls in the [0.8, 0.9) bucket,
    # which has historically resolved 36/40.
    assert item["calibrated_hit_rate"] == pytest.approx(36 / 40)
    # Both evidence lists travel with the finding. Disconfirming is not
    # optional -- a finding without it reads as advocacy.
    assert item["supporting"] == ["volume z=4.2"]
    assert item["disconfirming"] == ["earnings due tomorrow"]
    # Entity context so a client does not need a second round trip.
    assert item["entity"]["symbol"] == "AAPL"
    assert item["entity_id"] == item["entity"]["id"]
    assert item["prediction_id"] is not None


async def test_a_claimless_finding_reports_claim_id_null_not_the_string_None(
    db, database_url
):
    """claim_id has been nullable since 012. An unguarded str() renders NULL as
    the literal "None", which a client cannot distinguish from a real id -- and
    every finding the trend producer writes is claim-less, so this was the
    shipped shape of 100% of the feed."""
    e = await _entity(db)
    await _surface(db, entity_id=e, claim_id=None)

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/briefing")

    assert r.status_code == 200, r.text
    feed = r.json()
    assert len(feed) == 1
    assert feed[0]["claim_id"] is None
    assert feed[0]["claim_id"] != "None"


async def test_the_feed_shows_one_current_call_per_entity_and_method(
    db, database_url
):
    """The finding table is an append-only ledger; the feed is a statement of
    the current view. Two passes as price crosses the moving average write
    opposite directions on the same name -- rendering both makes the product
    contradict itself on one screen. Newest wins; the older row stays on the
    ledger."""
    e = await _entity(db)
    await _surface(db, entity_id=e, claim_id=None, direction="down")
    await _surface(db, entity_id=e, claim_id=None, direction="up")

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/briefing")

    feed = r.json()
    assert len(feed) == 1, [f["direction"] for f in feed]
    assert feed[0]["direction"] == "up"
    # Both rows are still on the ledger -- superseding the feed must not delete
    # history, or the refusal denominator and the scorecard stop adding up.
    assert await db.pool.fetchval(
        "SELECT count(*) FROM finding WHERE status = 'surfaced'"
    ) == 2


async def test_a_finding_reports_whether_its_counter_case_was_searched(
    db, database_url
):
    """The ambiguity migration 032 exists to remove. A finding written before
    the disconfirming search has `disconfirming = []`, and so does one where the
    search ran and found nothing -- the rows are identical. The card renders the
    second as "the checks ran and found none", which is a lie about the first.

    The flag is recorded at write time from what the gate was actually told, so
    the two are distinguishable without deleting either.
    """
    e = await _entity(db)
    await _surface(db, entity_id=e, claim_id=None, disconfirming=())

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        feed = (await client.get("/briefing")).json()

    assert feed[0]["disconfirming"] == []
    assert feed[0]["evidence_searched"] is True


async def test_a_legacy_finding_reports_that_nothing_was_searched(db, database_url):
    """Rows predating the search default to false, which is exactly correct for
    them, and they are NOT deleted -- there are ~50k in production carrying the
    resolved outcomes behind the published hit rate."""
    e = await _entity(db)
    p = await _prediction(db, e)
    await db.pool.execute(
        "INSERT INTO finding (claim_id, entity_id, status, method, confidence, "
        "threshold, prediction_id, supporting, disconfirming) "
        "VALUES (NULL,$1,'surfaced','trend.sma',0.85,0.7,$2,"
        "'[\"up directional call from trend.sma\"]'::jsonb,'[]'::jsonb)",
        e, p,
    )

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        feed = (await client.get("/briefing")).json()

    assert len(feed) == 1, "the legacy row is kept, not deleted"
    assert feed[0]["evidence_searched"] is False


async def test_a_searched_finding_must_report_what_it_found(db, database_url):
    """Claiming a completed search while naming nothing on either side is the
    original defect wearing a new column. The constraint refuses it."""
    e = await _entity(db)
    p = await _prediction(db, e)
    with pytest.raises(asyncpg.IntegrityConstraintViolationError) as exc:
        await db.pool.execute(
            "INSERT INTO finding (claim_id, entity_id, status, method, confidence, "
            "threshold, prediction_id, supporting, disconfirming, evidence_searched) "
            "VALUES (NULL,$1,'surfaced','trend.sma',0.85,0.7,$2,"
            "'[]'::jsonb,'[]'::jsonb,true)",
            e, p,
        )
    assert exc.value.constraint_name == "searched_findings_report_what_they_found"


async def test_a_resolved_call_leaves_the_feed_but_stays_on_the_scorecard(
    db, database_url
):
    """The feed is what the system says now. A call whose prediction has played
    out is what it said, and belongs to the record.

    Nothing else retires it: a resolved finding can only be displaced by a newer
    surfaced row for the same key, so if the next pass refuses it would stand as
    "currently standing" forever.
    """
    e = await _entity(db)
    await _surface(db, entity_id=e, claim_id=None, outcome="upper")
    viewer = await _user(db)

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        feed = (await client.get("/briefing")).json()
        card = (await client.get("/briefing/scorecard", headers=_auth(viewer))).json()

    assert feed == []
    # Still counted, and counted as a hit -- leaving the feed is not forgetting.
    assert card[0]["resolved"] == 1
    assert card[0]["hits"] == 1


async def test_a_resolved_call_does_not_hide_a_live_one_behind_it(db, database_url):
    """Filtering after the dedup instead of inside it would pick the resolved
    row as newest for its key and then drop it, silently hiding the open call
    underneath. Order matters, so it is asserted."""
    e = await _entity(db)
    await _surface(db, entity_id=e, claim_id=None, direction="up")  # open, older
    await _surface(db, entity_id=e, claim_id=None, outcome="upper")  # resolved, newer

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        feed = (await client.get("/briefing")).json()

    assert len(feed) == 1
    assert feed[0]["direction"] == "up"


async def test_a_private_finding_leaks_to_neither_another_user_nor_the_shared_feed(
    db, database_url
):
    """The leak test. A finding from A's licensed data must not reach B or anon.

    Serving one user's BYO-sourced finding to another makes this deployment the
    redistributor, which is exactly what the provider terms forbid. The gate is
    the most dangerous place for this rule to slip, because it speaks unprompted.
    """
    e = await _entity(db)
    owner_a = await _user(db)
    owner_b = await _user(db)
    c = await _claim(db, e, owner=owner_a)
    await _surface(db, entity_id=e, claim_id=c, owner=owner_a)

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r_a = await client.get(
            "/briefing", headers=_auth(owner_a)
        )
        r_b = await client.get(
            "/briefing", headers=_auth(owner_b)
        )
        r_shared = await client.get("/briefing")

    assert r_a.status_code == 200
    # A sees its own finding.
    assert len(r_a.json()) == 1
    # B does not.
    assert r_b.json() == []
    # Neither does the shared feed.
    assert r_shared.json() == []


async def test_refused_findings_never_appear_in_the_briefing(db, database_url):
    """Refusals are the denominator, not part of the feed. The feed is what the
    system chose to say; a refused finding is, by construction, what it did not."""
    e = await _entity(db)
    c = await _claim(db, e)
    # One surfaced, two refused for different reasons.
    await _surface(db, entity_id=e, claim_id=c)
    await record(
        db.pool, assess(_candidate(c), []), entity_id=e
    )  # UNCALIBRATED
    await record(
        db.pool,
        assess(_candidate(c, falsifiable=False), [_bucket(0.7, 40, 34)]),
        entity_id=e,
    )  # NOT_FALSIFIABLE

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/briefing")

    assert r.status_code == 200
    feed = r.json()
    assert len(feed) == 1
    assert feed[0]["method"] == "detect"


async def test_a_method_below_the_sample_floor_reports_hit_rate_null(
    db, database_url
):
    """A hit rate from three resolved predictions is noise wearing a percentage
    sign. The floor is applied in publish.scorecard; this layer must carry the
    null through rather than defaulting to 0."""
    e = await _entity(db)
    c = await _claim(db, e)
    for _ in range(3):
        await _surface(db, entity_id=e, claim_id=c, outcome="upper")
    viewer = await _user(db)

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/briefing/scorecard", headers=_auth(viewer))

    assert r.status_code == 200, r.text
    card = r.json()[0]
    assert card["method"] == "detect"
    assert card["surfaced"] == 3
    assert card["resolved"] == 3
    assert card["hits"] == 3
    # Null, not zero, not 1.0.
    assert card["hit_rate"] is None


async def test_refusals_are_counted_by_reason(db, database_url):
    e = await _entity(db)
    c = await _claim(db, e)
    viewer = await _user(db)
    await record(db.pool, assess(_candidate(c), []), entity_id=e)
    await record(
        db.pool,
        assess(_candidate(c, falsifiable=False), [_bucket(0.7, 40, 34)]),
        entity_id=e,
    )

    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/briefing/refusals", headers=_auth(viewer))

    assert r.status_code == 200, r.text
    counts = r.json()
    assert counts[Refusal.UNCALIBRATED.value] == 1
    assert counts[Refusal.NOT_FALSIFIABLE.value] == 1


async def test_an_empty_feed_returns_an_empty_list_not_a_placeholder(
    db, database_url
):
    """Silence is a real answer. A 'no findings' object would dress up the gate's
    silence as content, which is exactly the failure the gate exists to prevent."""
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r = await client.get("/briefing")

    assert r.status_code == 200
    assert r.json() == []


async def test_scorecard_and_refusals_refuse_anonymous_callers(db, database_url):
    """The scorecard and refusal mix are operator intelligence derived from
    BYO-sourced findings, not public stats. An anonymous caller on the public
    domain must not receive them."""
    app = _make_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        r_card = await client.get("/briefing/scorecard")
        r_ref = await client.get("/briefing/refusals")

    assert r_card.status_code == 401
    assert r_ref.status_code == 401
