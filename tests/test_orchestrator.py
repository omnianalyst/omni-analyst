"""The orchestrator: plan, execute, and turn what it cannot do into demand."""

from uuid import uuid4

import pytest

from omni.capability.builtin import build_builtin_registry
from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.orchestrator.planner import Objective, Unsatisfiable, explain, plan
from omni.orchestrator.run import execute, record_shortfalls_as_demand


async def _ok(target, **kw):
    return {"target": target}


def _cap(name, produces, *, byo=False, cost=1.0, call=_ok):
    return Capability(
        name=name, description=name, produces=produces, touches_byo=byo,
        cost=cost, maturity=Maturity.WIRED, callability=Callability.YES, call=call,
    )


@pytest.fixture
def registry():
    r = Registry()
    r.add(_cap("free.macro", ("macro_series_point",)))
    r.add(_cap("paid.price", ("price_snapshot",), byo=True, cost=3.0))
    return r


class TestPlanning:
    def test_a_satisfiable_objective_produces_steps(self, registry):
        p = plan(Objective("macro", "GDP", ("macro_series_point",)), registry)
        assert p.satisfiable
        assert [s.capability for s in p.steps] == ["free.macro"]

    def test_cost_is_known_before_the_plan_runs(self, registry):
        p = plan(Objective("x", "AAPL", ("price_snapshot",)), registry)
        assert p.cost == 3.0

    def test_an_unproducible_type_is_a_shortfall_not_an_exception(self, registry):
        p = plan(Objective("x", "AAPL", ("news_event",)), registry)
        assert not p.satisfiable
        assert p.shortfalls[0].reason is Unsatisfiable.NO_PRODUCER

    def test_a_partial_plan_is_distinguished_from_total_failure(self, registry):
        """A partial answer plus an honest gap list beats silence."""
        p = plan(
            Objective("x", "AAPL", ("macro_series_point", "news_event")), registry
        )
        assert p.partial
        assert not p.satisfiable
        assert len(p.steps) == 1 and len(p.shortfalls) == 1


class TestLicenceAtPlanningTime:
    def test_a_shareable_objective_refuses_a_licensed_source(self, registry):
        p = plan(
            Objective("x", "AAPL", ("price_snapshot",), shareable=True), registry
        )
        assert not p.steps
        assert p.shortfalls[0].reason is Unsatisfiable.ONLY_LICENSED

    def test_the_licensed_shortfall_is_distinct_from_no_producer(self, registry):
        """Different remedies: buy a redistribution licence, versus build an
        adapter. Collapsing them would send you after the wrong one."""
        licensed = plan(
            Objective("x", "A", ("price_snapshot",), shareable=True), registry
        ).shortfalls[0]
        absent = plan(
            Objective("x", "A", ("news_event",), shareable=True), registry
        ).shortfalls[0]
        assert licensed.reason is Unsatisfiable.ONLY_LICENSED
        assert absent.reason is Unsatisfiable.NO_PRODUCER

    def test_a_private_objective_may_use_the_same_licensed_source(self, registry):
        p = plan(
            Objective("x", "AAPL", ("price_snapshot",), audience=uuid4()), registry
        )
        assert p.satisfiable
        assert p.steps[0].touches_byo


class TestBudget:
    def test_a_step_over_budget_is_refused_not_silently_run(self, registry):
        p = plan(Objective("x", "A", ("price_snapshot",), budget=1.0), registry)
        assert not p.steps
        assert p.shortfalls[0].reason is Unsatisfiable.OVER_BUDGET

    def test_budget_is_consumed_across_steps(self, registry):
        registry.add(_cap("free.two", ("news_event",), cost=1.0))
        p = plan(
            Objective("x", "A", ("macro_series_point", "news_event", "price_snapshot"),
                      budget=2.0),
            registry,
        )
        assert len(p.steps) == 2
        assert p.shortfalls[0].reason is Unsatisfiable.OVER_BUDGET


class TestExecution:
    async def test_a_plan_runs_its_steps(self, registry):
        p = plan(Objective("x", "GDP", ("macro_series_point",)), registry)
        out = await execute(p, registry)
        assert out.answered
        assert out.evidence == [{"target": "GDP"}]

    async def test_a_failing_step_is_recorded_not_raised(self, registry):
        async def boom(target, **kw):
            raise RuntimeError("upstream down")

        registry.add(_cap("bad.news", ("news_event",), call=boom))
        p = plan(Objective("x", "A", ("news_event",)), registry)
        out = await execute(p, registry)
        assert not out.answered
        assert "upstream down" in out.results[0].error

    async def test_an_unbound_capability_fails_honestly(self):
        r = Registry()
        r.add(Capability(name="ghost", description="", produces=("x",),
                         maturity=Maturity.WIRED, call=None))
        # Not invocable, so the planner will not choose it.
        assert plan(Objective("o", "t", ("x",)), r).shortfalls


class TestShortfallsBecomeDemand:
    """The tightest loop in the system: an unanswerable question is the
    definition of a coverage gap worth filling."""

    async def _entity(self, db):
        return await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','A') "
            "RETURNING id"
        )

    @pytest.fixture(autouse=True)
    async def _clean(self, db):
        await db.pool.execute("TRUNCATE entity, demand CASCADE")
        yield

    async def test_a_missing_producer_becomes_demand(self, db, registry):
        entity_id = await self._entity(db)
        p = plan(Objective("x", "AAPL", ("news_event",)), registry)
        raised = await record_shortfalls_as_demand(db.pool, p, entity_id=entity_id)
        assert len(raised) == 1
        row = await db.pool.fetchrow("SELECT claim_type, active FROM demand")
        assert row["claim_type"] == "news_event"
        assert row["active"]

    async def test_a_licensing_shortfall_does_not_become_demand(self, db, registry):
        """Fetching it again would not help; it is forbidden, not absent."""
        entity_id = await self._entity(db)
        p = plan(
            Objective("x", "AAPL", ("price_snapshot",), shareable=True), registry
        )
        raised = await record_shortfalls_as_demand(db.pool, p, entity_id=entity_id)
        assert raised == []
        assert await db.pool.fetchval("SELECT count(*) FROM demand") == 0

    async def test_a_budget_shortfall_does_not_become_demand(self, db, registry):
        entity_id = await self._entity(db)
        p = plan(Objective("x", "A", ("price_snapshot",), budget=0.5), registry)
        assert await record_shortfalls_as_demand(db.pool, p, entity_id=entity_id) == []

    async def test_execute_raises_demand_as_a_side_effect(self, db, registry):
        entity_id = await self._entity(db)
        p = plan(
            Objective("x", "AAPL", ("macro_series_point", "news_event")), registry
        )
        out = await execute(p, registry, pool=db.pool, entity_id=entity_id)
        assert out.evidence
        assert len(out.demand_raised) == 1


class TestExplain:
    def test_the_plan_is_inspectable(self, registry):
        p = plan(
            Objective("Is AAPL mispriced?", "AAPL",
                      ("macro_series_point", "news_event")),
            registry,
        )
        text = explain(p)
        assert "Is AAPL mispriced?" in text
        assert "free.macro" in text and "shared" in text
        assert "cannot answer" in text and "news_event" in text


class TestAgainstTheRealRegistry:
    def test_a_real_shareable_price_objective_is_correctly_refused(self):
        """No price feed is redistributable, so this must fail at planning."""
        p = plan(
            Objective("shared price", "AAPL", ("price_snapshot",), shareable=True),
            build_builtin_registry(),
        )
        assert p.shortfalls[0].reason is Unsatisfiable.ONLY_LICENSED

    def test_a_real_cross_domain_objective_plans(self):
        p = plan(
            Objective(
                "How does macro sentiment relate to AAPL fundamentals?", "AAPL",
                ("perception_macro", "fundamental_metric"),
            ),
            build_builtin_registry(),
        )
        assert p.satisfiable
        assert {s.capability for s in p.steps} == {
            "fred.perception", "edgar.companyfacts",
        }


class TestEntityKindRouting:
    """Found by running the planner, not by reading it: with two price
    producers and no notion of asset class, it routed an equity to CoinGecko
    because CoinGecko was cheaper."""

    def test_an_equity_is_not_routed_to_a_crypto_feed(self):
        p = plan(
            Objective("price", "AAPL", ("price_snapshot",), entity_kind="company"),
            build_builtin_registry(),
        )
        assert [s.capability for s in p.steps] == ["polygon.aggregates"]

    def test_a_crypto_asset_is_not_routed_to_an_equity_feed(self):
        p = plan(
            Objective("price", "BTC", ("price_snapshot",), entity_kind="crypto_asset"),
            build_builtin_registry(),
        )
        assert [s.capability for s in p.steps] == ["coingecko.market_chart"]

    def test_a_capability_without_declared_kinds_serves_any(self):
        """fred.series is macro; it is not tied to an asset class."""
        r = build_builtin_registry()
        for kind in ("company", "crypto_asset"):
            assert r.producing("macro_series_point", entity_kind=kind)

    def test_asking_for_the_wrong_asset_class_is_an_honest_shortfall(self):
        p = plan(
            Objective("onchain for a stock", "AAPL", ("onchain_tvl",),
                      entity_kind="company"),
            build_builtin_registry(),
        )
        assert not p.steps
        assert "none serve entity kind 'company'" in p.shortfalls[0].detail
