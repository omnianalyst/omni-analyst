"""Container-level liveness for the scheduler process.

Loop-health rows in the database detect stale work externally, but Docker
regards a wedged scheduler as healthy for as long as the process merely
stays alive. The heartbeat closes that gap -- with one loop per file, not
one file for the whole process (audit A13): a single shared file touched by
ANY successful loop let one healthy sweep mask a delivery loop wedged
forever, and the container stayed green while half the background was dead.

The contract is now:

* ``expect_loop(name)`` -- declare a loop this process intends to run. The
  declaration is written before the loop's first pass, so a loop that never
  completes ANY pass is "expected but silent", which fails the check; a
  first-success gate alone would wait forever for a file that never appears.
* ``touch_heartbeat(name)`` -- a successful pass of loop ``name`` refreshes
  that loop's file. Failures never touch anything: a loop that is failing
  honestly must not keep the container looking alive.
* ``check_heartbeat(max_age)`` -- healthy only when EVERY expected loop's
  file exists and is fresher than ``max_age``.

The heartbeat directory is a flat directory of per-loop files next to the
manifest; nothing here reads the database, so the healthcheck stays cheap
and dependency-free.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

DEFAULT_HEARTBEAT_DIR = "/tmp/omni-scheduler-heartbeat"


def heartbeat_dir() -> Path:
    raw = os.environ.get("OMNI_SCHEDULER_HEARTBEAT", DEFAULT_HEARTBEAT_DIR)
    path = Path(raw)
    # An override pointing at the old single-file layout (an existing file)
    # is honoured by using a sibling directory instead of failing on mkdir.
    if path.is_file():
        path = path.parent / (path.name + ".d")
    return path


def _loop_path(name: str) -> Path:
    # Loop names are internal identifiers (letters, digits, dots,
    # underscores); a hostile name cannot reach this process, but the file
    # name is bounded anyway so a path separator cannot escape the dir.
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    return heartbeat_dir() / f"loop-{safe}"


def _expected_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    return heartbeat_dir() / f"expected-{safe}"


def expect_loop(name: str) -> None:
    """Declare a loop this process intends to run; see the module docstring."""
    directory = heartbeat_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _expected_path(name).touch(exist_ok=True)


def touch_heartbeat(name: str) -> None:
    directory = heartbeat_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = _loop_path(name)
    path.touch(exist_ok=True)
    os.utime(path)


def heartbeat_age_seconds(name: str) -> float | None:
    """Seconds since the loop's last successful pass, or None if never."""
    try:
        return max(0.0, time.time() - _loop_path(name).stat().st_mtime)
    except OSError:
        return None


def check_heartbeat(max_age_seconds: float) -> tuple[bool, str]:
    """Every expected loop must have a fresh success file.

    ``max_age_seconds`` is the floor. A loop with a known expected interval
    is allowed up to three of them: a daily autonomous loop is not "stale"
    six hours after its last pass, and flagging it between passes would make
    the healthcheck flap on a healthy scheduler.
    """
    from omni.scheduler.health import EXPECTED_OPERATION_INTERVALS

    directory = heartbeat_dir()
    try:
        expected = sorted(
            p.name[len("expected-"):]
            for p in directory.iterdir()
            if p.name.startswith("expected-")
        )
    except OSError:
        return False, "scheduler heartbeat missing: no loop has been declared"

    if not expected:
        return False, "scheduler heartbeat missing: no loop has been declared"

    for name in expected:
        age = heartbeat_age_seconds(name)
        if age is None:
            return False, (
                f"scheduler heartbeat missing: loop '{name}' has never "
                "completed a pass"
            )
        interval = EXPECTED_OPERATION_INTERVALS.get(name)
        allowed = max(interval * 3, max_age_seconds) if interval else max_age_seconds
        if age > allowed:
            return False, (
                f"scheduler heartbeat stale: loop '{name}' {age:.0f}s since "
                "last progress"
            )
    ages = ", ".join(f"{name} {heartbeat_age_seconds(name):.0f}s" for name in expected)
    return True, f"heartbeats fresh ({ages})"


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
    "expect_loop",
    "heartbeat_age_seconds",
    "heartbeat_dir",
    "touch_heartbeat",
]
