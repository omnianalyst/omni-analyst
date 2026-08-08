"""Calibration backfill for crypto producers.

Each test states what bug it catches. The headline (point-in-time) plants a
claim after the cutoff that would flip the direction and asserts the producer
did not see it -- a backfill that lets a producer read the bar it is predicting
manufactures a hit rate near 1.0 and opens every gate on nothing.

The dedicated TEST_DATABASE_URL (``omni_v2_agent_cbf``) keeps this suite off
the shared test database: concurrent agents TRUNCATE it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.autonomous.crypto_backfill import (
    CutoffOverlapError,
    backfill_crypto_predictions,
)
from omni.trading import policy

WINDOW = 50
HORIZON_DAYS = 5
N_PRICES = 60


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _entity(db, symbol="BTC"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('crypto_asset',$1,$1) RETURNING id",
        symbol,
    )


async def _price(db, entity_id, close, event_date, *, knowledge_date=None):
    if knowledge_date is None:
        knowledge_date = event_date
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'price_snapshot','close',$2::jsonb,'test',$3,$4,1.0,'allowed')",
        entity_id,
        json.dumps({"close": close}),
        event_date,
        knowledge_date,
    )


async def _series(db, entity_id, *, end_at, n, base, step):
    """Plant ``n`` daily closes ending at ``end_at``. A zigzag (alternating
    +/- offset) guarantees nonzero realized vol so the trend producer does not
    abstain on a flat series."""
    for i in range(n):
        close = base + i * step + (3.0 if i % 2 else -3.0)
        await _price(db, entity_id, close, end_at - timedelta(days=n - 1 - i))


async def _run(db, entity_id, *, cutoffs, method="trend.sma"):
    return await backfill_crypto_predictions(
        db.pool,
        method=method,
        entity_ids=[entity_id],
        cutoffs=cutoffs,
        horizon_days=HORIZON_DAYS,
        audience_user_id=None,
        run_id=str(uuid4()),
    )


async def _directions(db, entity_id):
    rows = await db.pool.fetch(
        "SELECT direction FROM prediction WHERE entity_id = $1 "
        "ORDER BY created_at",
        entity_id,
    )
    return [r["direction"] for r in rows]


async def _outcomes(db, entity_id):
    rows = await db.pool.fetch(
        "SELECT outcome FROM prediction WHERE entity_id = $1 "
        "ORDER BY created_at",
        entity_id,
    )
    return [r["outcome"] for r in rows]


class TestPointInTime:
    async def test_producer_cannot_see_a_claim_knowable_after_the_cutoff(self, db):
        """HEADLINE. A backfill that lets the producer read the bar it is
        predicting manufactures a hit rate near 1.0 and opens every gate on
        nothing.

        Pre-cutoff coverage implies direction ``down`` (entry well below SMA).
        A claim filed one day AFTER the cutoff has close=10000 -- if the
        producer could see it, that claim would become the entry and flip the
        direction to ``up``. The producer filters ``knowledge_date <= as_of``
        inside its own reader, so the post-cutoff claim is invisible and the
        direction stays ``down``.
        """
        e = await _entity(db)
        cutoff = datetime.now(UTC) - timedelta(days=30)
        await _series(db, e, end_at=cutoff - timedelta(days=1), n=N_PRICES,
                      base=200.0, step=-2.0)

        await _price(db, e, 10000.0, cutoff + timedelta(days=1))

        report = await _run(db, e, cutoffs=[cutoff])
        assert report.generated == 1
        directions = await _directions(db, e)
        assert directions == ["down"], (
            "producer must not have seen the post-cutoff claim; "
            "'up' means it peeked at the bar it was predicting"
        )


class TestBackfillMarker:
    async def test_every_prediction_carries_the_marker_and_policy_excludes_it(self, db):
        """Every generated prediction carries ``provenance.assumptions.backfill``
        (verified by reading provenance back) and is classified as backfilled
        by ``policy._NOT_BACKFILLED`` -- the exact predicate GATE C reads.

        A row committed without its marker reads as live forever and opens the
        scale gate on replayed history. Both checks must hold for every row.
        """
        e = await _entity(db)
        now = datetime.now(UTC)
        cutoffs = [now - timedelta(days=d) for d in (30, 20, 10)]
        run_id = str(uuid4())
        await _series(db, e, end_at=cutoffs[0], n=N_PRICES,
                      base=200.0, step=-2.0)

        report = await backfill_crypto_predictions(
            db.pool,
            method="trend.sma",
            entity_ids=[e],
            cutoffs=cutoffs,
            horizon_days=HORIZON_DAYS,
            audience_user_id=None,
            run_id=run_id,
        )
        assert report.generated == 3

        rows = await db.pool.fetch(
            "SELECT id, provenance FROM prediction WHERE entity_id = $1",
            e,
        )
        assert len(rows) == 3

        for row in rows:
            prov = row["provenance"]
            prov = json.loads(prov) if isinstance(prov, (str, bytes)) else prov
            marker = prov["assumptions"]["backfill"]
            assert marker["run_id"] == run_id
            datetime.fromisoformat(marker["cutoff"])
            datetime.fromisoformat(marker["as_of"])

        ids = [row["id"] for row in rows]
        live_count = await db.pool.fetchval(
            f"SELECT count(*) FROM prediction p "
            f"WHERE p.id = ANY($1::uuid[]) AND {policy._NOT_BACKFILLED}",
            ids,
        )
        assert live_count == 0, (
            "policy._NOT_BACKFILLED must exclude every backfilled row; "
            f"{live_count} of {len(ids)} read as live"
        )


class TestCutoffSpacing:
    async def test_cutoffs_closer_than_the_horizon_raise(self, db):
        """Overlapping horizons reuse the same price path across predictions,
        so the outcomes are correlated and the effective sample is smaller than
        the count. Closer-than-horizon spacing must raise, not silently inflate
        n."""
        e = await _entity(db)
        now = datetime.now(UTC)
        close_cutoffs = [now - timedelta(days=20), now - timedelta(days=18)]

        with pytest.raises(CutoffOverlapError, match="at least"):
            await backfill_crypto_predictions(
                db.pool,
                method="trend.sma",
                entity_ids=[e],
                cutoffs=close_cutoffs,
                horizon_days=HORIZON_DAYS,
                audience_user_id=None,
                run_id=str(uuid4()),
            )

    async def test_well_spaced_cutoffs_do_not_raise(self, db):
        e = await _entity(db)
        now = datetime.now(UTC)
        spaced = [now - timedelta(days=30), now - timedelta(days=20)]
        await _series(db, e, end_at=spaced[0], n=N_PRICES,
                      base=200.0, step=-2.0)

        report = await _run(db, e, cutoffs=spaced)
        assert report.generated == 2


class TestUnresolvable:
    async def test_no_subsequent_price_coverage_stays_pending(self, db):
        """A prediction whose horizon elapsed but whose barriers were never
        touched against observed prices stays ``pending`` -- not ``expiry``.
        ``expiry`` fabricates evidence (``price stayed within barriers``);
        ``pending`` is honest (``cannot score``). The report must count it as
        ``unresolvable``, never as ``resolved``."""
        e = await _entity(db)
        cutoff = datetime.now(UTC) - timedelta(days=30)
        await _series(db, e, end_at=cutoff - timedelta(days=1), n=N_PRICES,
                      base=200.0, step=-2.0)

        report = await _run(db, e, cutoffs=[cutoff])
        assert report.generated == 1
        assert report.resolved == 0
        assert report.unresolvable == 1

        outcomes = await _outcomes(db, e)
        assert outcomes == ["pending"], (
            "no price coverage after the cutoff -> pending, never expiry"
        )


class TestResolution:
    async def test_resolved_outcomes_match_the_real_price_path(self, db):
        """The resolver scores against the observed price path, never a
        synthesized one. A rising series gives direction ``up``; planting a
        post-cutoff price above the upper barrier must yield outcome
        ``upper``."""
        e = await _entity(db)
        cutoff = datetime.now(UTC) - timedelta(days=30)
        await _series(db, e, end_at=cutoff - timedelta(days=1), n=N_PRICES,
                      base=100.0, step=2.0)

        await _price(db, e, 100000.0, cutoff + timedelta(days=1))

        report = await _run(db, e, cutoffs=[cutoff])
        assert report.generated == 1
        assert report.resolved == 1

        barrier, direction, outcome = await db.pool.fetchrow(
            "SELECT upper_barrier, direction, outcome "
            "FROM prediction WHERE entity_id = $1",
            e,
        )
        assert direction == "up"
        assert outcome == "upper"
        assert 100000.0 > float(barrier), (
            "self-check: the planted price must actually exceed the upper barrier"
        )


class TestReportCounts:
    async def test_generated_resolved_and_abstained_are_counted_separately(self, db):
        """The report distinguishes a prediction the producer wrote (generated),
        one the producer declined to write (abstained), and one that resolved
        against a real price path (resolved). Pooling any two hides a defect.

        Scenario: two non-overlapping cutoffs. At the early cutoff the producer
        sees no coverage and abstains; at the late cutoff it sees enough to
        generate, and a planted post-cutoff price resolves the outcome.
        """
        e = await _entity(db)
        now = datetime.now(UTC)
        cutoff_late = now - timedelta(days=30)
        cutoff_early = cutoff_late - timedelta(days=60)

        await _series(
            db, e,
            end_at=cutoff_late - timedelta(days=1),
            n=N_PRICES,
            base=200.0,
            step=-2.0,
        )
        await _price(db, e, 100000.0, cutoff_late + timedelta(days=1))

        report = await _run(db, e, cutoffs=[cutoff_early, cutoff_late])

        assert report.abstained == 1, "early cutoff has no visible coverage"
        assert report.generated == 1, "late cutoff has full coverage"
        assert report.resolved == 1, "planted price path resolves the outcome"
        assert report.unresolvable == 0
