"""The background loops. Without these the system only works when asked."""

import asyncio

import pytest

from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.coverage.visibility import visible_claims
from omni.demand.ledger import direct_attention
from omni.ingest.fred import FredAdapter
from omni.scheduler.worker import (
    Scheduler,
    SchedulerConfig,
    default_registry,
    fill_once,
    sweep_once,
)

VINTAGES = [
    {"date": "2007-10-01", "realtime_start": "2008-01-30", "value": "0.6"},
    {"date": "2007-10-01", "realtime_start": "2008-03-27", "value": "-0.2"},
]


def _fill_registry() -> Registry:
    async def fake(series_id):
        return VINTAGES

    r = Registry()
    r.add(Capability(
        name="fred.series", description="FRED series",
        produces=("macro_series_point",), provider_key="fred", source="fred",
        touches_byo=False, maturity=Maturity.WIRED,
        callability=Callability.YES, call=FredAdapter(fetch_fn=fake).fetch,
    ))
    return r


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company',$1,$1) RETURNING id",
        symbol,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, demand CASCADE")
    yield


class TestSweep:
    async def test_a_sweep_records_gaps_for_unmet_demand(self, db):
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )
        assert await sweep_once(db.pool) > 0
        assert await db.pool.fetchval(
            "SELECT count(*) FROM gap WHERE resolved_at IS NULL"
        ) > 0

    async def test_sweeping_twice_does_not_duplicate_a_gap(self, db):
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )
        await sweep_once(db.pool)
        await sweep_once(db.pool)
        assert await db.pool.fetchval(
            "SELECT count(*) FROM gap WHERE resolved_at IS NULL "
            "AND claim_type='macro_series_point'"
        ) == 1

    async def test_a_sweep_with_no_demand_records_nothing(self, db):
        assert await sweep_once(db.pool) == 0


class TestFillCycle:
    async def test_a_cycle_closes_gaps_and_writes_coverage(self, db):
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )
        await sweep_once(db.pool)

        results = await fill_once(db.pool, _fill_registry(), SchedulerConfig())
        assert any(r.outcome == "filled" for r in results)
        assert await visible_claims(db.pool, audience=None) != []

    async def test_the_cycle_ceiling_is_enforced(self, db):
        """An unbounded drain against a gap engine that reopens gaps is how an
        API budget disappears in one loop iteration."""
        for i in range(8):
            e = await _entity(db, f"SYM{i}")
            await direct_attention(
                db.pool, entity_id=e, claim_type="macro_series_point", key=f"S{i}"
            )
        await sweep_once(db.pool)

        config = SchedulerConfig(max_gaps_per_cycle=3)
        results = await fill_once(db.pool, _fill_registry(), config)
        assert len(results) == 3

    async def test_an_empty_queue_returns_immediately(self, db):
        assert await fill_once(db.pool, _fill_registry(), SchedulerConfig()) == []


class TestSchedulerLoops:
    async def test_the_loops_close_the_whole_cycle_unattended(self, db):
        """The point of the module: nobody calls anything, coverage appears."""
        entity_id = await _entity(db)
        await direct_attention(
            db.pool, entity_id=entity_id, claim_type="macro_series_point", key="GDP"
        )

        scheduler = Scheduler(
            db.pool, _fill_registry(),
            SchedulerConfig(sweep_interval=0.05, fill_interval=0.05, fill_workers=1),
        )
        await scheduler.start()
        try:
            # Poll the same observable the assertions check. `stats.filled` is
            # incremented by the fill loop only after a completed cycle has
            # written its claim, so it is set strictly later than
            # `visible_claims`. Polling that earlier proxy and then stopping the
            # scheduler cancels the cycle mid-flight, before it records its
            # outcome — so `filled` would still be 0 and the test would pass or
            # fail on scheduling order. Contention widens that window; it does
            # not create it. Polling the recorded outcome makes both assertions
            # describe the same moment, so the test cannot race itself.
            deadline = asyncio.get_event_loop().time() + 30
            while asyncio.get_event_loop().time() < deadline:
                if scheduler.stats.filled >= 1:
                    break
                await asyncio.sleep(0.05)
        finally:
            await scheduler.stop()

        assert await visible_claims(db.pool, audience=None), (
            f"no coverage appeared; stats={scheduler.stats}"
        )
        assert scheduler.stats.filled >= 1

    async def test_a_failing_sweep_does_not_kill_the_loop(self, db):
        """Stopping silently would leave the system looking healthy while
        coverage quietly stopped updating."""
        class Broken:
            async def fetch(self, *a, **k):
                raise RuntimeError("db gone")
            def __getattr__(self, n):
                raise RuntimeError("db gone")

        scheduler = Scheduler(
            Broken(), _fill_registry(),
            SchedulerConfig(sweep_interval=0.02, fill_interval=0.02, fill_workers=1),
        )
        await scheduler.start()
        await asyncio.sleep(0.15)
        running = [t for t in scheduler._tasks if not t.done()]
        await scheduler.stop()
        assert running, "the loops died on an error instead of retrying"

    async def test_stop_is_clean_and_idempotent(self, db):
        scheduler = Scheduler(db.pool, _fill_registry(), SchedulerConfig())
        await scheduler.start()
        await scheduler.stop()
        await scheduler.stop()
        assert scheduler._tasks == []


class TestDefaultRegistry:
    def test_the_default_registry_is_everything_runnable(self):
        r = default_registry()
        assert len(r) == r.summary()["invocable"]
        assert len(r) == 129

    def test_derived_capabilities_are_reachable_through_default_registry(self):
        # The defect this catches: build_derived_registry() was never merged, so
        # producing("perception_divergence") on the registry the scheduler builds
        # returned nothing. Reaching it through default_registry -- not through
        # build_derived_registry directly -- is what makes the gap visible.
        r = default_registry()
        producers = r.producing("perception_divergence")
        assert [c.name for c in producers] == ["perception.divergence"]
        # And it is invocable through the merged registry, not just present.
        capability = r.get("perception.divergence")
        assert capability is not None
        assert capability.call is not None
        assert capability.invocable

    def test_yield_curve_signal_is_reachable_through_default_registry(self):
        # D10's earned claim type must resolve through the registry the
        # scheduler actually builds -- the same discipline the divergence test
        # above enforces. producing() is how the planner and fill dispatcher
        # select, so a claim type no capability produces is unreachable.
        r = default_registry()
        producers = r.producing("yield_curve_signal")
        assert [c.name for c in producers] == ["macro.yield_curve_signal"]
        capability = r.get("macro.yield_curve_signal")
        assert capability is not None
        assert capability.call is not None
        assert capability.invocable

    def test_sahm_rule_signal_is_reachable_through_default_registry(self):
        # D14's earned claim type must resolve through the registry the
        # scheduler actually builds -- the same discipline the divergence and
        # yield-curve tests above enforce. No worker.py edit is needed: D3
        # already merged build_derived_registry() into default_registry(), and
        # the sahm capability is registered there, so it flows through
        # automatically.
        r = default_registry()
        producers = r.producing("sahm_rule_signal")
        assert [c.name for c in producers] == ["macro.sahm_rule_signal"]
        capability = r.get("macro.sahm_rule_signal")
        assert capability is not None
        assert capability.call is not None
        assert capability.invocable

    def test_inflation_signal_is_reachable_through_default_registry(self):
        # The inflation signal's earned claim type must resolve through the
        # registry the scheduler actually builds -- the same discipline the
        # divergence, yield-curve and sahm tests above enforce. No worker.py
        # edit is needed: D3 already merged build_derived_registry() into
        # default_registry(), and the inflation capability is registered there,
        # so it flows through automatically.
        r = default_registry()
        producers = r.producing("inflation_signal")
        assert [c.name for c in producers] == ["macro.inflation_signal"]
        capability = r.get("macro.inflation_signal")
        assert capability is not None
        assert capability.call is not None
        assert capability.invocable

    def test_output_gap_signal_is_reachable_through_default_registry(self):
        # The output-gap signal's earned claim type must resolve through the
        # registry the scheduler actually builds. This is the claim type
        # macro.taylor_rule consumes, so reachability here is what makes the
        # taylor_rule composite possible.
        r = default_registry()
        producers = r.producing("output_gap_signal")
        assert [c.name for c in producers] == ["macro.output_gap_signal"]
        capability = r.get("macro.output_gap_signal")
        assert capability is not None
        assert capability.call is not None
        assert capability.invocable
