"""Scheduler liveness heartbeat (audit finding 6).

Docker regards a wedged-but-alive scheduler as healthy forever. The
heartbeat file is touched only after a loop records successful progress, and
the healthcheck fails once it goes stale.
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
    heartbeat.touch_heartbeat()
    ok, _message = heartbeat.check_heartbeat(900.0)
    assert ok is True


async def test_stale_heartbeat_is_unhealthy(_heartbeat_in_tmp):
    heartbeat.touch_heartbeat()
    stale = time.time() - 1200.0
    os.utime(_heartbeat_in_tmp, (stale, stale))
    ok, message = heartbeat.check_heartbeat(900.0)
    assert ok is False
    assert "stale" in message


async def test_record_loop_health_success_touches_the_heartbeat(
    db, _heartbeat_in_tmp
):
    from omni.scheduler.health import record_loop_health

    assert heartbeat.heartbeat_age_seconds() is None
    await record_loop_health(
        db.pool,
        loop_name=f"test-loop-{uuid4().hex[:6]}",
        ok=True,
        result="progress",
        expected_interval_seconds=300.0,
    )
    assert heartbeat.heartbeat_age_seconds() is not None
    assert heartbeat.heartbeat_age_seconds() < 5.0


async def test_recorded_failure_does_not_touch_the_heartbeat(db, _heartbeat_in_tmp):
    # "The process exists" and "the background is making progress" must not
    # be the same claim: a loop that is failing must not keep the container
    # looking alive.
    from omni.scheduler.health import record_loop_health

    await record_loop_health(
        db.pool,
        loop_name=f"test-loop-{uuid4().hex[:6]}",
        ok=False,
        error="boom",
        expected_interval_seconds=300.0,
    )
    assert heartbeat.heartbeat_age_seconds() is None


async def test_check_cli_exit_codes(_heartbeat_in_tmp, monkeypatch, capsys):
    monkeypatch.setattr("omni.config.settings.scheduler_heartbeat_max_age", 900.0)
    assert heartbeat.main() == 1  # missing
    heartbeat.touch_heartbeat()
    assert heartbeat.main() == 0
    stale = time.time() - 1200.0
    os.utime(_heartbeat_in_tmp, (stale, stale))
    assert heartbeat.main() == 1
    assert "stale" in capsys.readouterr().err
