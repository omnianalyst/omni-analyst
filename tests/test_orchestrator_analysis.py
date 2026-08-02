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

from omni.capability.arguments import AnalysisOutputSpec, ArgumentSpec
from omni.capability.derived import DERIVED
from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.fill.derived import DerivedCapability
from omni.orchestrator.analysis import DeclaredAnalysis, run_analysis
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


# ----------------------------------------- credit_risk shareable resolution
#
# QF1: market_risk.credit_risk is registered touches_byo=True (the safe
# direction QM left in extracted.py) because its bare-float signature hides the
# source. Declaring its two spreads as ArgumentSpecs lets run_analysis resolve
# the licence from the materialized claims -- so a FRED-sourced (allowed) call
# returns "allowed" even though the static descriptor still says byo. The static
# entry is left exactly as QM left it; this path overrides the per-call verdict.


class TestCreditRiskShareable:
    async def test_fred_sourced_spreads_resolve_allowed(self, db):
        """The finding, fixed: both spreads sourced from FRED-tagged (allowed)
        claims resolve redistributable="allowed", even though the static
        extracted.py entry still says touches_byo=True."""
        entity_id = await _entity(db, symbol="US")
        await _insert_series(
            db, entity_id, [(BASE, 200.0)],
            claim_type="macro_series_point", key="BAMLC0A0CM",
            source="fred", redistributable="allowed", audience_user_id=None,
        )
        await _insert_series(
            db, entity_id, [(BASE, 500.0)],
            claim_type="macro_series_point", key="BAMLH0A0HYM2",
            source="fred", redistributable="allowed", audience_user_id=None,
        )

        registry = default_registry()
        result = await run_analysis(
            registry,
            db.pool,
            name="market_risk.credit_risk",
            entity_id=entity_id,
            audience=None,
        )

        assert not result.abstained, result.shortfalls
        assert result.redistributable == "allowed"
        assert result.audience_user_id is None
        # analyze_credit_risk ran on the materialized scalars: ig=200 > 180
        # (1.5 * _IG_AVG) -> score 80, proving the inputs reached compute.
        assert result.result["score"] == 80
        assert result.result["ig_spread"] == 200.0
        assert result.result["hy_spread"] == 500.0
        # The static registry entry is unchanged -- still the safe direction.
        assert registry.get("market_risk.credit_risk").touches_byo is True


class TestCreditRiskAbstention:
    async def test_one_spread_absent_abstains_and_compute_not_called(
        self, db, monkeypatch
    ):
        """With the HY spread absent (below min_obs) the call abstains naming
        hy_spread, and analyze_credit_risk is never invoked -- proven by a spy,
        not an assumption."""
        entity_id = await _entity(db, symbol="US")
        await _insert_series(
            db, entity_id, [(BASE, 200.0)],
            claim_type="macro_series_point", key="BAMLC0A0CM",
            source="fred", redistributable="allowed", audience_user_id=None,
        )
        # No BAMLH0A0HYM2 claim -- hy_spread cannot be materialized.

        from omni.orchestrator import analysis as analysis_module

        called: list = []

        async def spy_compute(**kwargs):
            called.append(kwargs)
            return {}

        spy = DeclaredAnalysis(
            name="market_risk.credit_risk",
            arguments=analysis_module._CREDIT_RISK_ARGUMENTS,
            compute=spy_compute,
        )
        monkeypatch.setitem(
            analysis_module._NON_CLAIM_ANALYSES,
            "market_risk.credit_risk",
            spy,
        )

        result = await run_analysis(
            default_registry(),
            db.pool,
            name="market_risk.credit_risk",
            entity_id=entity_id,
            audience=None,
        )

        assert result.abstained
        assert called == [], "compute was called despite an argument abstention"
        reasons = {s.argument: s.reason for s in result.shortfalls}
        assert "hy_spread" in reasons
        assert "ig_spread" not in reasons


class TestCreditRiskAudienceIsolation:
    async def test_byo_spread_resolves_to_owner_not_allowed(self, db):
        """The most important test in the order. A spread sourced from a
        byo_only/audience-scoped claim must resolve to that audience, not
        allowed -- the fix must not make every call shareable regardless of its
        real inputs. Mirrors D8's audience-isolation test."""
        entity_id = await _entity(db, symbol="US")
        owner, other = uuid4(), uuid4()

        # IG spread from a byo source, private to owner.
        await _insert_series(
            db, entity_id, [(BASE, 200.0)],
            claim_type="macro_series_point", key="BAMLC0A0CM",
            source="bloomberg", redistributable="byo_only",
            audience_user_id=owner,
        )
        # HY spread shareable (fred).
        await _insert_series(
            db, entity_id, [(BASE, 500.0)],
            claim_type="macro_series_point", key="BAMLH0A0HYM2",
            source="fred", redistributable="allowed", audience_user_id=None,
        )

        registry = default_registry()

        owner_result = await run_analysis(
            registry,
            db.pool,
            name="market_risk.credit_risk",
            entity_id=entity_id,
            audience=owner,
        )
        assert not owner_result.abstained, owner_result.shortfalls
        assert owner_result.redistributable == "byo_only"
        assert owner_result.audience_user_id == owner

        # The byo IG spread is invisible to another user, so their call
        # abstains on ig_spread rather than computing from the owner's data.
        other_result = await run_analysis(
            registry,
            db.pool,
            name="market_risk.credit_risk",
            entity_id=entity_id,
            audience=other,
        )
        assert other_result.abstained
        shortfall_args = {s.argument for s in other_result.shortfalls}
        assert "ig_spread" in shortfall_args
        assert "hy_spread" not in shortfall_args


class TestCreditRiskScope:
    async def test_other_market_risk_still_refused(self, db):
        """A capability not in the new non-claim registry (any other
        market_risk.* entry) continues to be refused exactly as before -- this
        order must not widen what is callable beyond credit_risk."""
        entity_id = await _entity(db, symbol="US")
        registry = default_registry()
        name = "market_risk.liquidity_risk"
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


# ======================================================================
# D12 -- composite over sibling outputs (analysis_output argument shape)
# ======================================================================
#
# market_risk.overall_risk_score takes five scores that are sibling
# capabilities' outputs, not claims. The mechanism below resolves a sibling
# by running it first (recursively), feeding its score into the composite.
# Abstention propagates: one sub-analysis short -> composite abstains, compute
# never called. Licence composes transitively through resolve_derived_licence
# over every sub-analysis's input rows -- one rule, not two. Cycles and
# excessive depth are refused with bad_request, not abstained.
#
# The mechanism is proven with test doubles (two trivial sub-analyses reading
# macro_series_point scalars), because none of the five REAL sub-analyses
# (breadth, growth, sentiment, correlation, geopolitical) are honestly
# declarable from claims today. The real overall_risk_score therefore abstains
# on all five -- the expected outcome, not a failure.


_TEST_SUB_A_ARGS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        name="level",
        claim_type="macro_series_point",
        key="TESTA",
        shape="scalar",
        transform="level",
        min_obs=1,
    ),
)

_TEST_SUB_B_ARGS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        name="level",
        claim_type="macro_series_point",
        key="TESTB",
        shape="scalar",
        transform="level",
        min_obs=1,
    ),
)

_TEST_COMPOSITE_ARGS: tuple[AnalysisOutputSpec, ...] = (
    AnalysisOutputSpec(name="a_score", capability="test.sub_a"),
    AnalysisOutputSpec(name="b_score", capability="test.sub_b"),
)


async def _compute_test_sub(*, level) -> dict | None:
    return {"score": level.value * 10}


async def _compute_test_composite(*, a_score, b_score) -> dict | None:
    return {"score": a_score.value + b_score.value}


def _patch_test_composite(monkeypatch, *, compute=_compute_test_composite):
    """Insert test.sub_a, test.sub_b and test.composite into _NON_CLAIM_ANALYSES."""
    from omni.orchestrator import analysis as analysis_module

    monkeypatch.setitem(
        analysis_module._NON_CLAIM_ANALYSES,
        "test.sub_a",
        DeclaredAnalysis(
            name="test.sub_a", arguments=_TEST_SUB_A_ARGS,
            compute=_compute_test_sub,
        ),
    )
    monkeypatch.setitem(
        analysis_module._NON_CLAIM_ANALYSES,
        "test.sub_b",
        DeclaredAnalysis(
            name="test.sub_b", arguments=_TEST_SUB_B_ARGS,
            compute=_compute_test_sub,
        ),
    )
    monkeypatch.setitem(
        analysis_module._NON_CLAIM_ANALYSES,
        "test.composite",
        DeclaredAnalysis(
            name="test.composite", arguments=_TEST_COMPOSITE_ARGS,
            compute=compute,
        ),
    )


def _test_registry():
    """Registry with test capabilities registered as invocable."""
    registry = Registry()

    async def _noop(*args, **kwargs):
        pass

    for name in (
        "test.composite", "test.self_ref",
        "test.cycle_a", "test.cycle_b",
    ):
        registry.add(
            Capability(
                name=name,
                description="test double",
                callability=Callability.YES,
                maturity=Maturity.WIRED,
                call=_noop,
            )
        )
    return registry


# --------------------------------- test 1: all sub-scores resolvable


class TestCompositeAllResolvable:
    async def test_composite_returns_score_and_shareable_licence(self, db, monkeypatch):
        """Both sub-analyses resolvable from allowed claims: the composite
        returns a score, and its licence resolves shareable. The score is
        a + b = 5*10 + 3*10 = 80, proving both sub-analyses' outputs reached
        the composite's compute."""
        entity_id = await _entity(db, symbol="US")
        await _insert_series(
            db, entity_id, [(BASE, 5.0)],
            claim_type="macro_series_point", key="TESTA",
            source="test", redistributable="allowed", audience_user_id=None,
        )
        await _insert_series(
            db, entity_id, [(BASE, 3.0)],
            claim_type="macro_series_point", key="TESTB",
            source="test", redistributable="allowed", audience_user_id=None,
        )

        _patch_test_composite(monkeypatch)

        result = await run_analysis(
            _test_registry(), db.pool,
            name="test.composite", entity_id=entity_id, audience=None,
        )

        assert not result.abstained, result.shortfalls
        assert result.result["score"] == 80.0
        assert result.redistributable == "allowed"
        assert result.audience_user_id is None
        assert len(result.evidence) == 2


# ------------------------- test 2: one sub-analysis short -> abstention


class TestCompositeAbstention:
    async def test_one_sub_short_abstains_and_compute_never_called(
        self, db, monkeypatch
    ):
        """THE most important test in the order. With sub_b's claim absent
        (below min_obs), the composite abstains, names the blocked
        sub-analysis, and the composite's compute is never invoked -- proven
        by a spy, not an assumption."""
        entity_id = await _entity(db, symbol="US")
        await _insert_series(
            db, entity_id, [(BASE, 5.0)],
            claim_type="macro_series_point", key="TESTA",
            source="test", redistributable="allowed", audience_user_id=None,
        )

        called: list = []

        async def spy_compute(**kwargs):
            called.append(kwargs)
            return {}

        _patch_test_composite(monkeypatch, compute=spy_compute)

        result = await run_analysis(
            _test_registry(), db.pool,
            name="test.composite", entity_id=entity_id, audience=None,
        )

        assert result.abstained
        assert called == [], (
            "composite compute was called despite a sub-analysis abstention"
        )
        shortfall_args = {s.argument for s in result.shortfalls}
        assert "b_score" in shortfall_args
        assert "a_score" not in shortfall_args


# ------------------- test 3: byo sub-score -> audience-scoped licence


class TestCompositeAudienceIsolation:
    async def test_byo_sub_score_resolves_to_owner_not_allowed(
        self, db, monkeypatch
    ):
        """One sub-score sourced from a byo_only/audience-scoped claim: the
        composite resolves to that audience, not allowed, even though the
        other input is shareable. Mirrors D8's audience-isolation test."""
        entity_id = await _entity(db, symbol="US")
        owner, other = uuid4(), uuid4()

        await _insert_series(
            db, entity_id, [(BASE, 5.0)],
            claim_type="macro_series_point", key="TESTA",
            source="bloomberg", redistributable="byo_only",
            audience_user_id=owner,
        )
        await _insert_series(
            db, entity_id, [(BASE, 3.0)],
            claim_type="macro_series_point", key="TESTB",
            source="test", redistributable="allowed", audience_user_id=None,
        )

        _patch_test_composite(monkeypatch)
        registry = _test_registry()

        owner_result = await run_analysis(
            registry, db.pool,
            name="test.composite", entity_id=entity_id, audience=owner,
        )
        assert not owner_result.abstained, owner_result.shortfalls
        assert owner_result.redistributable == "byo_only"
        assert owner_result.audience_user_id == owner

        other_result = await run_analysis(
            registry, db.pool,
            name="test.composite", entity_id=entity_id, audience=other,
        )
        assert other_result.abstained
        shortfall_args = {s.argument for s in other_result.shortfalls}
        assert "a_score" in shortfall_args
        assert "b_score" not in shortfall_args


# ------------------------- test 4: cycle and depth guards


class TestCompositeCycleGuard:
    async def test_self_referential_spec_is_refused(self, db, monkeypatch):
        """A composite that names itself as a sub-analysis is refused with a
        clear error, not recursed."""
        entity_id = await _entity(db, symbol="US")

        from omni.orchestrator import analysis as analysis_module

        monkeypatch.setitem(
            analysis_module._NON_CLAIM_ANALYSES,
            "test.self_ref",
            DeclaredAnalysis(
                name="test.self_ref",
                arguments=(
                    AnalysisOutputSpec(
                        name="x", capability="test.self_ref"
                    ),
                ),
                compute=_compute_test_sub,
            ),
        )

        with pytest.raises(AppError) as exc_info:
            await run_analysis(
                _test_registry(), db.pool,
                name="test.self_ref", entity_id=entity_id, audience=None,
            )
        assert exc_info.value.status == 400
        assert "cycle" in exc_info.value.detail

    async def test_two_capability_cycle_is_refused(self, db, monkeypatch):
        """A -> B -> A is refused, not recursed."""
        entity_id = await _entity(db, symbol="US")

        from omni.orchestrator import analysis as analysis_module

        monkeypatch.setitem(
            analysis_module._NON_CLAIM_ANALYSES,
            "test.cycle_a",
            DeclaredAnalysis(
                name="test.cycle_a",
                arguments=(
                    AnalysisOutputSpec(
                        name="b", capability="test.cycle_b"
                    ),
                ),
                compute=_compute_test_sub,
            ),
        )
        monkeypatch.setitem(
            analysis_module._NON_CLAIM_ANALYSES,
            "test.cycle_b",
            DeclaredAnalysis(
                name="test.cycle_b",
                arguments=(
                    AnalysisOutputSpec(
                        name="a", capability="test.cycle_a"
                    ),
                ),
                compute=_compute_test_sub,
            ),
        )

        with pytest.raises(AppError) as exc_info:
            await run_analysis(
                _test_registry(), db.pool,
                name="test.cycle_a", entity_id=entity_id, audience=None,
            )
        assert exc_info.value.status == 400
        assert "cycle" in exc_info.value.detail


# ---------- test 5: real overall_risk_score abstains (all five blocked)


class TestOverallRiskScoreAbstains:
    async def test_all_five_sub_analyses_blocked_abstains(self, db, monkeypatch):
        """The real market_risk.overall_risk_score abstains because none of its
        five sub-analyses are declarable from claims today. This is the
        expected outcome -- the honest partial. The spy proves
        calculate_overall_risk_score is never reached despite the composite
        being registered as callable."""
        entity_id = await _entity(db, symbol="US")

        from omni.orchestrator import analysis as analysis_module

        called: list = []

        async def spy_compute(**kwargs):
            called.append(kwargs)
            return {}

        spy = DeclaredAnalysis(
            name="market_risk.overall_risk_score",
            arguments=analysis_module._OVERALL_RISK_ARGUMENTS,
            compute=spy_compute,
        )
        monkeypatch.setitem(
            analysis_module._NON_CLAIM_ANALYSES,
            "market_risk.overall_risk_score",
            spy,
        )

        result = await run_analysis(
            default_registry(), db.pool,
            name="market_risk.overall_risk_score",
            entity_id=entity_id, audience=None,
        )

        assert result.abstained
        assert called == [], (
            "overall_risk_score compute was called despite sub-analysis abstentions"
        )
        assert len(result.shortfalls) == 5
        args = {s.argument for s in result.shortfalls}
        assert args == {
            "market_score", "economic_score", "sentiment_score",
            "correlation_score", "geopolitical_score",
        }


# --------------------- test 6: nothing else became callable


class TestOverallRiskScoreScope:
    async def test_other_market_risk_still_refused(self, db):
        """A market_risk capability not declared here (e.g. options_skew)
        is still refused exactly as before -- the order widens callable
        surface by exactly overall_risk_score, no more."""
        entity_id = await _entity(db, symbol="US")
        registry = default_registry()
        name = "market_risk.options_skew"
        cap = registry.get(name)
        assert cap is not None, "expected this capability to exist in the registry"
        assert cap.invocable

        with pytest.raises(AppError) as exc_info:
            await run_analysis(
                registry, db.pool,
                name=name, entity_id=entity_id, audience=None,
            )
        assert exc_info.value.status == 400
        assert "declares no arguments" in exc_info.value.detail


# ----------------------------------------- recession_probability (§6.3)
#
# The first composite within reach: consumes the two earned claim types
# (yield_curve_signal from D10, sahm_rule_signal from D14) as plain ArgumentSpecs
# over the claims -- NOT the analysis_output seam. lei_signals has no producer
# today, so the composite runs as an honest 2-of-3 (probability in [0, 0.7]);
# the LEI term is omitted, not fabricated.


async def _insert_signal(
    db, entity_id, *, claim_type, key, value,
    source="fred", redistributable="allowed", audience_user_id=None,
    event_date=None,
):
    """Insert one claim carrying an arbitrary JSONB ``value`` dict, returning id.

    ``_insert_series`` hardcodes ``{"value": float}``; the earned signal claim
    types carry object values (``{"is_inverted": bool, ...}``,
    ``{"triggered": bool, ...}``) that ArgumentSpec's ``value_field`` reaches
    into, so this sibling inserts the producer's real shape.
    """
    event_date = event_date or BASE
    knowledge_date = event_date + timedelta(days=1)
    return await db.pool.fetchval(
        _INSERT_CLAIM, entity_id, claim_type, key,
        json.dumps(value), source,
        event_date, knowledge_date, 1.0,
        redistributable, audience_user_id,
    )


class TestRecessionProbability:
    async def _seed(self, db, *, inverted: bool, triggered: bool):
        entity_id = await _entity(db, symbol="US")
        yc = await _insert_signal(
            db, entity_id, claim_type="yield_curve_signal", key="DGS10-DGS2",
            value={"is_inverted": inverted, "current_spread": -0.3,
                   "days_inverted_90d": 45},
        )
        sahm = await _insert_signal(
            db, entity_id, claim_type="sahm_rule_signal", key="UNRATE",
            value={"triggered": triggered, "indicator": 0.53},
        )
        return entity_id, yc, sahm

    async def _run(self, db, entity_id):
        return await run_analysis(
            default_registry(), db.pool,
            name="macro.recession_probability",
            entity_id=entity_id, audience=None,
        )

    async def test_both_signals_triggered_yields_70_percent(self, db):
        entity_id, yc, sahm = await self._seed(db, inverted=True, triggered=True)
        result = await self._run(db, entity_id)
        assert not result.abstained, result.shortfalls
        # 0.3 (yield curve) + 0.4 (sahm); the 0.3 LEI term is honestly absent
        # (no producer), so 0.7 -- not 1.0.
        assert result.result["probability"] == pytest.approx(0.7)
        assert isinstance(result.result["assessment"], str)
        assert set(result.evidence) == {str(yc), str(sahm)}

    async def test_neither_signal_yields_zero(self, db):
        entity_id, _, _ = await self._seed(db, inverted=False, triggered=False)
        result = await self._run(db, entity_id)
        assert not result.abstained
        assert result.result["probability"] == pytest.approx(0.0)

    async def test_yield_curve_only_contributes_its_term(self, db):
        entity_id, _, _ = await self._seed(db, inverted=True, triggered=False)
        result = await self._run(db, entity_id)
        assert result.result["probability"] == pytest.approx(0.3)

    async def test_sahm_only_contributes_its_term(self, db):
        entity_id, _, _ = await self._seed(db, inverted=False, triggered=True)
        result = await self._run(db, entity_id)
        assert result.result["probability"] == pytest.approx(0.4)

    async def test_abstains_when_a_signal_claim_is_absent(self, db, monkeypatch):
        """With yield_curve_signal absent the call abstains naming
        yield_curve_inverted, and compute is never invoked -- proven by a spy,
        matching the credit_risk abstention test."""
        entity_id = await _entity(db, symbol="US")
        await _insert_signal(
            db, entity_id, claim_type="sahm_rule_signal", key="UNRATE",
            value={"triggered": True, "indicator": 0.53},
        )
        # No yield_curve_signal claim.

        from omni.orchestrator import analysis as analysis_module

        called: list = []

        async def spy_compute(**kwargs):
            called.append(kwargs)
            return {}

        monkeypatch.setitem(
            analysis_module._NON_CLAIM_ANALYSES,
            "macro.recession_probability",
            DeclaredAnalysis(
                name="macro.recession_probability",
                arguments=analysis_module._RECESSION_PROBABILITY_ARGUMENTS,
                compute=spy_compute,
            ),
        )

        result = await self._run(db, entity_id)

        assert result.abstained
        assert called == [], "compute called despite an argument abstention"
        reasons = {s.argument: s.reason for s in result.shortfalls}
        assert "yield_curve_inverted" in reasons
        assert "sahm_triggered" not in reasons

    async def test_undiscovered_macro_capability_still_refused(self, db):
        """Sanity: declaring recession_probability widens the callable surface
        by exactly one -- an unrelated macro capability is still refused with
        'declares no arguments', not silently run."""
        entity_id = await _entity(db, symbol="US")
        registry = default_registry()
        name = "macro.inflation_measures"
        cap = registry.get(name)
        assert cap is not None and cap.invocable
        with pytest.raises(AppError) as exc_info:
            await run_analysis(
                registry, db.pool, name=name,
                entity_id=entity_id, audience=None,
            )
        assert exc_info.value.status == 400
        assert "declares no arguments" in exc_info.value.detail


# ----------------------------------------- inflation_expectations (non-claim)
#
# The 7th callable-by-name capability, and the second on the non-claim
# DeclaredAnalysis path (after credit_risk). 5y/10y breakeven inflation from
# FRED T5YIE/T10YIE (both verified). Non-claim because nothing today consumes
# "the current 5y5y forward" as durable coverage -- it is a computed read, not
# an accumulating asset (contrast inflation_signal, which taylor_rule consumes).


class TestInflationExpectations:
    async def _seed(self, db, *, five_y, ten_y):
        entity_id = await _entity(db, symbol="US")
        f = await _insert_signal(
            db, entity_id, claim_type="macro_series_point", key="T5YIE",
            value={"value": five_y},
        )
        t = await _insert_signal(
            db, entity_id, claim_type="macro_series_point", key="T10YIE",
            value={"value": ten_y},
        )
        return entity_id, f, t

    async def _run(self, db, entity_id):
        return await run_analysis(
            default_registry(), db.pool,
            name="macro.inflation_expectations",
            entity_id=entity_id, audience=None,
        )

    async def test_returns_breakevens_and_5y5y_forward(self, db):
        entity_id, f, t = await self._seed(db, five_y=2.3, ten_y=2.4)
        result = await self._run(db, entity_id)
        assert not result.abstained, result.shortfalls
        assert result.result["5y"] == pytest.approx(2.3)
        assert result.result["10y"] == pytest.approx(2.4)
        # 5y5y forward = 2*10y - 5y = 2.5. Discriminates a (10y-5y)=0.1 or
        # arithmetic-mean=2.35 bug.
        assert result.result["5y5y_forward"] == pytest.approx(2.5)
        assert set(result.evidence) == {str(f), str(t)}

    async def test_anchored_when_10y_near_target(self, db):
        entity_id, _, _ = await self._seed(db, five_y=2.2, ten_y=2.1)
        result = await self._run(db, entity_id)
        # abs(2.1 - 2.0) = 0.1 < 0.5 -> anchored.
        assert result.result["anchored"] is True

    async def test_not_anchored_when_10y_far_from_target(self, db):
        entity_id, _, _ = await self._seed(db, five_y=2.0, ten_y=2.8)
        result = await self._run(db, entity_id)
        # abs(2.8 - 2.0) = 0.8 >= 0.5 -> not anchored.
        assert result.result["anchored"] is False

    async def test_licence_shareable_for_fred_inputs(self, db):
        entity_id, _, _ = await self._seed(db, five_y=2.3, ten_y=2.4)
        result = await self._run(db, entity_id)
        assert result.redistributable == "allowed"
        assert result.audience_user_id is None

    async def test_abstains_when_one_series_absent(self, db, monkeypatch):
        """With T5YIE absent the call abstains naming exp_5y, and compute is
        never invoked -- proven by a spy, matching the recession_probability
        abstention test."""
        entity_id = await _entity(db, symbol="US")
        await _insert_signal(
            db, entity_id, claim_type="macro_series_point", key="T10YIE",
            value={"value": 2.4},
        )
        # No T5YIE claim.

        from omni.orchestrator import analysis as analysis_module

        called: list = []

        async def spy_compute(**kwargs):
            called.append(kwargs)
            return {}

        monkeypatch.setitem(
            analysis_module._NON_CLAIM_ANALYSES,
            "macro.inflation_expectations",
            DeclaredAnalysis(
                name="macro.inflation_expectations",
                arguments=analysis_module._INFLATION_EXPECTATIONS_ARGUMENTS,
                compute=spy_compute,
            ),
        )

        result = await self._run(db, entity_id)

        assert result.abstained
        assert called == [], "compute called despite an argument abstention"
        reasons = {s.argument: s.reason for s in result.shortfalls}
        assert "exp_5y" in reasons
        assert "exp_10y" not in reasons


# ----------------------------------------- taylor_rule (composite of two claims)
#
# The architectural payoff: a composite consuming TWO earned claim types
# (inflation_signal + output_gap_signal) -- coverage accumulation made concrete.
# Same ArgumentSpecs-over-claims pattern as recession_probability.


class TestPceInflation:
    """The 10th callable-by-name capability, non-claim. PCE YoY from FRED PCEPI
    (Monthly, verified). Hand-computed 4.0% YoY from a seeded index whose first
    value is 100 and whose 13th (a year later) is 104; the abstention path seeds
    one fewer than min_obs=13."""

    async def _seed(self, db, *, n=13, start=100.0, end=104.0):
        entity_id = await _entity(db, symbol="US")
        step = (end - start) / (n - 1) if n > 1 else 0.0
        obs = [
            (BASE + timedelta(days=31 * i), start + step * i) for i in range(n)
        ]
        ids = await _insert_series(
            db, entity_id, obs,
            claim_type="macro_series_point", key="PCEPI",
            source="fred", redistributable="allowed", audience_user_id=None,
        )
        return entity_id, ids

    async def _run(self, db, entity_id):
        return await run_analysis(
            default_registry(), db.pool,
            name="macro.pce_inflation",
            entity_id=entity_id, audience=None,
        )

    async def test_returns_yoy_vs_target_and_distance(self, db):
        entity_id, ids = await self._seed(db)
        result = await self._run(db, entity_id)
        assert not result.abstained, result.shortfalls
        # (104 - 100) / 100 * 100 = 4.0% YoY. Discriminates a MoM bug or a
        # wrong index pair (e.g. [-1]/[-12] would read one month short).
        assert result.result["yoy"] == pytest.approx(4.0)
        assert result.result["vs_target"] == pytest.approx(2.0)  # 4.0 - 2.0
        assert result.result["distance_from_target"] == pytest.approx(2.0)
        assert len(result.evidence) == 13

    async def test_licence_shareable_for_fred_input(self, db):
        entity_id, _ = await self._seed(db)
        result = await self._run(db, entity_id)
        assert result.redistributable == "allowed"
        assert result.audience_user_id is None

    async def test_abstains_below_min_obs_and_compute_not_called(self, db, monkeypatch):
        """With only 12 observations (min_obs=13) the call abstains naming pce,
        and compute is never invoked -- proven by a spy."""
        entity_id, _ = await self._seed(db, n=12)

        from omni.orchestrator import analysis as analysis_module

        called: list = []

        async def spy_compute(**kwargs):
            called.append(kwargs)
            return {}

        monkeypatch.setitem(
            analysis_module._NON_CLAIM_ANALYSES,
            "macro.pce_inflation",
            DeclaredAnalysis(
                name="macro.pce_inflation",
                arguments=analysis_module._PCE_INFLATION_ARGUMENTS,
                compute=spy_compute,
            ),
        )

        result = await self._run(db, entity_id)
        assert result.abstained
        assert called == [], "compute called despite an argument abstention"
        reasons = {s.argument: s.reason for s in result.shortfalls}
        assert "pce" in reasons

    async def test_abstains_when_pcepi_absent(self, db):
        entity_id = await _entity(db, symbol="US")
        # No PCEPI claims at all.
        result = await self._run(db, entity_id)
        assert result.abstained


# ----------------------------------------- taylor_rule (composite of two claims)
#
# The architectural payoff: a composite consuming TWO earned claim types
# (inflation_signal + output_gap_signal) -- coverage accumulation made concrete.
# Same ArgumentSpecs-over-claims pattern as recession_probability.


class TestTaylorRule:
    async def _seed(self, db, *, inflation, output_gap):
        entity_id = await _entity(db, symbol="US")
        inf = await _insert_signal(
            db, entity_id, claim_type="inflation_signal", key="cpi_all",
            value={
                "yoy": inflation,
                "mom_annualized": inflation,
                "3m_annualized": inflation,
            },
        )
        og = await _insert_signal(
            db, entity_id, claim_type="output_gap_signal", key="gdpc1_gdppot",
            value={"output_gap": output_gap},
        )
        return entity_id, inf, og

    async def _run(self, db, entity_id):
        return await run_analysis(
            default_registry(), db.pool,
            name="macro.taylor_rule",
            entity_id=entity_id, audience=None,
        )

    async def test_composes_two_earned_claim_types(self, db):
        # taylor_rule = 0.5 + inf + 1.5*(inf-2.0) + 0.5*gap
        # inflation=2.5, output_gap=-1.0 -> 0.5 + 2.5 + 0.75 - 0.5 = 3.25.
        # Discriminates coefficient bugs (inflation_weight 1.0 -> 3.0; missing
        # (inf-target) term -> 2.5; output_gap_weight 1.0 -> 2.75).
        entity_id, inf, og = await self._seed(db, inflation=2.5, output_gap=-1.0)
        result = await self._run(db, entity_id)
        assert not result.abstained, result.shortfalls
        assert result.result["taylor_rate"] == pytest.approx(3.25)
        # Evidence is the two claim ids the composite read from the store.
        assert set(result.evidence) == {str(inf), str(og)}

    async def test_zero_gap_at_target_inflation(self, db):
        # inflation=2.0 (target), output_gap=0 -> 0.5 + 2.0 + 0 + 0 = 2.5
        # (neutral real rate + inflation).
        entity_id, _, _ = await self._seed(db, inflation=2.0, output_gap=0.0)
        result = await self._run(db, entity_id)
        assert result.result["taylor_rate"] == pytest.approx(2.5)

    async def test_licence_shareable_for_fred_sourced_inputs(self, db):
        entity_id, _, _ = await self._seed(db, inflation=2.5, output_gap=-1.0)
        result = await self._run(db, entity_id)
        assert result.redistributable == "allowed"
        assert result.audience_user_id is None

    async def test_abstains_when_one_signal_claim_absent(self, db, monkeypatch):
        """With output_gap_signal absent the call abstains naming output_gap,
        and compute is never invoked -- proven by a spy."""
        entity_id = await _entity(db, symbol="US")
        await _insert_signal(
            db, entity_id, claim_type="inflation_signal", key="cpi_all",
            value={
                "yoy": 2.5,
                "mom_annualized": 2.5,
                "3m_annualized": 2.5,
            },
        )
        # No output_gap_signal claim.

        from omni.orchestrator import analysis as analysis_module

        called: list = []

        async def spy_compute(**kwargs):
            called.append(kwargs)
            return {}

        monkeypatch.setitem(
            analysis_module._NON_CLAIM_ANALYSES,
            "macro.taylor_rule",
            DeclaredAnalysis(
                name="macro.taylor_rule",
                arguments=analysis_module._TAYLOR_RULE_ARGUMENTS,
                compute=spy_compute,
            ),
        )

        result = await self._run(db, entity_id)

        assert result.abstained
        assert called == [], "compute called despite an argument abstention"
        reasons = {s.argument: s.reason for s in result.shortfalls}
        assert "output_gap" in reasons
        assert "inflation" not in reasons
