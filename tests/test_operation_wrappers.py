from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _run(tmp_path: Path, wrapper: str, *, command_exit: int):
    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / "nav_snapshot.py").write_text("nav\n")
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $* == *'python -m omni.research.launches'* ]]; then\n"
        "  echo 'launch ran'\n"
        "else\n"
        "  read -r pass\n"
        "  echo \"$pass ran\"\n"
        "fi\n"
        "exit \"$COMMAND_EXIT\"\n"
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "OMNI_ROOT": str(tmp_path),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "COMMAND_EXIT": str(command_exit),
    }
    result = subprocess.run(
        ["bash", str(ROOT / "ops" / wrapper)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    log = ops / wrapper.replace(".sh", ".log")
    return result, log.read_text()


@pytest.mark.parametrize(
    ("wrapper", "operation"),
    [("nav_snapshot.sh", "nav_snapshot"), ("launch_sweep.sh", "launch_sweep")],
)
@pytest.mark.parametrize("command_exit", [0, 29])
def test_wrapper_preserves_and_labels_command_exit(
    tmp_path, wrapper, operation, command_exit
):
    result, log = _run(tmp_path, wrapper, command_exit=command_exit)

    assert result.returncode == command_exit
    assert f"{operation} start at " in log
    if command_exit == 0:
        assert f"{operation} end exit 0 at " in log
        assert "failure" not in log
    else:
        assert f"{operation} failure exit {command_exit} at " in log
        assert f"{operation} end" not in log
