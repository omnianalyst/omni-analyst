"""D8 -- name-keyed analysis invocation.

Covers the behaviour the work order names:

- ``perception.divergence`` (the one declared analysis) is callable by name and
  returns a result with its contributing claim ids
- a short window abstains, naming the argument and the shortfall; the compute
  function is NOT called (spy proof)
- a capability with no declared arguments is refused with a reason, and
  ``capability.call`` is never reached
- an unknown name and a registered-but-not-invocable name each return their
  distinct client error
- audience isolation: a claim private to A is invisible to B, so B's request
  abstains rather than computing from A's licensed data
- the licence verdict is correct for a shareable input set and a BYO one
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import numpy as np
import pytest
from neutron.error import AppError

from omni.capability.derived import DERIVED
from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.fill.derived import DerivedCapability
from omni.orchestrator.analysis import run_analysis
from omni.scheduler.worker import default_registry

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


# ----------------------------------------------------------- callable by name


class TestCallableByName:
    async def test_divergence_returns_result_and_contributing_claim_ids(self, db):
        entity_id = await _entity(db)
        perc_ids, fact_ids = await _seed_shared(db, entity_id)

        result = await run_analysis(
            default_registry(),
            db.pool,
            name="perception.divergence",
            entity_id=entity_id,
            audience=None,
        )

        assert not result.abstained
        assert result.capability == "perception.divergence"
        assert result.result is not None
        assert result.result.claim_type == "perception_divergence"
        assert "direction" in result.result.value
        assert "score" in result.result.value

        expected = {str(x) for x in perc_ids} | {str(x) for x in fact_ids}
        assert set(result.evidence) == expected


# ----------------------------------------------------------- honest abstention


class TestAbstention:
    async def test_short_window_abstains_and_compute_is_not_called(
        self, db, monkeypatch
    ):
        """Below ``min_obs`` the argument abstains and the compute function is
        never called. The spy is the proof: if it records an invocation the
        path has degraded into calling compute with a short or padded series."""
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

        from omni.orchestrator import analysis as analysis_module

        called: list = []

        async def spy_compute(pool, gap, **kwargs):
            called.append(True)

        spy = DerivedCapability(
            name="perception.divergence",
            arguments=DERIVED.arguments,
            compute=spy_compute,
        )
        monkeypatch.setitem(
            analysis_module._DECLARED_ANALYSES,
            "perception.divergence",
            spy,
        )

        result = await run_analysis(
            default_registry(),
            db.pool,
            name="perception.divergence",
            entity_id=entity_id,
            audience=None,
        )

        assert result.abstained
        assert called == [], "compute was called despite an argument abstention"
        reasons = {s.argument: s.reason for s in result.shortfalls}
        assert "perception_macro" in reasons
        assert "fundamental_metric" in reasons
        assert "30 of 80" in reasons["perception_macro"]
        assert "30 of 80" in reasons["fundamental_metric"]


# ----------------------------------------------- no declared arguments refused


class TestNoDeclaredArguments:
    async def test_capability_without_arguments_is_refused(self, db):
        """A real registered analysis (``backtest.evaluate_strategy_sharpe``)
        has no ``ArgumentSpec`` declaration, so it is refused with a reason --
        not silently called through ``capability.call(target)``."""
        entity_id = await _entity(db)
        registry = default_registry()
        name = "backtest.evaluate_strategy_sharpe"
        cap = registry.get(name)
        assert cap is not None, "expected this capability to exist in the registry"
        assert cap.invocable

        with pytest.raises(AppError) as exc_info:
            await run_analysis(
                registry,
                db.pool,
                name=name,
                entity_id=entity_id,
                audience=None,
            )
        assert exc_info.value.status == 400
        assert "declares no arguments" in exc_info.value.detail

    async def test_capability_call_is_never_reached(self, db):
        """Direct proof: a capability whose ``call`` is a spy is refused before
        the spy is ever invoked."""
        entity_id = await _entity(db)

        call_log: list = []

        async def spy_call(*args, **kwargs):
            call_log.append(True)

        registry = Registry()
        registry.add(
            Capability(
                name="spy.no_args",
                description="invocable but no declared arguments",
                callability=Callability.YES,
                maturity=Maturity.WIRED,
                call=spy_call,
            )
        )

        with pytest.raises(AppError) as exc_info:
            await run_analysis(
                registry,
                db.pool,
                name="spy.no_args",
                entity_id=entity_id,
                audience=None,
            )
        assert exc_info.value.status == 400
        assert call_log == [], "capability.call was reached"


# ------------------------------------------------- distinct resolution errors


class TestResolutionErrors:
    async def test_unknown_name_is_not_found(self, db):
        entity_id = await _entity(db)
        with pytest.raises(AppError) as exc_info:
            await run_analysis(
                default_registry(),
                db.pool,
                name="does.not.exist",
                entity_id=entity_id,
                audience=None,
            )
        assert exc_info.value.status == 404
        assert "does.not.exist" in exc_info.value.detail

    async def test_registered_but_not_invocable_is_bad_request(self, db):
        entity_id = await _entity(db)
        registry = Registry()
        registry.add(
            Capability(
                name="stub.needs_extraction",
                description="exists but cannot be called",
                callability=Callability.NEEDS_EXTRACTION,
                maturity=Maturity.STUB,
            )
        )

        with pytest.raises(AppError) as exc_info:
            await run_analysis(
                registry,
                db.pool,
                name="stub.needs_extraction",
                entity_id=entity_id,
                audience=None,
            )
        assert exc_info.value.status == 400
        assert "not invocable" in exc_info.value.detail
        # Distinct from the unknown-name error (404).
        assert exc_info.value.status != 404


# --------------------------------------- audience isolation (most important)


class TestAudienceIsolation:
    async def test_private_claim_of_A_is_invisible_to_B(self, db):
        """The most important test in the order. A ``perception_macro`` series
        private to ``owner`` is invisible to ``other``. The owner's request
        computes a BYO result; the other user's request abstains on
        ``perception_macro`` rather than computing from the owner's licensed
        data."""
        entity_id = await _entity(db)
        owner, other = uuid4(), uuid4()

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

        registry = default_registry()

        owner_result = await run_analysis(
            registry,
            db.pool,
            name="perception.divergence",
            entity_id=entity_id,
            audience=owner,
        )
        assert not owner_result.abstained, owner_result.shortfalls
        assert owner_result.redistributable == "byo_only"
        assert owner_result.audience_user_id == owner

        other_result = await run_analysis(
            registry,
            db.pool,
            name="perception.divergence",
            entity_id=entity_id,
            audience=other,
        )
        assert other_result.abstained
        shortfall_args = {s.argument for s in other_result.shortfalls}
        assert "perception_macro" in shortfall_args
        # The shared fundamental_metric side was visible.
        assert "fundamental_metric" not in shortfall_args


# --------------------------------------------------- licence verdict correct


class TestLicenceVerdict:
    async def test_shareable_inputs_yield_allowed_licence(self, db):
        entity_id = await _entity(db)
        await _seed_shared(db, entity_id)

        result = await run_analysis(
            default_registry(),
            db.pool,
            name="perception.divergence",
            entity_id=entity_id,
            audience=None,
        )
        assert not result.abstained
        assert result.redistributable == "allowed"
        assert result.audience_user_id is None

    async def test_byo_inputs_yield_byo_only_licence(self, db):
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

        result = await run_analysis(
            default_registry(),
            db.pool,
            name="perception.divergence",
            entity_id=entity_id,
            audience=owner,
        )
        assert not result.abstained
        assert result.redistributable == "byo_only"
        assert result.audience_user_id == owner
