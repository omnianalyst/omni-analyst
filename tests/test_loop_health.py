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
        "last_error, expected_interval_seconds, last_status, last_result "
        "FROM loop_health WHERE loop_name = $1",
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
        assert row["last_error"] == "boom"
        assert row["last_status"] == "success"
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
        assert row["last_status"] == "failure"
        assert "second-outage" in row["last_error"]

    async def test_result_state_is_replaced_and_bounded(self, db):
        await record_loop_health(
            db.pool,
            loop_name="nav",
            ok=True,
            result="first result",
            expected_interval_seconds=86_400,
        )
        await record_loop_health(
            db.pool,
            loop_name="nav",
            ok=True,
            result="x" * 2500,
            expected_interval_seconds=86_400,
        )

        row = await _row(db.pool, "nav")
        assert row["last_result"] == "x" * 2000
        assert await db.pool.fetchval(
            "SELECT count(*) FROM loop_health WHERE loop_name = 'nav'"
        ) == 1

    async def test_a_report_is_recorded_as_its_words_not_its_repr(self, db):
        # The scheduled-units table displayed raw dataclass reprs
        # ("SectorScanReport(scored=0, ...)") and even a bare UUID as a job
        # result. A report that can summarize itself is recorded that way.
        from omni.autonomous.sector import SectorScanReport

        await record_loop_health(
            db.pool,
            loop_name="autonomous.sector",
            ok=True,
            result=SectorScanReport(scored=3, skipped_unchanged=8),
            expected_interval_seconds=43_200,
        )

        row = await _row(db.pool, "autonomous.sector")
        assert row["last_result"] == (
            "scored 3 sector ETFs; 8 unchanged since last scan"
        )

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

    async def test_do_marks_the_pass_in_flight_before_running_it(
        self, db, monkeypatch, tmp_path
    ):
        # Pass-B F5: the marker is what lets a legitimately slow pass read
        # as live; without it the healthcheck only ever sees completed
        # passes and a long healthy pass looks wedged mid-flight.
        monkeypatch.setenv("OMNI_SCHEDULER_HEARTBEAT", str(tmp_path / "hb"))
        from omni.scheduler import heartbeat

        seen: list[float | None] = []

        async def slow(*a, **kw):
            seen.append(heartbeat.pass_age_seconds("sweep"))

        await self._scheduler(db)._do("sweep", 300.0, slow)

        assert seen and seen[0] is not None and seen[0] < 5.0
        assert heartbeat.pass_age_seconds("sweep") is None, (
            "the in-flight marker outlived the pass it was for"
        )

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

    async def test_the_marker_drops_even_when_the_health_record_cannot_commit(
        self, db, monkeypatch, tmp_path
    ):
        # Pass-C C1: the unlink used to live only inside record_loop_health,
        # so a record that could not commit left the marker behind and the
        # next begin_pass re-touched it -- a scheduler whose every pass
        # failed read healthy indefinitely.
        monkeypatch.setenv("OMNI_SCHEDULER_HEARTBEAT", str(tmp_path / "hb"))
        from omni.scheduler import heartbeat

        async def unreachable_record(*a, **kw):
            raise RuntimeError("loop_health unreachable")

        monkeypatch.setattr(
            "omni.scheduler.worker.record_loop_health", unreachable_record
        )

        async def boom(*a, **kw):
            raise RuntimeError("pass blew up")

        sched = self._scheduler(db)
        for cycle in range(3):
            # Pass-C C3: the pass's own exception must survive a record
            # that raised; the recorder's error must not replace it.
            with pytest.raises(RuntimeError, match="pass blew up"):
                await sched._do("fill", 30.0, boom)
            assert heartbeat.pass_age_seconds("fill") is None, (
                f"cycle {cycle}: the in-flight marker outlived a pass whose "
                "outcome could not be recorded"
            )
        heartbeat.expect_loop("fill")
        ok, message = heartbeat.check_heartbeat(900.0)
        assert ok is False, "every pass failed, no outcome recorded, still healthy"
        assert "never completed" in message

    async def test_a_passing_pass_drops_its_marker_when_the_record_cannot_commit(
        self, db, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("OMNI_SCHEDULER_HEARTBEAT", str(tmp_path / "hb"))
        from omni.scheduler import heartbeat

        async def unreachable_record(*a, **kw):
            raise RuntimeError("loop_health unreachable")

        monkeypatch.setattr(
            "omni.scheduler.worker.record_loop_health", unreachable_record
        )

        async def fine(*a, **kw):
            return []

        sched = self._scheduler(db)
        # The unrecorded success still propagates: the outcome is the record,
        # and swallowing it here would read a dead record as a clean pass.
        with pytest.raises(RuntimeError, match="loop_health unreachable"):
            await sched._do("fill", 30.0, fine)
        assert heartbeat.pass_age_seconds("fill") is None


class TestRunWithHealth:
    """The autonomous-tier wrapper: same marker discipline as _do."""

    async def test_the_marker_drops_even_when_the_health_record_cannot_commit(
        self, db, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("OMNI_SCHEDULER_HEARTBEAT", str(tmp_path / "hb"))
        from omni.scheduler import health, heartbeat

        async def unreachable_record(*a, **kw):
            raise RuntimeError("loop_health unreachable")

        monkeypatch.setattr(health, "record_loop_health", unreachable_record)

        async def boom():
            raise RuntimeError("pass blew up")

        with pytest.raises(RuntimeError, match="pass blew up"):
            await health.run_with_health(
                db.pool, loop_name="carry", interval=86_400.0, fn=boom
            )
        assert heartbeat.pass_age_seconds("carry") is None

    async def test_a_passing_pass_drops_its_marker_when_the_record_cannot_commit(
        self, db, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("OMNI_SCHEDULER_HEARTBEAT", str(tmp_path / "hb"))
        from omni.scheduler import health, heartbeat

        async def unreachable_record(*a, **kw):
            raise RuntimeError("loop_health unreachable")

        monkeypatch.setattr(health, "record_loop_health", unreachable_record)

        async def fine():
            return "done"

        with pytest.raises(RuntimeError, match="loop_health unreachable"):
            await health.run_with_health(
                db.pool, loop_name="carry", interval=86_400.0, fn=fine
            )
        assert heartbeat.pass_age_seconds("carry") is None
