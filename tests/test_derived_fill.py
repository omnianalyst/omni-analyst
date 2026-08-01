"""A1/D5 -- the derived-claim fill path, driven through fill_analysis.

Covers the behaviour the work order names, now through the **declared**
path (``ArgumentSpec`` + ``materialize``) rather than an injected gather:

- a perception_divergence gap filled from two shared inputs, producing a claim
  and its claim_input edges in one commit
- a divergence derived from one byo_only input written private and invisible
  to another user -- the leak test
- insufficient history producing unfillable with a reason and no claim
- the gap's attempts incrementing and next_attempt_at set on an unfillable,
  matching the ingested path's backoff
- inputs read audience-scoped: a private input of another user is not
  materialized (Abstention), so the derivation cannot proceed
- the claim_input edges written equal the ids materialization reported
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import numpy as np
import pytest

from omni.capability.arguments import (
    Abstention,
    Materialized,
    materialize,
)
from omni.capability.derived import ARGUMENTS, DERIVED
from omni.coverage.visibility import visible_claims
from omni.fill.derived import fill_analysis
from omni.perception.divergence import resolve_derived_licence

BASE = datetime(2024, 1, 1, tzinfo=UTC)
N = 100
SPIKE = 15


def _gen(perc_delta: float, fact_delta: float, *, seed: int = 0):
    """Two aligned daily series, noisy and co-moving, then opposing drift.

    The noise is load-bearing: a flat baseline makes rolling.std() zero and the
    z-scores undefined. 100 points clears the 4*window (80) history floor.
    """
    rng = np.random.default_rng(seed)
    base = 50.0 + rng.normal(0, 0.8, N)
    perc = base.copy()
    fact = base.copy()
    for i in range(N - SPIKE, N):
        k = (i - (N - SPIKE)) / SPIKE
        perc[i] += perc_delta * k
        fact[i] += fact_delta * k
    dates = [BASE + timedelta(days=i) for i in range(N)]
    return list(zip(dates, perc.tolist())), list(zip(dates, fact.tolist()))


_INSERT_CLAIM = """
INSERT INTO claim (entity_id, claim_type, key, value, source,
                   event_date, knowledge_date, confidence,
                   redistributable, audience_user_id, derivation)
VALUES ($1,$2::claim_type,$3,$4::jsonb,$5,$6,$7,$8,
        $9::redistribution,$10,'ingested')
RETURNING id
"""

_INSERT_GAP = """
INSERT INTO gap (entity_id, claim_type, key, gap_class,
                 audience_user_id, score, detail)
VALUES ($1, $2::claim_type, $3, $4::gap_class, $5, $6, '{}'::jsonb)
RETURNING id
"""


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company', $1, $1) "
        "RETURNING id",
        symbol,
    )


async def _insert_series(
    db, entity_id, obs, *, claim_type, key, source,
    redistributable, audience_user_id,
):
    ids = []
    for event_date, value in obs:
        knowledge_date = event_date + timedelta(days=1)
        cid = await db.pool.fetchval(
            _INSERT_CLAIM, entity_id, claim_type, key,
            json.dumps({"value": value}), source,
            event_date, knowledge_date, 1.0,
            redistributable, audience_user_id,
        )
        ids.append(cid)
    return ids


async def _gap(db, entity_id, *, audience_user_id=None):
    gap_id = await db.pool.fetchval(
        _INSERT_GAP, entity_id, "perception_divergence",
        "perception_vs_fundamentals", "missing", audience_user_id, 100.0,
    )
    return {
        "id": gap_id,
        "entity_id": entity_id,
        "claim_type": "perception_divergence",
        "key": "perception_vs_fundamentals",
        "audience_user_id": audience_user_id,
    }


async def _seed_shared(db, entity_id, *, seed=0):
    perc_obs, fact_obs = _gen(+20, -20, seed=seed)
    perc_ids = await _insert_series(
        db, entity_id, perc_obs, claim_type="perception_macro", key="vix",
        source="fred", redistributable="allowed", audience_user_id=None,
    )
    fact_ids = await _insert_series(
        db, entity_id, fact_obs, claim_type="fundamental_metric", key="Revenues",
        source="sec_edgar", redistributable="allowed", audience_user_id=None,
    )
    return perc_ids, fact_ids


# --------------------------------------------------------------- the happy path


class TestFilledFromSharedInputs:
    async def test_writes_claim_and_claim_input_edges(self, db):
        entity_id = await _entity(db)
        perc_ids, fact_ids = await _seed_shared(db, entity_id)
        gap = await _gap(db, entity_id, audience_user_id=None)

        result = await fill_analysis(db.pool, gap, capability=DERIVED)

        assert result.outcome == "filled", result.reason
        assert result.capability == "perception.divergence"
        assert len(result.claim_ids) == 1
        claim_id = result.claim_ids[0]

        # The derived claim is marked derived and lands in shared coverage.
        row = await db.pool.fetchrow(
            "SELECT derivation, redistributable, audience_user_id "
            "FROM claim WHERE id = $1",
            claim_id,
        )
        assert row["derivation"] == "derived"
        assert row["redistributable"] == "allowed"
        assert row["audience_user_id"] is None
        assert await db.pool.fetchval(
            "SELECT count(*) FROM shared_coverage WHERE id = $1", claim_id
        ) == 1

        # An edge was written for every input, in the same commit -- the
        # deferred trigger would have rejected the claim otherwise.
        edge_ids = {
            r["input_id"] for r in await db.pool.fetch(
                "SELECT input_id FROM claim_input WHERE claim_id = $1", claim_id
            )
        }
        assert edge_ids == set(perc_ids) | set(fact_ids)

        # The gap is closed and the attempt is on the record.
        assert await db.pool.fetchval(
            "SELECT resolved_at IS NOT NULL FROM gap WHERE id = $1", gap["id"]
        )
        attempt = await db.pool.fetchrow(
            "SELECT outcome, claim_id FROM fill_attempt WHERE gap_id = $1",
            gap["id"],
        )
        assert attempt["outcome"] == "filled"
        assert attempt["claim_id"] == claim_id


# --------------------------------------------------------------- the leak test


class TestByoOnlyLeak:
    async def test_byo_input_is_private_and_invisible_to_another_user(self, db):
        entity_id = await _entity(db)
        owner, other = uuid4(), uuid4()

        perc_obs, fact_obs = _gen(+20, -20)
        # perception is the owner's private (byo_only) series.
        perc_ids = await _insert_series(
            db, entity_id, perc_obs, claim_type="perception_macro", key="vix",
            source="polygon", redistributable="byo_only",
            audience_user_id=owner,
        )
        # fundamentals are shared.
        await _insert_series(
            db, entity_id, fact_obs, claim_type="fundamental_metric",
            key="Revenues", source="sec_edgar", redistributable="allowed",
            audience_user_id=None,
        )
        # The gap belongs to the owner: only they can see the byo input.
        gap = await _gap(db, entity_id, audience_user_id=owner)

        result = await fill_analysis(db.pool, gap, capability=DERIVED)
        assert result.outcome == "filled", result.reason
        claim_id = result.claim_ids[0]

        # The derivation inherits the most restrictive input's licence.
        row = await db.pool.fetchrow(
            "SELECT redistributable, audience_user_id FROM claim WHERE id = $1",
            claim_id,
        )
        assert row["redistributable"] == "byo_only"
        assert row["audience_user_id"] == owner

        # The owner sees the derived claim.
        owner_rows = await visible_claims(
            db.pool, audience=owner, claim_type="perception_divergence"
        )
        assert any(r["id"] == claim_id for r in owner_rows)

        # A different user does not -- the leak this path exists to prevent.
        other_rows = await visible_claims(
            db.pool, audience=other, claim_type="perception_divergence"
        )
        assert all(r["id"] != claim_id for r in other_rows)
        assert other_rows == []

        # And it never reached shared coverage.
        assert await db.pool.fetchval(
            "SELECT count(*) FROM shared_coverage WHERE id = $1", claim_id
        ) == 0

        # The byo input is still an edge: licence propagation holds both ways.
        edge_ids = {
            r["input_id"] for r in await db.pool.fetch(
                "SELECT input_id FROM claim_input WHERE claim_id = $1", claim_id
            )
        }
        assert set(perc_ids) <= edge_ids


# ----------------------------------------------------------- honest abstention


class TestInsufficientHistory:
    async def test_too_few_inputs_is_unfillable_and_writes_no_claim(self, db):
        entity_id = await _entity(db)
        perc_obs, fact_obs = _gen(+20, -20)
        # Below the 4*window (80) aligned-point floor the engine needs.
        await _insert_series(
            db, entity_id, perc_obs[:30], claim_type="perception_macro",
            key="vix", source="fred", redistributable="allowed",
            audience_user_id=None,
        )
        await _insert_series(
            db, entity_id, fact_obs[:30], claim_type="fundamental_metric",
            key="Revenues", source="sec_edgar", redistributable="allowed",
            audience_user_id=None,
        )
        gap = await _gap(db, entity_id, audience_user_id=None)

        result = await fill_analysis(db.pool, gap, capability=DERIVED)

        assert result.outcome == "unfillable"
        assert result.reason is not None
        assert result.claim_ids == []

        # No claim and no edges were written.
        assert await db.pool.fetchval(
            "SELECT count(*) FROM claim WHERE derivation = 'derived'"
        ) == 0
        assert await db.pool.fetchval("SELECT count(*) FROM claim_input") == 0

        attempt = await db.pool.fetchrow(
            "SELECT outcome, reason, claim_id FROM fill_attempt WHERE gap_id = $1",
            gap["id"],
        )
        assert attempt["outcome"] == "unfillable"
        assert attempt["claim_id"] is None
        assert attempt["reason"] == result.reason


# --------------------------------------------------------- backoff equivalence


class TestBackoffMatchesIngestedPath:
    async def test_unfillable_increments_attempts_and_sets_next_attempt_at(
        self, db
    ):
        entity_id = await _entity(db)
        perc_obs, fact_obs = _gen(+20, -20)
        await _insert_series(
            db, entity_id, perc_obs[:30], claim_type="perception_macro",
            key="vix", source="fred", redistributable="allowed",
            audience_user_id=None,
        )
        await _insert_series(
            db, entity_id, fact_obs[:30], claim_type="fundamental_metric",
            key="Revenues", source="sec_edgar", redistributable="allowed",
            audience_user_id=None,
        )
        gap = await _gap(db, entity_id, audience_user_id=None)

        before = await db.pool.fetchrow(
            "SELECT attempts, next_attempt_at FROM gap WHERE id = $1", gap["id"]
        )
        assert before["attempts"] == 0
        assert before["next_attempt_at"] is None

        result = await fill_analysis(db.pool, gap, capability=DERIVED)
        assert result.outcome == "unfillable"

        after = await db.pool.fetchrow(
            "SELECT attempts, next_attempt_at, resolved_at FROM gap "
            "WHERE id = $1",
            gap["id"],
        )
        # The same _RELEASE the ingested path uses: attempts grows, a backoff
        # window is set, and the gap stays open for a later attempt.
        assert after["attempts"] == 1
        assert after["next_attempt_at"] is not None
        assert after["resolved_at"] is None


# ------------------------------------------------------- audience-scoped gather


class TestAudienceScopedMaterialize:
    async def test_a_private_input_of_another_user_is_not_materialized(self, db):
        entity_id = await _entity(db)
        owner, other = uuid4(), uuid4()

        perc_obs, fact_obs = _gen(+20, -20)
        # A perception series private to `owner`.
        await _insert_series(
            db, entity_id, perc_obs, claim_type="perception_macro", key="vix",
            source="polygon", redistributable="byo_only",
            audience_user_id=owner,
        )
        # Shared fundamentals anyone can see.
        await _insert_series(
            db, entity_id, fact_obs, claim_type="fundamental_metric",
            key="Revenues", source="sec_edgar", redistributable="allowed",
            audience_user_id=None,
        )

        gap = await _gap(db, entity_id, audience_user_id=other)

        # The other user cannot see owner's private perception series, so
        # materialize abstains -- the private claim never reaches a derivation
        # it must not feed.
        perc_result = await materialize(
            ARGUMENTS[0], db.pool, entity_id=entity_id, audience=other
        )
        assert isinstance(perc_result, Abstention)

        # The shared fundamentals are visible.
        fact_result = await materialize(
            ARGUMENTS[1], db.pool, entity_id=entity_id, audience=other
        )
        assert isinstance(fact_result, Materialized)
        assert len(fact_result.value) == N

        # With one whole side abstaining the derivation cannot proceed.
        result = await fill_analysis(db.pool, gap, capability=DERIVED)
        assert result.outcome == "unfillable"
        assert await db.pool.fetchval(
            "SELECT count(*) FROM claim WHERE derivation = 'derived'"
        ) == 0


# ----------------------------------------- edges equal materialized ids (D5 risk)


class TestEdgesEqualMaterializedIds:
    async def test_claim_input_edges_equal_materialized_claim_ids(self, db):
        """The claim_input edges must be exactly the ids materialization
        reported -- not a subset. An incomplete edge set means a derived claim
        whose licence is computed from a subset of its real inputs (a licence
        leak with extra steps)."""
        entity_id = await _entity(db)
        await _seed_shared(db, entity_id)
        gap = await _gap(db, entity_id, audience_user_id=None)

        # Materialize independently to get the reported claim_ids.
        perc_m = await materialize(
            ARGUMENTS[0], db.pool, entity_id=entity_id, audience=None
        )
        fact_m = await materialize(
            ARGUMENTS[1], db.pool, entity_id=entity_id, audience=None
        )
        assert isinstance(perc_m, Materialized)
        assert isinstance(fact_m, Materialized)
        materialized_ids = set(perc_m.claim_ids) | set(fact_m.claim_ids)

        result = await fill_analysis(db.pool, gap, capability=DERIVED)
        assert result.outcome == "filled", result.reason
        claim_id = result.claim_ids[0]

        edge_ids = {
            r["input_id"] for r in await db.pool.fetch(
                "SELECT input_id FROM claim_input WHERE claim_id = $1", claim_id
            )
        }
        assert edge_ids == materialized_ids


class _VisibilityReadCounter:
    """Wraps a pool, counting ``fetch`` calls that touch the visibility CTE.

    Every read through ``visible_claims`` (and the old ``_to_inputs_by_ids`` /
    ``_licence_inputs`` re-reads) carries ``redistributable = 'allowed'`` in its
    SQL; no other query in the fill path does. Delegates all other methods
    (``fetchval``, ``execute``, ``acquire``) to the real pool untouched.
    """

    def __init__(self, pool):
        self._pool = pool
        self.count = 0

    async def fetch(self, sql, *args):
        if "redistributable = 'allowed'" in sql:
            self.count += 1
        return await self._pool.fetch(sql, *args)

    def __getattr__(self, name):
        return getattr(self._pool, name)


# ----------------------------------------- licence from carried rows (D7 risk)


class TestLicenceFromCarriedRows:
    async def test_resolve_derived_licence_over_rows_matches_re_read(self, db):
        """``resolve_derived_licence`` over ``Materialized.rows`` produces the
        same ``(redistributable, audience)`` the old re-read produced. The
        byo_only leak test depends on this: one byo_only input among allowed
        ones makes the whole derivation private to that input's owner."""
        entity_id = await _entity(db)
        owner = uuid4()
        perc_obs, fact_obs = _gen(+20, -20)
        await _insert_series(
            db, entity_id, perc_obs, claim_type="perception_macro", key="vix",
            source="polygon", redistributable="byo_only",
            audience_user_id=owner,
        )
        await _insert_series(
            db, entity_id, fact_obs, claim_type="fundamental_metric",
            key="Revenues", source="sec_edgar", redistributable="allowed",
            audience_user_id=None,
        )

        perc_m = await materialize(
            ARGUMENTS[0], db.pool, entity_id=entity_id, audience=owner
        )
        fact_m = await materialize(
            ARGUMENTS[1], db.pool, entity_id=entity_id, audience=owner
        )
        assert isinstance(perc_m, Materialized)
        assert isinstance(fact_m, Materialized)

        # The licence inputs are the carried rows -- no re-read.
        rows = [*perc_m.rows, *fact_m.rows]
        redistributable, audience = resolve_derived_licence(rows)

        # One byo_only input among allowed ones makes the whole derivation
        # private to that input's owner.
        assert redistributable == "byo_only"
        assert audience == owner

        # Cross-check: the perception rows carry byo_only, the fundamental
        # rows carry allowed -- the most restrictive wins.
        assert all(r.redistributable == "byo_only" for r in perc_m.rows)
        assert all(r.redistributable == "allowed" for r in fact_m.rows)


# ----------------------------------------------------------- re-reads are gone


class TestNoReReads:
    async def test_declared_fill_reads_visible_claims_once_per_argument(self, db):
        """A declared ``fill_analysis`` issues exactly one ``visible_claims``
        read per ArgumentSpec (materialization) and no more. The re-reads D5
        carried -- two ``_to_inputs_by_ids`` in the compute adapter and one
        ``_licence_inputs`` for the licence -- are gone, supplied by
        ``Materialized.rows``."""
        entity_id = await _entity(db)
        await _seed_shared(db, entity_id)
        gap = await _gap(db, entity_id, audience_user_id=None)

        counter = _VisibilityReadCounter(db.pool)
        result = await fill_analysis(counter, gap, capability=DERIVED)
        assert result.outcome == "filled", result.reason

        # Two ArgumentSpecs -> two materialization reads. The old code added
        # three more (two _to_inputs_by_ids + one _licence_inputs); those are
        # gone. Exactly len(ARGUMENTS) visibility reads remain.
        assert counter.count == len(ARGUMENTS)
