"""The capability registry: what the planner may call, and what it must not."""

import pytest

from omni.capability.from_census import EXCLUDED_ROUTERS, build_registry
from omni.capability.registry import Callability, Capability, Maturity, Registry


async def _noop(*a, **k):
    return None


def _cap(name, **kw):
    kw.setdefault("description", name)
    kw.setdefault("call", _noop)
    return Capability(name=name, **kw)


class TestInvocability:
    def test_a_descriptor_without_an_implementation_is_not_invocable(self):
        """The registry doubles as a backlog; a catalogue entry is not a tool."""
        assert not _cap("x", call=None).invocable

    def test_a_capability_needing_extraction_is_not_invocable(self):
        assert not _cap("x", callability=Callability.NEEDS_EXTRACTION).invocable

    @pytest.mark.parametrize("grade", [Maturity.FABRICATED, Maturity.ORPHANED])
    def test_fabricated_and_orphaned_are_never_invocable(self, grade):
        assert not _cap("x", maturity=grade).invocable

    def test_a_wired_bound_callable_capability_is_invocable(self):
        assert _cap("x", maturity=Maturity.WIRED).invocable

    def test_a_stub_is_invocable_but_will_rank_last(self):
        """A stub can still be called; it just should not be preferred."""
        assert _cap("x", maturity=Maturity.STUB).invocable


class TestSelection:
    def test_only_capabilities_producing_the_asked_type_are_returned(self):
        r = Registry()
        r.add(_cap("a", produces=("price_snapshot",)))
        r.add(_cap("b", produces=("news_event",)))
        assert [c.name for c in r.producing("price_snapshot")] == ["a"]

    def test_a_shareable_plan_excludes_paid_sources(self):
        """How a planner avoids tainting an answer it intends to share."""
        r = Registry()
        r.add(_cap("free", produces=("x",), touches_byo=False))
        r.add(_cap("paid", produces=("x",), touches_byo=True))
        assert [c.name for c in r.producing("x", allow_byo=False)] == ["free"]
        assert len(r.producing("x", allow_byo=True)) == 2

    def test_calibrated_capabilities_outrank_uncalibrated(self):
        r = Registry()
        r.add(_cap("proven", produces=("x",)))
        r.add(_cap("unproven", produces=("x",)))
        r.observe_reliability("proven", 0.7)
        assert [c.name for c in r.producing("x")] == ["proven", "unproven"]

    def test_a_better_hit_rate_wins(self):
        r = Registry()
        r.add(_cap("good", produces=("x",)))
        r.add(_cap("bad", produces=("x",)))
        r.observe_reliability("good", 0.8)
        r.observe_reliability("bad", 0.3)
        assert [c.name for c in r.producing("x")] == ["good", "bad"]

    def test_cheaper_breaks_a_tie(self):
        r = Registry()
        r.add(_cap("dear", produces=("x",), cost=10.0))
        r.add(_cap("cheap", produces=("x",), cost=1.0))
        assert [c.name for c in r.producing("x")] == ["cheap", "dear"]


class TestReliability:
    def test_unproven_is_none_not_zero(self):
        """A planner must distinguish 'never measured' from 'measured as bad'."""
        r = Registry()
        r.add(_cap("x"))
        assert r.reliability("x") is None

    def test_an_observed_hit_rate_is_recorded(self):
        r = Registry()
        r.add(_cap("x"))
        r.observe_reliability("x", 0.62)
        assert r.reliability("x") == pytest.approx(0.62)

    @pytest.mark.parametrize("bad", [-0.01, 1.01])
    def test_an_impossible_hit_rate_is_rejected(self, bad):
        r = Registry()
        with pytest.raises(ValueError):
            r.observe_reliability("x", bad)


class TestFromCensus:
    def test_the_census_produces_a_populated_registry(self):
        assert len(build_registry()) > 300

    def test_execution_never_becomes_a_schedulable_capability(self):
        """A planner that can place an order while answering a question is a
        different and far more dangerous product."""
        r = build_registry()
        for name in ("trading", "ai_trading", "auto_trading", "crypto_trading"):
            assert not [c for c in r._by_name if c.startswith(f"{name}.")]

    def test_auth_and_infra_are_excluded(self):
        r = build_registry()
        for name in ("auth", "users", "api_keys", "gdpr"):
            assert not [c for c in r._by_name if c.startswith(f"{name}.")]

    def test_nothing_is_invocable_until_an_implementation_is_bound(self):
        """Honest default: the census says what exists, not what v2 can run."""
        r = build_registry()
        assert r.summary()["invocable"] == 0
        assert len(r.backlog()) == len(r)

    def test_keyless_capabilities_are_not_marked_as_paid(self):
        rows = [{"router": "markets", "endpoint": "GET /a", "feature": "f",
                 "grade": "wired", "callable": "yes", "credential_path": "keyless",
                 "impl": "x.py:1"}]
        assert not build_registry(rows).get("markets.get_a").touches_byo

    def test_an_unknown_credential_path_is_treated_as_paid(self):
        """Conservative by design: over-restricting a plan is recoverable,
        redistributing someone's licensed data is not."""
        rows = [{"router": "markets", "endpoint": "GET /a", "feature": "f",
                 "grade": "wired", "callable": "yes", "credential_path": "resolver",
                 "impl": "x.py:1"}]
        assert build_registry(rows).get("markets.get_a").touches_byo

    def test_a_dual_mounted_route_yields_one_capability(self):
        """portfolio serves /portfolio and /portfolios from one handler."""
        rows = [
            {"router": "portfolio", "endpoint": "GET /api/v1/portfolio/x",
             "feature": "f", "grade": "wired", "callable": "yes",
             "credential_path": "n/a", "impl": "x.py:1"},
            {"router": "portfolio", "endpoint": "GET /api/v1/portfolios/x",
             "feature": "f", "grade": "wired", "callable": "yes",
             "credential_path": "n/a", "impl": "x.py:1"},
        ]
        assert len(build_registry(rows)) == 1

    def test_a_duplicate_name_is_rejected(self):
        r = Registry()
        r.add(_cap("x"))
        with pytest.raises(ValueError, match="duplicate"):
            r.add(_cap("x"))
