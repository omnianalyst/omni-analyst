"""Container-level liveness for the scheduler process.

Loop-health rows in the database detect stale work externally, but Docker
regards a wedged scheduler as healthy for as long as the process merely
stays alive. The heartbeat closes that gap: every successful loop pass
recorded through ``record_loop_health`` touches a file, and the container
healthcheck fails once that file goes staler than the configured bound --
"the process exists" and "the background is making progress" stop being the
same claim.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_HEARTBEAT_PATH = "/tmp/omni-scheduler-heartbeat"


def heartbeat_path() -> Path:
    return Path(os.environ.get("OMNI_SCHEDULER_HEARTBEAT", DEFAULT_HEARTBEAT_PATH))


def touch_heartbeat() -> None:
    path = heartbeat_path()
    path.touch(exist_ok=True)
    os.utime(path)


def heartbeat_age_seconds() -> float | None:
    """Seconds since the last successful loop pass, or None if never beaten."""
    import time

    path = heartbeat_path()
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def check_heartbeat(max_age_seconds: float) -> tuple[bool, str]:
    age = heartbeat_age_seconds()
    if age is None:
        return False, "scheduler heartbeat missing: no loop has completed yet"
    if age > max_age_seconds:
        return False, f"scheduler heartbeat stale: {age:.0f}s since last progress"
    return True, f"heartbeat {age:.0f}s old"


def main() -> int:
    from omni.config import settings

    ok, message = check_heartbeat(settings.scheduler_heartbeat_max_age)
    if not ok:
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "check_heartbeat",
    "heartbeat_age_seconds",
    "heartbeat_path",
    "touch_heartbeat",
]
