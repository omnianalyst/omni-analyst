"""How the fill pipeline chooses the argument it hands an adapter.

The defect this file pins down: the pipeline used to pass ``gap["key"]`` to every
producer. For a producer that keys per entity (EDGAR by CIK, Polygon by ticker,
CoinGecko by coin id) that key is NULL on a real gap, so EDGAR was handed ``None``
and asked for ``CIK000000None.json``. The provider must decide which key it is
fetched by -- read from the entity for per-entity providers, from the gap's series
key otherwise -- and a missing key must decline honestly rather than be passed
through as NULL.

Every assertion is on the argument the adapter actually received, or on the fact
that it was never called. The fake records its calls; no real network is made.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.fill.pipeline import MAX_ATTEMPTS, NO_NEW_DATA_COOLDOWN_SECONDS, run_once
from omni.ingest.protocol import ClaimDraft, Unavailable

NOW = datetime(2026, 7, 27, tzinfo=UTC)


class Recorder:
    """A capability call that records every argument it was handed."""

    def __init__(self, drafts):
        self._drafts = drafts
        self.calls: list = []

    async def __call__(self, key):
        self.calls.append(key)
        return list(self._drafts)


def _draft(claim_type: str, key: str) -> ClaimDraft:
    return ClaimDraft(
        claim_type=claim_type,
        key=key,
        value={"amount": 1},
        event_date=NOW - timedelta(days=1),
        knowledge_date=NOW,
        confidence=1.0,
    )


def _cap(provider_key, produces, call, *, name=None, entity_kinds=()):
    return Capability(
        name=name or f"{provider_key}.test",
        description="recording test capability",
        produces=produces,
        provider_key=provider_key,
        source=provider_key,
        entity_kinds=entity_kinds,
        maturity=Maturity.WIRED,
        callability=Callability.YES,
        call=call,
    )


async def _entity(db, *, identifiers=None, kind="company", symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name, identifiers) "
        "VALUES ($1, $2, $2, $3::jsonb) RETURNING id",
        kind,
        symbol,
        json.dumps(identifiers or {}),
    )


async def _gap(db, entity_id, *, claim_type, key, audience=None, score=1.0):
    return await db.pool.fetchval(
        "INSERT INTO gap (entity_id, claim_type, key, gap_class, "
        "audience_user_id, score) "
        "VALUES ($1, $2::claim_type, $3, 'missing', $4, $5) RETURNING id",
        entity_id,
        claim_type,
        key,
        audience,
        score,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, demand CASCADE")
    yield


class TestPerEntityRouting:
    async def test_the_adapter_is_called_with_the_identifier_not_gap_key(self, db):
        """The point of the order: EDGAR gets the CIK, never gap["key"] (NULL)."""
        entity_id = await _entity(db, identifiers={"cik": "0000320193"})
        # gap.key is NULL, exactly as it is on a real fundamental_metric gap.
        await _gap(db, entity_id, claim_type="fundamental_metric", key=None)

        rec = Recorder([_draft("fundamental_metric", "Assets")])
        registry = Registry()
        registry.add(
            _cap("sec_edgar", ("fundamental_metric",), rec, entity_kinds=("company",))
        )

        result = await run_once(db.pool, registry=registry, worker_id="w1")
        assert result.outcome == "filled", result.reason
        assert rec.calls == ["0000320193"], (
            f"adapter was handed {rec.calls!r}, expected the CIK only"
        )

    async def test_a_missing_identifier_declines_unfilled_and_makes_no_call(self, db):
        """The other half: no CIK -> unfillable with a reason, and EDGAR is never
        asked. A missing identifier is permanent, not a transient error."""
        entity_id = await _entity(db, identifiers={})  # carries no cik
        await _gap(db, entity_id, claim_type="fundamental_metric", key=None)

        rec = Recorder([_draft("fundamental_metric", "Assets")])
        registry = Registry()
        registry.add(_cap("sec_edgar", ("fundamental_metric",), rec))

        result = await run_once(db.pool, registry=registry, worker_id="w1")
        assert result.outcome == "unfillable"
        assert rec.calls == [], "the adapter must not be invoked without an identifier"

        assert result.reason is not None
        assert "cik" in result.reason, (
            f"reason must name the missing identifier, got {result.reason!r}"
        )
        assert "sec_edgar" in result.reason

        attempt = await db.pool.fetchrow(
            "SELECT outcome, claim_id, reason FROM fill_attempt"
        )
        assert attempt["outcome"] == "unfillable"
        assert attempt["claim_id"] is None
        assert attempt["reason"] and "cik" in attempt["reason"]


class TestSeriesRouting:
    async def test_a_series_provider_is_called_with_gap_key(self, db):
        """Regression guard on the path that already works: FRED gets 'GDP'."""
        entity_id = await _entity(db)  # carries no identifiers, and needs none
        await _gap(db, entity_id, claim_type="macro_series_point", key="GDP")

        rec = Recorder([_draft("macro_series_point", "GDP")])
        registry = Registry()
        registry.add(_cap("fred", ("macro_series_point",), rec))

        result = await run_once(db.pool, registry=registry, worker_id="w1")
        assert result.outcome == "filled", result.reason
        assert rec.calls == ["GDP"]

    async def test_a_null_key_for_a_series_provider_does_not_invent_a_call(self, db):
        """A NULL series key is not something to fetch. Passing it through is the
        same shape of mistake as handing EDGAR a CIK of None."""
        entity_id = await _entity(db)
        await _gap(db, entity_id, claim_type="macro_series_point", key=None)

        rec = Recorder([_draft("macro_series_point", "GDP")])
        registry = Registry()
        registry.add(_cap("fred", ("macro_series_point",), rec))

        result = await run_once(db.pool, registry=registry, worker_id="w1")
        assert result.outcome == "unfillable"
        assert rec.calls == [], "a NULL key must not reach the adapter"
        assert result.reason is not None and "fred" in result.reason

        attempt = await db.pool.fetchrow(
            "SELECT outcome, claim_id FROM fill_attempt"
        )
        assert attempt["outcome"] == "unfillable"
        assert attempt["claim_id"] is None


class TestNoNewDataCooldown:
    """The "all already held" branch: a correct source re-queried for data that
    cannot have changed. This is the rate-budget protection (a daily-bar source
    re-queried every sweep burned ~the entire free tier). It must cool down
    WITHOUT counting as a failure -- attempts not incremented, gap stays open --
    or a regression to _RELEASE would both penalize the source and let
    detect_gaps reopen a fresh gap each sweep."""

    async def test_all_already_held_cooldowns_without_incrementing_or_resolving(
        self, db
    ):
        entity_id = await _entity(db)
        # A claim identical to what the capability will draft, so write_claims
        # inserts nothing (ON CONFLICT DO NOTHING) -> "all already held".
        await db.pool.execute(
            "INSERT INTO claim (entity_id, claim_type, key, value, source, "
            "event_date, knowledge_date, confidence, redistributable, "
            "audience_user_id, derivation) "
            "VALUES ($1,'macro_series_point'::claim_type,'GDP',"
            "'{\"amount\": 1}'::jsonb,'fred',$2,$3,1.0,'allowed',NULL,'ingested')",
            entity_id,
            NOW - timedelta(days=1),
            NOW,
        )
        gap_id = await _gap(db, entity_id, claim_type="macro_series_point", key="GDP")

        rec = Recorder([_draft("macro_series_point", "GDP")])
        registry = Registry()
        registry.add(_cap("fred", ("macro_series_point",), rec))

        result = await run_once(db.pool, registry=registry, worker_id="w1")
        assert result.outcome == "unfillable"
        assert result.reason is not None and "already held" in result.reason

        gap = await db.pool.fetchrow(
            "SELECT attempts, resolved_at, next_attempt_at FROM gap WHERE id = $1",
            gap_id,
        )
        # Not a failure: attempts stays at 0 and the gap stays OPEN. A regression
        # to _RELEASE would increment attempts and/or resolve.
        assert gap["attempts"] == 0
        assert gap["resolved_at"] is None
        # Retry scheduled ~6h out, not immediately.
        assert gap["next_attempt_at"] is not None
        delta = (gap["next_attempt_at"].replace(tzinfo=UTC)
                 - datetime.now(UTC)).total_seconds()
        assert NO_NEW_DATA_COOLDOWN_SECONDS - 600 < delta < NO_NEW_DATA_COOLDOWN_SECONDS + 600


class TestUnreachableSourceExhaustsAndResolves:
    """A source that keeps failing backs off exponentially and, after
    MAX_ATTEMPTS, resolves the gap -- an unreachable source is a fact about the
    world, recorded in fill_attempt, not retried forever."""

    async def test_a_persistently_failing_source_resolves_after_max_attempts(
        self, db
    ):
        entity_id = await _entity(db)
        gap_id = await _gap(db, entity_id, claim_type="macro_series_point", key="GDP")

        async def always_unavailable(key):
            raise Unavailable("source unreachable")

        registry = Registry()
        registry.add(_cap("fred", ("macro_series_point",), always_unavailable))

        for _ in range(MAX_ATTEMPTS):
            # The real scheduler waits out the backoff between attempts; the
            # exhaustion path is what's under test here, so force the gap back
            # to leasable each cycle.
            await db.pool.execute(
                "UPDATE gap SET lease_owner = NULL, lease_expires_at = NULL, "
                "next_attempt_at = NULL WHERE id = $1",
                gap_id,
            )
            result = await run_once(db.pool, registry=registry, worker_id="w1")
            assert result is not None
            assert result.outcome == "unfillable"

        gap = await db.pool.fetchrow(
            "SELECT attempts, resolved_at FROM gap WHERE id = $1", gap_id
        )
        assert gap["attempts"] == MAX_ATTEMPTS
        # Exhausted -> resolved, not retried forever. The pre-fix path retried
        # indefinitely; pin that the door closes.
        assert gap["resolved_at"] is not None
