"""Multi-step planning: resolving what a capability needs before running it."""

import pytest

from omni.capability.builtin import build_builtin_registry
from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.orchestrator.chain import MAX_DEPTH, plan_chain
from omni.orchestrator.planner import Objective, Unsatisfiable


async def _ok(target, **kw):
    return {"target": target}


def _cap(name, produces, consumes=(), *, byo=False, cost=1.0, kinds=()):
    return Capability(
        name=name, description=name, produces=produces, consumes=consumes,
        entity_kinds=kinds, touches_byo=byo, cost=cost,
        maturity=Maturity.WIRED, callability=Callability.YES, call=_ok,
    )


@pytest.fixture
def registry():
    r = Registry()
    r.add(_cap("price.fetch", ("price_snapshot",), byo=True))
    r.add(_cap("detect.manipulation", ("manipulation_signal",),
               consumes=("price_snapshot",), cost=0.1, byo=True))
    return r


class TestChaining:
    def test_a_derived_need_plans_its_input_first(self, registry):
        """The failure this exists for: asking for the derived thing alone
        used to report 'no capability produces this' even when everything
        needed to build it was available."""
        p = plan_chain(
            Objective("manipulation on AAPL", "AAPL", ("manipulation_signal",)),
            registry,
        )
        assert p.satisfiable
        assert [s.capability for s in p.steps] == [
            "price.fetch", "detect.manipulation",
        ]

    def test_an_input_is_planned_once_even_if_two_things_need_it(self, registry):
        registry.add(_cap("detect.other", ("news_event",),
                          consumes=("price_snapshot",), byo=True))
        p = plan_chain(
            Objective("both", "AAPL", ("manipulation_signal", "news_event")),
            registry,
        )
        assert [s.capability for s in p.steps].count("price.fetch") == 1

    def test_cost_includes_the_whole_chain(self, registry):
        p = plan_chain(
            Objective("x", "AAPL", ("manipulation_signal",)), registry
        )
        assert p.cost == pytest.approx(1.1)

    def test_a_directly_fetchable_need_still_plans_as_one_step(self, registry):
        p = plan_chain(Objective("x", "AAPL", ("price_snapshot",)), registry)
        assert len(p.steps) == 1


class TestUnresolvableChains:
    def test_a_missing_input_fails_the_whole_branch(self):
        """Scheduling a capability whose input cannot be planned would queue a
        step guaranteed to fail; refusing the branch is the honest answer."""
        r = Registry()
        r.add(_cap("detect.manipulation", ("manipulation_signal",),
                   consumes=("price_snapshot",)))
        p = plan_chain(
            Objective("x", "AAPL", ("manipulation_signal",)), r
        )
        assert p.steps == ()
        reasons = {f.detail for f in p.shortfalls}
        assert any("price_snapshot" in d for d in reasons)

    def test_a_cycle_is_reported_not_hung(self):
        """A looping chain is a registry bug, not a reason to spin."""
        r = Registry()
        r.add(_cap("a", ("A",), consumes=("B",)))
        r.add(_cap("b", ("B",), consumes=("A",)))
        p = plan_chain(Objective("x", "t", ("A",)), r)
        assert p.steps == ()
        assert any("cycle" in f.detail for f in p.shortfalls)

    def test_depth_is_bounded(self):
        """An unbounded resolver on a rich registry turns one objective into
        hundreds of steps, which is a bill rather than an answer."""
        r = Registry()
        chain = [f"T{i}" for i in range(MAX_DEPTH + 3)]
        for i, t in enumerate(chain[:-1]):
            r.add(_cap(f"c{i}", (t,), consumes=(chain[i + 1],)))
        r.add(_cap("leaf", (chain[-1],)))
        p = plan_chain(Objective("x", "t", (chain[0],)), r)
        assert p.steps == ()
        assert any("deeper than" in f.detail for f in p.shortfalls)


class TestLicenceAndBudget:
    def test_a_shareable_objective_refuses_a_licensed_input(self, registry):
        """The rule reaches into the chain: a shareable answer cannot use a
        licensed input even when the thing asked for is itself derived."""
        p = plan_chain(
            Objective("x", "AAPL", ("manipulation_signal",), shareable=True),
            registry,
        )
        assert p.steps == ()
        assert any(
            f.reason is Unsatisfiable.ONLY_LICENSED for f in p.shortfalls
        )

    def test_budget_is_applied_after_resolution_not_during(self, registry):
        """Truncating mid-chain would schedule a capability without the input
        it consumes."""
        p = plan_chain(
            Objective("x", "AAPL", ("manipulation_signal",), budget=0.5),
            registry,
        )
        assert [s.capability for s in p.steps] == []
        assert any(
            f.reason is Unsatisfiable.OVER_BUDGET for f in p.shortfalls
        )


class TestAgainstTheRealRegistry:
    def test_manipulation_chains_to_a_real_price_source(self):
        p = plan_chain(
            Objective("is AAPL being manipulated?", "AAPL",
                      ("manipulation_signal",), entity_kind="company"),
            build_builtin_registry(),
        )
        assert p.satisfiable, p.shortfalls
        assert [s.capability for s in p.steps] == [
            "polygon.aggregates", "detect.manipulation",
        ]

    def test_the_same_question_for_crypto_chains_to_coingecko(self):
        p = plan_chain(
            Objective("is BTC being manipulated?", "BTC",
                      ("manipulation_signal",), entity_kind="crypto_asset"),
            build_builtin_registry(),
        )
        assert [s.capability for s in p.steps] == [
            "coingecko.market_chart", "detect.manipulation",
        ]


def test_dropping_an_input_for_budget_drops_what_consumes_it():
    """The first implementation of the budget loop skipped only the expensive
    step, leaving a capability scheduled without its input — the very thing
    the surrounding comment claimed it prevented."""
    r = Registry()
    r.add(_cap("price.fetch", ("price_snapshot",), cost=5.0))
    r.add(_cap("detect", ("manipulation_signal",),
               consumes=("price_snapshot",), cost=0.1))
    p = plan_chain(
        Objective("x", "t", ("manipulation_signal",), budget=1.0), r
    )
    assert p.steps == ()
    details = " ".join(f.detail for f in p.shortfalls)
    assert "dropped for budget" in details
