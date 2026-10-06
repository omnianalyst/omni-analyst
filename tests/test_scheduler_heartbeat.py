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
