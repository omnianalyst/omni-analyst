from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"neutron-framework==([0-9]+\.[0-9]+\.[0-9]+)")


def pinned_version(project_file: Path = ROOT / "pyproject.toml") -> str:
    project = tomllib.loads(project_file.read_text())
    matches = [
        match.group(1)
        for dependency in project["project"]["dependencies"]
        if (match := PIN.fullmatch(dependency)) is not None
    ]
    if len(matches) != 1:
        raise ValueError("expected exactly one exact neutron-framework version pin")
    return matches[0]


if __name__ == "__main__":
    print(pinned_version())
