"""Slice 0's acceptance oracle: does the loop actually close?

demand in -> gap detected -> capability fills -> claim lands with provenance
-> coverage measurably improves -> the gap closes.

Everything else in this repo is in service of this test passing honestly.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.coverage.gaps import detect_gaps, persist_gaps
from omni.coverage.visibility import visible_claims
from omni.demand.ledger import direct_attention
from omni.fill.pipeline import claim_next_gap, drain, run_once
from omni.ingest.fred import FredAdapter
from omni.ingest.protocol import ClaimDraft, Unavailable

NOW = datetime(2026, 7, 27, tzinfo=UTC)

GDP_VINTAGES = [
    {"date": "2007-10-01", "realtime_start": "2008-01-30", "value": "0.6"},
    {"date": "2007-10-01", "realtime_start": "2008-03-27", "value": "-0.2"},
]


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company',$1,$1) RETURNING id",
        symbol,
    )


def _fred_registry():
    async def fake_fetch(series_id: str) -> list[dict]:
        return GDP_VINTAGES

    return _fred_capability(fake_fetch)

def _fred_capability(fetch_fn):
    """One real capability, built the way the scheduler builds them."""
    r = Registry()
    r.add(Capability(
        name="fred.series", description="FRED series",
        produces=("macro_series_point",), provider_key="fred", source="fred",
        touches_byo=False, maturity=Maturity.WIRED,
        callability=Callability.YES, call=FredAdapter(fetch_fn=fetch_fn).fetch,
    ))
    return r



@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, demand CASCADE")
    yield


class TestTheLoop:
    async def test_the_loop_closes_end_to_end(self, db):
        entity_id = await _entity(db)

        # Nothing is known yet.
        assert await visible_claims(db.pool, audience=None) == []

        # 1. Demand: someone directs attention at this fact.
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )

        # 2. The gap engine notices there is no coverage for it.
        gaps = await detect_gaps(db.pool)
        missing = [g for g in gaps if g["gap_class"] == "missing"]
        assert missing, "demand with no coverage should produce a missing gap"
        await persist_gaps(db.pool, gaps)

        # 3. A worker leases the gap and a capability fills it.
        result = await run_once(
            db.pool, registry=_fred_registry(), worker_id="w1"
        )
        assert result is not None
        assert result.outcome == "filled", result.reason
        assert result.capability == "fred.series"

        # 4. Claims landed, carrying provenance and both time axes.
        claims = await visible_claims(db.pool, audience=None)
        assert len(claims) == 2, "both ALFRED vintages should be stored"
        for claim in claims:
            assert claim["source"] == "fred"
            assert claim["event_date"] is not None
            assert claim["knowledge_date"] is not None
            assert claim["redistributable"] == "allowed"

        # 5. The revision is a second claim, not an overwrite of the first.
        assert len({c["knowledge_date"] for c in claims}) == 2
        assert len({c["event_date"] for c in claims}) == 1

        # 6. The gap is closed and the attempt is on the record.
        row = await db.pool.fetchrow(
            "SELECT resolved_at, lease_owner FROM gap WHERE id = $1", result.gap_id
        )
        assert row["resolved_at"] is not None
        assert row["lease_owner"] is None

        attempt = await db.pool.fetchrow(
            "SELECT capability, outcome, claim_id FROM fill_attempt WHERE gap_id = $1",
            result.gap_id,
        )
        assert attempt["outcome"] == "filled"
        assert attempt["claim_id"] is not None

        # 7. Coverage improved: the same demand no longer reports missing.
        after = await detect_gaps(db.pool)
        assert "missing" not in {g["gap_class"] for g in after}


class TestHonestRefusal:
    async def test_an_unavailable_source_records_why_and_writes_no_claim(self, db):
        """The failure mode that keeps fabricated coverage out of the store."""
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )
        await persist_gaps(db.pool, await detect_gaps(db.pool))

        async def broken(series_id: str) -> list[dict]:
            raise Unavailable("ALFRED returned HTTP 429")

        registry = _fred_capability(broken)

        result = await run_once(db.pool, registry=registry, worker_id="w1")
        assert result.outcome == "unfillable"
        assert "429" in result.reason

        assert await visible_claims(db.pool, audience=None) == []
        attempt = await db.pool.fetchrow(
            "SELECT outcome, reason, claim_id FROM fill_attempt"
        )
        assert attempt["outcome"] == "unfillable"
        assert attempt["claim_id"] is None
        assert attempt["reason"]

    async def test_an_unfilled_gap_stays_open_for_a_later_attempt(self, db):
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )
        await persist_gaps(db.pool, await detect_gaps(db.pool))

        async def broken(series_id: str) -> list[dict]:
            raise Unavailable("temporary outage")

        registry = _fred_capability(broken)
        await run_once(db.pool, registry=registry, worker_id="w1")

        row = await db.pool.fetchrow("SELECT resolved_at, lease_owner FROM gap")
        assert row["resolved_at"] is None, "a transient failure must not close the gap"
        assert row["lease_owner"] is None, "the lease must be released for a retry"

    async def test_no_registered_capability_is_recorded_not_crashed(self, db):
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="news_event", key="x"
        )
        await persist_gaps(db.pool, await detect_gaps(db.pool))

        result = await run_once(
            db.pool, registry=Registry(), worker_id="w1"
        )
        assert result.outcome == "unfillable"
        assert "no capability registered" in result.reason

    async def test_a_source_with_nothing_to_say_closes_the_gap(self, db):
        """Distinct from a failure: the source answered, the answer was empty."""
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )
        await persist_gaps(db.pool, await detect_gaps(db.pool))

        async def empty(series_id: str) -> list[dict]:
            return []

        registry = _fred_capability(empty)
        result = await run_once(db.pool, registry=registry, worker_id="w1")
        assert result.outcome == "unfillable"
        assert await db.pool.fetchval("SELECT resolved_at FROM gap") is not None


class TestIdempotentReingest:
    async def test_a_fill_that_wrote_no_new_claim_is_not_recorded_filled(self, db):
        """A source that returns only data already held produces an empty
        claim_ids. That is not a fill, and recording it as one both overstates
        coverage and -- via filled_attempt_produces_a_claim -- kills the worker.
        Distinct from a failure: the source answered, correctly."""
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )
        await persist_gaps(db.pool, await detect_gaps(db.pool))

        registry = _fred_registry()
        first = await run_once(db.pool, registry=registry, worker_id="w1")
        assert first.outcome == "filled"
        assert len(first.claim_ids) == 2

        # Re-open an identical gap against the now-covered target. The source
        # will answer again, but every draft is already held, so write_claims
        # returns no ids -- the defect's trigger.
        await db.pool.execute(
            "INSERT INTO gap (entity_id, claim_type, key, gap_class, "
            "audience_user_id, score) "
            "VALUES ($1, 'macro_series_point', 'GDP', 'missing', NULL, 1.0)",
            entity_id,
        )

        # Must not raise, and must not be recorded as 'filled'.
        result = await run_once(db.pool, registry=registry, worker_id="w1")
        assert result is not None
        assert result.outcome != "filled", result.reason

        attempt = await db.pool.fetchrow(
            "SELECT outcome, claim_id, reason FROM fill_attempt WHERE gap_id = $1",
            result.gap_id,
        )
        assert attempt["outcome"] != "filled"
        assert attempt["claim_id"] is None
        assert attempt["reason"], "nothing-new must carry a reason distinct from failure"
        assert "already held" in attempt["reason"]

        # The loop completes the cycle rather than dying: the queue drains.
        assert await drain(db.pool, registry=registry, worker_id="w1") == []


class TestLeasing:
    async def test_two_workers_do_not_take_the_same_gap(self, db):
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )
        await persist_gaps(db.pool, await detect_gaps(db.pool))

        first = await claim_next_gap(db.pool, worker_id="w1")
        second = await claim_next_gap(db.pool, worker_id="w2")
        assert first is not None
        assert second is None, "a leased gap must not be handed to a second worker"

    async def test_an_expired_lease_is_reclaimed(self, db):
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )
        await persist_gaps(db.pool, await detect_gaps(db.pool))

        await claim_next_gap(db.pool, worker_id="dead", lease_seconds=1)
        await db.pool.execute(
            "UPDATE gap SET lease_expires_at = now() - interval '1 second'"
        )
        reclaimed = await claim_next_gap(db.pool, worker_id="alive")
        assert reclaimed is not None
        assert await db.pool.fetchval("SELECT lease_owner FROM gap") == "alive"

    async def test_the_highest_scoring_gap_is_worked_first(self, db):
        a = await _entity(db, "AAA")
        b = await _entity(db, "BBB")
        await direct_attention(
            db.pool, entity_id=a, claim_type="macro_series_point", key="low",
            weight=1.0,
        )
        await direct_attention(
            db.pool, entity_id=b, claim_type="macro_series_point", key="high",
            weight=50.0,
        )
        await persist_gaps(db.pool, await detect_gaps(db.pool))

        leased = await claim_next_gap(db.pool, worker_id="w1")
        assert leased["entity_id"] == b

    async def test_drain_empties_the_queue_and_stops(self, db):
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )
        await persist_gaps(db.pool, await detect_gaps(db.pool))

        results = await drain(db.pool, registry=_fred_registry(), worker_id="w1")
        assert results
        assert all(r.outcome in ("filled", "unfillable") for r in results)
        assert await run_once(db.pool, registry=_fred_registry(), worker_id="w1") is None


class TestLicenceThroughTheLoop:
    async def test_a_byo_fill_stays_private_to_the_requester(self, db):
        """The licence rule surviving a full pass through the pipeline."""
        entity_id = await _entity(db)
        owner = uuid4()
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="price_snapshot",
            key="AAPL", requested_by=owner,
        )
        await persist_gaps(db.pool, await detect_gaps(db.pool))

        async def prices(symbol: str) -> list[ClaimDraft]:
            return [
                ClaimDraft(
                    claim_type="price_snapshot", key=symbol,
                    value={"close": 178.4},
                    event_date=NOW - timedelta(days=1), knowledge_date=NOW,
                    confidence=1.0,
                )
            ]

        from omni.capability.registry import Callability, Capability, Maturity, Registry
        registry = Registry()
        registry.add(Capability(
            name="polygon.aggregates", description="prices",
            produces=("price_snapshot",), provider_key="polygon", source="polygon",
            touches_byo=True, maturity=Maturity.WIRED,
            callability=Callability.YES, call=prices,
        ))

        result = await run_once(db.pool, registry=registry, worker_id="w1")
        assert result.outcome == "filled", result.reason

        assert await visible_claims(db.pool, audience=owner) != []
        assert await visible_claims(db.pool, audience=uuid4()) == []
        assert await visible_claims(db.pool, audience=None) == []


class TestRetryBackoff:
    """Found by running the scheduler: 3 gaps produced 14,900 attempts in 20
    seconds, because a transiently-failed gap was re-leased immediately."""

    async def _seed(self, db):
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )
        await persist_gaps(db.pool, await detect_gaps(db.pool))

    async def _broken_registry(self):
        async def broken(series_id):
            raise Unavailable("source down")
        return _fred_capability(broken)

    async def test_a_failed_gap_is_not_immediately_reclaimable(self, db):
        await self._seed(db)
        registry = await self._broken_registry()
        assert await run_once(db.pool, registry=registry, worker_id="w1") is not None
        # The hot loop: without backoff this returns the same gap instantly.
        assert await run_once(db.pool, registry=registry, worker_id="w1") is None

    async def test_the_backoff_window_is_recorded_and_grows(self, db):
        await self._seed(db)
        registry = await self._broken_registry()
        await run_once(db.pool, registry=registry, worker_id="w1")
        first = await db.pool.fetchrow(
            "SELECT attempts, next_attempt_at FROM gap"
        )
        assert first["attempts"] == 1
        assert first["next_attempt_at"] is not None

        await db.pool.execute("UPDATE gap SET next_attempt_at = now()")
        await run_once(db.pool, registry=registry, worker_id="w1")
        second = await db.pool.fetchrow("SELECT attempts FROM gap")
        assert second["attempts"] == 2

    async def test_a_permanently_dead_source_stops_being_retried(self, db):
        """An unreachable source is a fact about the world; the fill_attempt
        rows record why, and the gap stops costing money."""
        await self._seed(db)
        registry = await self._broken_registry()
        for _ in range(8):
            await db.pool.execute(
                "UPDATE gap SET next_attempt_at = now() WHERE resolved_at IS NULL"
            )
            if await run_once(db.pool, registry=registry, worker_id="w1") is None:
                break
        row = await db.pool.fetchrow("SELECT attempts, resolved_at FROM gap")
        assert row["resolved_at"] is not None, "gap retried past MAX_ATTEMPTS"
        assert row["attempts"] <= 6

    async def test_a_bounded_number_of_attempts_is_recorded(self, db):
        """The regression proper: attempts must be single digits, not 14,900."""
        await self._seed(db)
        registry = await self._broken_registry()
        for _ in range(20):
            await run_once(db.pool, registry=registry, worker_id="w1")
        assert await db.pool.fetchval("SELECT count(*) FROM fill_attempt") <= 6
