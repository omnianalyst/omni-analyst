"""W6 -- perception_divergence, the first derived claim.

Covers the behaviour the work order names:

- opposing drift in perception vs fundamentals classifies bearish/bullish
- co-moving series produce no claim
- insufficient history returns None, not a guess
- knowledge_date is the newest input's knowledge_date
- a divergence from a byo_only input is byo_only and invisible to other users
- a divergence from all-allowed inputs lands in shared_coverage
- a derived claim with no claim_input edges is rejected at commit
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import numpy as np
import pytest

from omni.coverage.visibility import visible_claims
from omni.perception.divergence import (
    ConflictingAudience,
    DivergenceInput,
    compute_divergence,
    resolve_derived_licence,
    write_derived,
)

BASE = datetime(2024, 1, 1, tzinfo=UTC)
N = 100
SPIKE = 15


def _gen(perc_delta: float, fact_delta: float, *, seed: int = 0):
    """Two aligned daily series, noisy and co-moving, then a drift in the tail.

    The noise is load-bearing: a flat baseline makes rolling.std() zero and the
    z-scores undefined. Real perception data is never noise-free.
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


def _inputs(
    obs,
    *,
    redistributable: str = "allowed",
    audience_user_id=None,
    knowledge_lag_days: int = 1,
):
    return [
        DivergenceInput(
            id=uuid4(),
            event_date=d,
            knowledge_date=d + timedelta(days=knowledge_lag_days),
            value=v,
            redistributable=redistributable,
            audience_user_id=audience_user_id,
        )
        for d, v in obs
    ]


# --------------------------------------------------------------- classification


class TestClassification:
    def test_rising_perception_falling_fundamentals_is_bearish(self):
        perc_obs, fact_obs = _gen(+20, -20)
        draft = compute_divergence(_inputs(perc_obs), _inputs(fact_obs))
        assert draft is not None
        assert draft.value["direction"] == "bearish"
        ev = draft.evidence
        assert ev["perception_z"] > ev["fact_z"]

    def test_falling_perception_rising_fundamentals_is_bullish(self):
        perc_obs, fact_obs = _gen(-20, +20, seed=1)
        draft = compute_divergence(_inputs(perc_obs), _inputs(fact_obs))
        assert draft is not None
        assert draft.value["direction"] == "bullish"
        ev = draft.evidence
        assert ev["perception_z"] < ev["fact_z"]

    def test_co_moving_series_produce_no_divergence(self):
        perc_obs, fact_obs = _gen(+20, +20, seed=2)
        assert compute_divergence(_inputs(perc_obs), _inputs(fact_obs)) is None

    def test_co_moving_downward_produce_no_divergence(self):
        perc_obs, fact_obs = _gen(-20, -20, seed=3)
        assert compute_divergence(_inputs(perc_obs), _inputs(fact_obs)) is None


# ------------------------------------------------------------- honest abstention


class TestInsufficientHistory:
    def test_too_few_aligned_points_returns_none(self):
        perc_obs, fact_obs = _gen(+20, -20)
        # MIN_HISTORY is 4 * window = 80 at the default window. Trim below it.
        assert (
            compute_divergence(_inputs(perc_obs[:70]), _inputs(fact_obs[:70]))
            is None
        )

    def test_empty_inputs_return_none(self):
        assert compute_divergence([], []) is None

    def test_one_side_empty_returns_none(self):
        perc_obs, _ = _gen(+20, -20)
        assert compute_divergence(_inputs(perc_obs), []) is None


# --------------------------------------------------------------- bitemporal rule


class TestKnowledgeDate:
    def test_knowledge_date_is_the_newest_input_knowledge_date(self):
        perc_obs, fact_obs = _gen(+20, -20)
        perc = _inputs(perc_obs, knowledge_lag_days=1)
        # Give facts a later knowledge_date so the max is unambiguous.
        facts = _inputs(fact_obs, knowledge_lag_days=3)
        expected = max(c.knowledge_date for c in (*perc, *facts))

        draft = compute_divergence(perc, facts)
        assert draft is not None
        assert draft.knowledge_date == expected

    def test_event_date_is_the_last_observation(self):
        perc_obs, fact_obs = _gen(+20, -20)
        draft = compute_divergence(_inputs(perc_obs), _inputs(fact_obs))
        assert draft is not None
        assert draft.event_date == perc_obs[-1][0]

    def test_evidence_carries_window_zscores_direction_and_ids(self):
        perc_obs, fact_obs = _gen(+20, -20)
        perc = _inputs(perc_obs)
        facts = _inputs(fact_obs)
        draft = compute_divergence(perc, facts)
        assert draft is not None
        ev = draft.evidence
        assert ev["window"] == 20
        assert isinstance(ev["perception_z"], float)
        assert isinstance(ev["fact_z"], float)
        assert ev["direction"] == draft.value["direction"]
        assert set(ev["input_claim_ids"]) == {
            str(c.id) for c in (*perc, *facts)
        }


# --------------------------------------------------------------- licence resolution


class TestResolveLicence:
    def test_all_allowed_inputs_are_shared(self):
        inputs = [
            DivergenceInput(uuid4(), BASE, BASE, 1.0, "allowed", None),
            DivergenceInput(uuid4(), BASE, BASE, 2.0, "allowed", None),
        ]
        assert resolve_derived_licence(inputs) == ("allowed", None)

    def test_one_byo_input_makes_the_whole_derivation_private(self):
        owner = uuid4()
        inputs = [
            DivergenceInput(uuid4(), BASE, BASE, 1.0, "allowed", None),
            DivergenceInput(
                uuid4(), BASE, BASE, 2.0, "byo_only", audience_user_id=owner
            ),
        ]
        assert resolve_derived_licence(inputs) == ("byo_only", owner)

    def test_byo_inputs_from_different_owners_refuse(self):
        a, b = uuid4(), uuid4()
        inputs = [
            DivergenceInput(
                uuid4(), BASE, BASE, 1.0, "byo_only", audience_user_id=a
            ),
            DivergenceInput(
                uuid4(), BASE, BASE, 2.0, "byo_only", audience_user_id=b
            ),
        ]
        with pytest.raises(ConflictingAudience):
            resolve_derived_licence(inputs)


# --------------------------------------------------------------- database writes


_INSERT_CLAIM = """
INSERT INTO claim (entity_id, claim_type, key, value, source,
                   event_date, knowledge_date, confidence,
                   redistributable, audience_user_id, derivation)
VALUES ($1,$2::claim_type,$3,$4::jsonb,$5,$6,$7,$8,
        $9::redistribution,$10,'ingested')
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
    db,
    entity_id,
    obs,
    *,
    claim_type,
    key,
    source,
    redistributable,
    audience_user_id,
):
    """Insert scalar claims and return (ids, DivergenceInputs)."""
    ids = []
    inputs = []
    for event_date, value in obs:
        knowledge_date = event_date + timedelta(days=1)
        cid = await db.pool.fetchval(
            _INSERT_CLAIM,
            entity_id,
            claim_type,
            key,
            json.dumps({"value": value}),
            source,
            event_date,
            knowledge_date,
            1.0,
            redistributable,
            audience_user_id,
        )
        ids.append(cid)
        inputs.append(
            DivergenceInput(
                id=cid,
                event_date=event_date,
                knowledge_date=knowledge_date,
                value=value,
                redistributable=redistributable,
                audience_user_id=audience_user_id,
            )
        )
    return ids, inputs


class TestByoOnlyPropagation:
    async def test_byo_input_yields_private_divergence_hidden_from_others(
        self, db
    ):
        entity_id = await _entity(db)
        owner, other = uuid4(), uuid4()

        perc_obs, fact_obs = _gen(+20, -20)
        perc_ids, perc_inputs = await _insert_series(
            db, entity_id, perc_obs,
            claim_type="perception_social", key="score",
            source="polygon", redistributable="byo_only",
            audience_user_id=owner,
        )
        fact_ids, fact_inputs = await _insert_series(
            db, entity_id, fact_obs,
            claim_type="fundamental_metric", key="revenue",
            source="sec_edgar", redistributable="allowed",
            audience_user_id=None,
        )

        draft = compute_divergence(perc_inputs, fact_inputs)
        assert draft is not None

        redistributable, audience = resolve_derived_licence(
            [*perc_inputs, *fact_inputs]
        )
        assert redistributable == "byo_only"
        assert audience == owner

        claim_id = await write_derived(
            db.pool,
            draft,
            entity_id=entity_id,
            input_claim_ids=[*perc_ids, *fact_ids],
            audience_user_id=audience,
            redistributable=redistributable,
        )
        assert claim_id is not None

        # The owner sees the derived claim.
        owner_rows = await visible_claims(
            db.pool, audience=owner, claim_type="perception_divergence"
        )
        assert any(r["id"] == claim_id for r in owner_rows)

        # A different user does not -- this is the leak the rule prevents.
        other_rows = await visible_claims(
            db.pool, audience=other, claim_type="perception_divergence"
        )
        assert all(r["id"] != claim_id for r in other_rows)
        assert other_rows == []


class TestSharedAllowed:
    async def test_all_allowed_inputs_appear_in_shared_coverage(self, db):
        entity_id = await _entity(db)

        perc_obs, fact_obs = _gen(+20, -20)
        perc_ids, perc_inputs = await _insert_series(
            db, entity_id, perc_obs,
            claim_type="perception_macro", key="vix",
            source="fred", redistributable="allowed",
            audience_user_id=None,
        )
        fact_ids, fact_inputs = await _insert_series(
            db, entity_id, fact_obs,
            claim_type="fundamental_metric", key="revenue",
            source="sec_edgar", redistributable="allowed",
            audience_user_id=None,
        )

        draft = compute_divergence(perc_inputs, fact_inputs)
        assert draft is not None

        redistributable, audience = resolve_derived_licence(
            [*perc_inputs, *fact_inputs]
        )
        assert (redistributable, audience) == ("allowed", None)

        claim_id = await write_derived(
            db.pool,
            draft,
            entity_id=entity_id,
            input_claim_ids=[*perc_ids, *fact_ids],
            audience_user_id=audience,
            redistributable=redistributable,
        )

        shared = await db.pool.fetch(
            "SELECT id, key FROM shared_coverage "
            "WHERE claim_type = 'perception_divergence'"
        )
        assert any(r["id"] == claim_id for r in shared)


class TestDerivedClaimRequiresInputs:
    async def test_commit_fails_without_claim_input_edges(self, db):
        """The deferred trigger is load-bearing: bypass write_derived and
        insert a derived claim with no edges; commit must reject it."""
        entity_id = await _entity(db)
        with pytest.raises(asyncpg.CheckViolationError, match="declares no inputs"):
            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO claim (
                            entity_id, claim_type, key, value, source,
                            event_date, knowledge_date, confidence,
                            redistributable, audience_user_id, derivation
                        )
                        VALUES ($1,'perception_divergence','orphan',
                                '{}'::jsonb,'internal',
                                $2,$3,0.5,'allowed',NULL,'derived')
                        """,
                        entity_id,
                        BASE,
                        BASE + timedelta(days=1),
                    )
        # Nothing landed.
        assert await db.pool.fetchval(
            "SELECT count(*) FROM claim WHERE derivation = 'derived'"
        ) == 0

    async def test_write_derived_persists_edges(self, db):
        """The happy path: edges are written and the claim commits."""
        entity_id = await _entity(db)

        perc_obs, fact_obs = _gen(+20, -20)
        perc_ids, perc_inputs = await _insert_series(
            db, entity_id, perc_obs,
            claim_type="perception_macro", key="vix",
            source="fred", redistributable="allowed",
            audience_user_id=None,
        )
        fact_ids, fact_inputs = await _insert_series(
            db, entity_id, fact_obs,
            claim_type="fundamental_metric", key="revenue",
            source="sec_edgar", redistributable="allowed",
            audience_user_id=None,
        )

        draft = compute_divergence(perc_inputs, fact_inputs)
        claim_id = await write_derived(
            db.pool,
            draft,
            entity_id=entity_id,
            input_claim_ids=[*perc_ids, *fact_ids],
            audience_user_id=None,
            redistributable="allowed",
        )
        edge_count = await db.pool.fetchval(
            "SELECT count(*) FROM claim_input WHERE claim_id = $1", claim_id
        )
        assert edge_count == len(perc_ids) + len(fact_ids)
