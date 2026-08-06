"""Loop health: the signal that turns a quietly-broken scheduler into a noticed
one.

A loop that iterates but fails every cycle used to be invisible -- it swallowed
its exception into a per-error log line and kept going, so the process looked
alive while coverage stopped moving. ``record_loop_health`` persists the
failure (with reason) and the success (resetting the streak), and the scheduler
records both through ``Scheduler._do``. These tests pin the behaviour and the
discrimination: a wrong recorder (always-ok, or one that drops the error) must
fail them.
"""

import logging

import pytest

from omni.scheduler.worker import Scheduler, SchedulerConfig, record_loop_health


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE loop_health")
    yield


async def _row(pool, name="sweep"):
    return await pool.fetchrow(
        "SELECT last_success_at, last_failure_at, consecutive_failures, "
        "last_error, expected_interval_seconds FROM loop_health WHERE loop_name = $1",
        name,
    )


class TestRecordLoopHealth:
    async def test_success_stamps_last_success_and_zeroes_failures(self, db):
        await record_loop_health(
            db.pool, loop_name="sweep", ok=False, error="boom"
        )
        await record_loop_health(
            db.pool, loop_name="sweep", ok=True, expected_interval_seconds=300.0
        )
        row = await _row(db.pool)
        assert row["consecutive_failures"] == 0
        assert row["last_success_at"] is not None
        assert row["last_error"] is None
        assert float(row["expected_interval_seconds"]) == 300.0

    async def test_failure_increments_consecutive_and_captures_the_error(self, db):
        # Two distinct error strings so a recorder that ignored the argument
        # (always None) or never incremented would fail the count or the text.
        await record_loop_health(
            db.pool, loop_name="resolve", ok=False, error="first-outage"
        )
        n = await record_loop_health(
            db.pool, loop_name="resolve", ok=False, error="second-outage"
        )
        assert n == 2
        row = await _row(db.pool, "resolve")
        assert row["consecutive_failures"] == 2
        assert row["last_failure_at"] is not None
        assert row["last_success_at"] is None
        assert "second-outage" in row["last_error"]

    async def test_a_first_failure_records_one_not_zero(self, db):
        # The INSERT path must seed consecutive_failures at 1, not the column
        # default of 0 -- a failure that reads as 0 is invisible to the verdict.
        n = await record_loop_health(
            db.pool, loop_name="predict", ok=False, error="x"
        )
        assert n == 1
        assert (await _row(db.pool, "predict"))["consecutive_failures"] == 1

    async def test_a_degraded_loop_logs_a_warning_at_threshold_not_before(
        self, db, caplog
    ):
        threshold = 3  # _DEGRADED_THRESHOLD in worker.py
        caplog.set_level(logging.WARNING, logger="omni.scheduler.worker")
        for _ in range(threshold - 1):
            await record_loop_health(
                db.pool, loop_name="fill", ok=False, error="provider-500"
            )
        assert not any(
            "degraded" in rec.message and "'fill'" in rec.message
            for rec in caplog.records
        ), "below threshold a failure must stay a plain exception, not cry degraded"
        await record_loop_health(
            db.pool, loop_name="fill", ok=False, error="provider-500"
        )
        assert any(
            "degraded" in rec.message and "'fill'" in rec.message for rec in caplog.records
        )

    async def test_success_silences_a_previously_degraded_loop(self, db, caplog):
        caplog.set_level(logging.WARNING, logger="omni.scheduler.worker")
        for _ in range(4):
            await record_loop_health(db.pool, loop_name="fill", ok=False, error="x")
        caplog.clear()
        await record_loop_health(db.pool, loop_name="fill", ok=True)
        assert not any("degraded" in rec.message for rec in caplog.records)
        assert (await _row(db.pool, "fill"))["consecutive_failures"] == 0


class TestSchedulerDoWrapper:
    """The wiring: each loop's work call goes through _do, which records the
    outcome and re-raises so the loop's own except still logs the traceback."""

    def _scheduler(self, db):
        return Scheduler(db.pool, registry=None, config=SchedulerConfig())

    async def test_a_raising_work_call_is_recorded_as_failure_and_reraised(self, db):
        sched = self._scheduler(db)

        async def boom(*a, **kw):
            raise RuntimeError("sweep blew up")

        with pytest.raises(RuntimeError, match="sweep blew up"):
            await sched._do("sweep", 300.0, boom)

        row = await _row(db.pool, "sweep")
        assert row["consecutive_failures"] == 1
        assert row["last_success_at"] is None
        assert "sweep blew up" in row["last_error"]
        assert float(row["expected_interval_seconds"]) == 300.0

    async def test_a_returning_work_call_is_recorded_as_success(self, db):
        sched = self._scheduler(db)

        async def fine(*a, **kw):
            return 7

        result = await sched._do("resolve", 60.0, fine)
        assert result == 7
        row = await _row(db.pool, "resolve")
        assert row["consecutive_failures"] == 0
        assert row["last_success_at"] is not None
        assert row["last_error"] is None

    async def test_a_failure_then_success_resets_the_streak(self, db):
        sched = self._scheduler(db)

        async def boom(*a, **kw):
            raise ValueError("nope")

        async def fine(*a, **kw):
            return None

        with pytest.raises(ValueError):
            await sched._do("predict", 300.0, boom)
        await sched._do("predict", 300.0, fine)
        row = await _row(db.pool, "predict")
        assert row["consecutive_failures"] == 0
        assert row["last_success_at"] is not None
