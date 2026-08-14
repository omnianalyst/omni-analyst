from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _run_wrapper(tmp_path: Path, *, compose_exit: int):
    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / "cycle_one.py").write_text("carry\n")
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "read -r pass\n"
        "echo \"$pass ran\"\n"
        "exit \"$COMPOSE_EXIT\"\n"
    )
    docker.chmod(0o755)
    flock = tmp_path / "flock"
    flock.write_text(
        "#!/usr/bin/env bash\n"
        "shift 2\n"
        'exec "$@"\n'
    )
    flock.chmod(0o755)
    env = {
        **os.environ,
        "OMNI_ROOT": str(tmp_path),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "COMPOSE_EXIT": str(compose_exit),
    }
    result = subprocess.run(
        ["bash", str(ROOT / "ops" / "carry_cycle.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, (ops / "carry_cycle.log").read_text()


@pytest.mark.parametrize("compose_exit", [0, 23])
def test_carry_wrapper_exposes_compose_result_and_logs_truthfully(tmp_path, compose_exit):
    result, log = _run_wrapper(tmp_path, compose_exit=compose_exit)

    assert result.returncode == compose_exit
    assert "carry ran" in log
    assert "carry_cycle start at " in log
    if compose_exit == 0:
        assert "carry_cycle end exit 0 at " in log
        assert "failure" not in log
    else:
        assert f"carry_cycle failure exit {compose_exit} at " in log
        assert "carry_cycle end" not in log
