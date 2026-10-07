"""Scheduler liveness heartbeat (audit finding 6, hardened by finding A13).

Docker regards a wedged-but-alive scheduler as healthy forever. The
heartbeat files are touched only after a loop records successful progress,
and the healthcheck fails once they go stale. Per loop, not per process:
one healthy loop must not be able to keep a wedged sibling looking alive.
"""

from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest

from omni.scheduler import heartbeat


@pytest.fixture(autouse=True)
def _heartbeat_in_tmp(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat"
    monkeypatch.setenv("OMNI_SCHEDULER_HEARTBEAT", str(path))
    yield path


async def test_missing_heartbeat_is_unhealthy(_heartbeat_in_tmp):
    ok, message = heartbeat.check_heartbeat(900.0)
    assert ok is False
    assert "missing" in message


async def test_fresh_heartbeat_is_healthy(_heartbeat_in_tmp):
    heartbeat.expect_loop("sweep")
    heartbeat.touch_heartbeat("sweep")
    ok, _message = heartbeat.check_heartbeat(900.0)
    assert ok is True


async def test_stale_heartbeat_is_unhealthy(_heartbeat_in_tmp):
    heartbeat.expect_loop("sweep")
    heartbeat.touch_heartbeat("sweep")
    stale = time.time() - 1200.0
    os.utime(_heartbeat_in_tmp / "loop-sweep", (stale, stale))
    ok, message = heartbeat.check_heartbeat(900.0)
    assert ok is False
    assert "stale" in message
    assert "sweep" in message


async def test_one_healthy_loop_does_not_mask_a_wedged_sibling(_heartbeat_in_tmp):
    # The A13 core: sweep keeps touching, delivery never completes. Under
    # the single-file heartbeat the container stayed green forever; every
    # expected loop must be fresh on its own.
    heartbeat.expect_loop("sweep")
    heartbeat.expect_loop("notification_delivery")
    heartbeat.touch_heartbeat("sweep")
    ok, message = heartbeat.check_heartbeat(900.0)
    assert ok is False, "a healthy sweep masked a loop that never completed"
    assert "notification_delivery" in message
    assert "never completed" in message


async def test_a_declared_loop_that_goes_stale_fails_the_check(_heartbeat_in_tmp):
    heartbeat.expect_loop("sweep")
    heartbeat.expect_loop("alerts")
    heartbeat.touch_heartbeat("sweep")
    heartbeat.touch_heartbeat("alerts")
    stale = time.time() - 1200.0
    os.utime(_heartbeat_in_tmp / "loop-alerts", (stale, stale))
    ok, message = heartbeat.check_heartbeat(900.0)
    assert ok is False
    assert "alerts" in message


async def test_a_pass_that_began_within_the_allowance_is_live(_heartbeat_in_tmp):
    # Pass-B F5: a delivery batch of slow sends, a long fill, a big alerts
    # pass -- all healthy work that outlasts the allowance between touches.
    # Judged by the last completed pass alone, mid-pass read as wedged.
    heartbeat.expect_loop("fill")
    heartbeat.begin_pass("fill")
    ok, _message = heartbeat.check_heartbeat(900.0)
    assert ok is True, "a pass in flight read as a wedged loop"


async def test_an_abandoned_pass_marker_goes_stale_like_a_success_does(
    _heartbeat_in_tmp,
):
    # A worker that died mid-pass leaves its marker behind; the marker must
    # decay by the same allowance, not prove liveness forever.
    heartbeat.expect_loop("fill")
    heartbeat.begin_pass("fill")
    stale = time.time() - 1200.0
    os.utime(_heartbeat_in_tmp / "running-fill", (stale, stale))
    ok, message = heartbeat.check_heartbeat(900.0)
    assert ok is False
    assert "stale" in message
    assert "fill" in message


async def test_a_fresh_marker_does_not_mask_a_wedged_sibling(_heartbeat_in_tmp):
    heartbeat.expect_loop("sweep")
    heartbeat.expect_loop("notification_delivery")
    heartbeat.begin_pass("sweep")
    ok, message = heartbeat.check_heartbeat(900.0)
    assert ok is False
    assert "notification_delivery" in message
    assert "never completed" in message


async def test_a_loop_that_begins_passes_but_fails_them_all_reads_dead(
    db, _heartbeat_in_tmp
):
    # A13 discipline under markers: recording a failed pass drops the
    # in-flight marker, or a crash-looping loop would keep the container
    # alive through its own restarts.
    from omni.scheduler.health import record_loop_health

    name = f"test-loop-{uuid4().hex[:6]}"
    heartbeat.expect_loop(name)
    heartbeat.begin_pass(name)
    await record_loop_health(
        db.pool,
        loop_name=name,
        ok=False,
        error="boom",
        expected_interval_seconds=60.0,
    )
    ok, message = heartbeat.check_heartbeat(900.0)
    assert ok is False, "a crash-looping loop kept itself alive via pass markers"
    assert "never completed" in message


async def test_a_successful_pass_clears_its_in_flight_marker(db, _heartbeat_in_tmp):
    from omni.scheduler.health import record_loop_health

    name = f"test-loop-{uuid4().hex[:6]}"
    heartbeat.expect_loop(name)
    heartbeat.begin_pass(name)
    await record_loop_health(db.pool, loop_name=name, ok=True)
    assert heartbeat.pass_age_seconds(name) is None


async def test_a_completed_pass_with_contained_failures_keeps_liveness(
    db, _heartbeat_in_tmp
):
    # Pass-B F2: the heartbeat means "a pass completed", not "a pass had
    # nothing to complain about". One user's expired venue token must not
    # fail the scheduler container's healthcheck while every loop runs.
    from omni.scheduler.health import record_loop_health

    name = "venue_reconciliation"
    heartbeat.expect_loop(name)
    await record_loop_health(
        db.pool,
        loop_name=name,
        ok=False,
        error="questrade=error: invalid refresh token",
        liveness=True,
    )
    ok, message = heartbeat.check_heartbeat(900.0)
    assert ok is True, message


async def test_the_alerts_pass_marks_progress_per_alert_without_claiming_completion(
    db, _heartbeat_in_tmp
):
    # The alerts pass is linear in alert count with no ceiling; each alert
    # evaluated is a liveness milestone, so a long healthy pass does not
    # read as stale between touches. Pass-C C2: the milestone refreshes the
    # in-flight marker, NOT the success file -- "completed a pass" must stay
    # the success file's meaning, or a pass aborted right after a milestone
    # reads healthy with zero completed passes.
    import json

    from omni.scheduler.worker import evaluate_alerts_once

    await db.pool.execute("TRUNCATE entity, users CASCADE")
    user = await db.pool.fetchval(
        "INSERT INTO users (email, password_hash) "
        "VALUES ('hb-alerts@example.com', 'x') RETURNING id"
    )
    entity = await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('company', 'HB', 'HB') RETURNING id"
    )
    for threshold in (100, 200):
        await db.pool.execute(
            "INSERT INTO alert (user_id, entity_id, claim_type, condition) "
            "VALUES ($1, $2, 'price_snapshot', $3::jsonb)",
            user,
            entity,
            json.dumps({"kind": "value_above", "threshold": threshold}),
        )

    fired = await evaluate_alerts_once(db.pool)
    assert fired == 0  # no claims exist: nothing fires, the pass still ran
    assert heartbeat.heartbeat_age_seconds("alerts") is None, (
        "a mid-pass milestone touched the success file"
    )
    age = heartbeat.pass_age_seconds("alerts")
    assert age is not None and age < 5.0, (
        "a slow-but-working alerts pass read as never having progressed"
    )


async def test_the_fill_pass_marks_progress_without_claiming_completion(
    db, monkeypatch, _heartbeat_in_tmp
):
    # The third milestone call site: fill_once wires drain's on_progress.
    # It must refresh the marker, not the success file, for the same reason
    # as the alerts milestone above.
    from omni.scheduler import worker
    from omni.scheduler.worker import SchedulerConfig, fill_once

    async def fake_drain(
        pool, *, registry, worker_id, max_gaps, licensed, on_progress
    ):
        on_progress()
        return []

    monkeypatch.setattr(worker, "drain", fake_drain)

    await fill_once(db.pool, registry=None, config=SchedulerConfig())

    assert heartbeat.heartbeat_age_seconds("fill") is None, (
        "a mid-pass milestone touched the success file"
    )
    age = heartbeat.pass_age_seconds("fill")
    assert age is not None and age < 5.0, (
        "a slow-but-working fill pass read as never having progressed"
    )


async def test_a_slow_scheduled_loop_is_judged_against_its_own_interval(
    _heartbeat_in_tmp,
):
    # autonomous.macro runs daily; a fixed 900s bound would flag it stale
    # between passes forever. Three intervals is the allowance.
    heartbeat.expect_loop("autonomous.macro")
    heartbeat.touch_heartbeat("autonomous.macro")
    two_hours = time.time() - 2 * 3600
    os.utime(_heartbeat_in_tmp / "loop-autonomous.macro", (two_hours, two_hours))
    ok, _message = heartbeat.check_heartbeat(900.0)
    assert ok is True, "a daily loop was flagged stale hours before its next pass"


async def test_record_loop_health_success_touches_the_heartbeat(
    db, _heartbeat_in_tmp
):
    from omni.scheduler.health import record_loop_health

    name = f"test-loop-{uuid4().hex[:6]}"
    heartbeat.expect_loop(name)
    assert heartbeat.heartbeat_age_seconds(name) is None
    await record_loop_health(
        db.pool,
        loop_name=name,
        ok=True,
        result="progress",
        expected_interval_seconds=300.0,
    )
    assert heartbeat.heartbeat_age_seconds(name) is not None
    assert heartbeat.heartbeat_age_seconds(name) < 5.0


async def test_recorded_failure_does_not_touch_the_heartbeat(db, _heartbeat_in_tmp):
    from omni.scheduler.health import record_loop_health

    name = f"test-loop-{uuid4().hex[:6]}"
    heartbeat.expect_loop(name)
    await record_loop_health(
        db.pool,
        loop_name=name,
        ok=False,
        error="boom",
        expected_interval_seconds=300.0,
    )
    assert heartbeat.heartbeat_age_seconds(name) is None


async def test_check_cli_exit_codes(_heartbeat_in_tmp, monkeypatch, capsys):
    monkeypatch.setattr("omni.config.settings.scheduler_heartbeat_max_age", 900.0)
    assert heartbeat.main() == 1  # nothing declared
    heartbeat.expect_loop("sweep")
    assert heartbeat.main() == 1  # declared, never completed
    heartbeat.touch_heartbeat("sweep")
    assert heartbeat.main() == 0
    stale = time.time() - 1200.0
    os.utime(_heartbeat_in_tmp / "loop-sweep", (stale, stale))
    assert heartbeat.main() == 1
    assert "stale" in capsys.readouterr().err
