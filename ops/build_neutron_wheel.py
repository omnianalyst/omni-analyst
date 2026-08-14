from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path

REVISION_HEADER = "X-Neutron-Revision"
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")


def _revision(value: str) -> str:
    revision = value.strip().lower()
    if not REVISION_PATTERN.fullmatch(revision):
        raise ValueError(f"revision must be a full 40-character Git commit: {value!r}")
    return revision


def source_revision(project: Path) -> str:
    project = project.resolve()
    root = Path(
        subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    relative_project = project.relative_to(root)
    dirty = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            str(relative_project),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError(f"Neutron source is dirty and cannot be identified by a commit:\n{dirty}")
    return _revision(
        subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )


def wheel_revision(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError(f"expected one METADATA file in {wheel}, found {len(metadata_names)}")
        parsed = Parser().parsestr(archive.read(metadata_names[0]).decode())
    value = parsed.get(REVISION_HEADER)
    if value is None:
        raise ValueError(f"{wheel} has no {REVISION_HEADER} metadata")
    return _revision(value)


def verify_wheel(wheel: Path, expected: str) -> str:
    expected = _revision(expected)
    actual = wheel_revision(wheel)
    if actual != expected:
        raise RuntimeError(
            f"stale Neutron wheel: expected revision {expected}, wheel contains {actual}"
        )
    return actual


def _record(files: dict[str, tuple[zipfile.ZipInfo, bytes]], record_name: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted(files):
        if name == record_name:
            continue
        data = files[name][1]
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", len(data)))
    writer.writerow((record_name, "", ""))
    return output.getvalue().encode()


def stamp_wheel(wheel: Path, revision: str) -> None:
    revision = _revision(revision)
    with zipfile.ZipFile(wheel) as archive:
        files = {info.filename: (info, archive.read(info.filename)) for info in archive.infolist()}

    metadata_names = [name for name in files if name.endswith(".dist-info/METADATA")]
    record_names = [name for name in files if name.endswith(".dist-info/RECORD")]
    if len(metadata_names) != 1 or len(record_names) != 1:
        raise ValueError("wheel must contain exactly one METADATA and one RECORD file")

    metadata_name = metadata_names[0]
    metadata = files[metadata_name][1]
    if re.search(rb"(?im)^X-Neutron-Revision:", metadata):
        raise ValueError(f"wheel already contains {REVISION_HEADER}")
    headers, separator, body = metadata.partition(b"\n\n")
    stamped = headers.rstrip(b"\n") + f"\n{REVISION_HEADER}: {revision}\n".encode()
    if separator:
        stamped += b"\n" + body
    files[metadata_name] = (files[metadata_name][0], stamped)

    record_name = record_names[0]
    files[record_name] = (files[record_name][0], _record(files, record_name))
    temporary = wheel.with_suffix(".whl.tmp")
    with zipfile.ZipFile(temporary, "w") as archive:
        for info, data in files.values():
            archive.writestr(info, data)
    temporary.replace(wheel)


def build_wheel(project: Path, output: Path) -> tuple[Path, str]:
    revision = source_revision(project)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        temporary_output = Path(temporary)
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--project",
                str(project.resolve()),
                "--out-dir",
                str(temporary_output),
            ],
            check=True,
        )
        wheels = list(temporary_output.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one Neutron wheel, found {len(wheels)}")
        stamp_wheel(wheels[0], revision)
        destination = output / wheels[0].name
        shutil.move(wheels[0], destination)
    return destination, revision


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--project", type=Path, default=Path("../../Neutron/python"))
    build.add_argument("--out-dir", type=Path, default=Path("vendor"))
    verify = commands.add_parser("verify")
    verify.add_argument("--wheel", type=Path, required=True)
    verify.add_argument("--expected", required=True)
    args = parser.parse_args()

    if args.command == "build":
        wheel, revision = build_wheel(args.project, args.out_dir)
        print(f"wheel={wheel}")
        print(f"neutron_revision={revision}")
        return 0

    print(verify_wheel(args.wheel, args.expected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
