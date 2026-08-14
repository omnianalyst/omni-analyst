from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import sys
import zipfile
from email.parser import Parser
from pathlib import Path
from types import SimpleNamespace

import pytest

from omni import build_info

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_neutron_wheel_ops", ROOT / "ops" / "build_neutron_wheel.py"
)
assert SPEC is not None and SPEC.loader is not None
build_neutron_wheel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_neutron_wheel
SPEC.loader.exec_module(build_neutron_wheel)

NEUTRON_REVISION = "1" * 40
OTHER_REVISION = "2" * 40


def _wheel(path: Path) -> None:
    metadata = b"Metadata-Version: 2.4\nName: neutron-py\nVersion: 0.1.0\n\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("neutron/__init__.py", b"")
        archive.writestr("neutron_py-0.1.0.dist-info/METADATA", metadata)
        archive.writestr("neutron_py-0.1.0.dist-info/RECORD", b"")


def test_stamped_wheel_records_exact_revision_and_valid_record(tmp_path):
    wheel = tmp_path / "neutron_py-0.1.0-py3-none-any.whl"
    _wheel(wheel)

    build_neutron_wheel.stamp_wheel(wheel, NEUTRON_REVISION)

    assert build_neutron_wheel.wheel_revision(wheel) == NEUTRON_REVISION
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = "neutron_py-0.1.0.dist-info/METADATA"
        metadata = archive.read(metadata_name)
        assert Parser().parsestr(metadata.decode())["X-Neutron-Revision"] == NEUTRON_REVISION
        rows = {
            row[0]: row
            for row in csv.reader(
                io.StringIO(archive.read("neutron_py-0.1.0.dist-info/RECORD").decode())
            )
        }
        digest = base64.urlsafe_b64encode(hashlib.sha256(metadata).digest()).rstrip(b"=").decode()
        assert rows[metadata_name] == [metadata_name, f"sha256={digest}", str(len(metadata))]


def test_wheel_revision_rejects_missing_and_stale_metadata(tmp_path):
    wheel = tmp_path / "neutron_py-0.1.0-py3-none-any.whl"
    _wheel(wheel)
    with pytest.raises(ValueError, match="has no X-Neutron-Revision"):
        build_neutron_wheel.wheel_revision(wheel)

    build_neutron_wheel.stamp_wheel(wheel, NEUTRON_REVISION)
    with pytest.raises(RuntimeError, match=f"expected revision {OTHER_REVISION}"):
        build_neutron_wheel.verify_wheel(wheel, OTHER_REVISION)


def test_runtime_verification_matches_expected_to_installed_wheel(monkeypatch):
    monkeypatch.setattr(
        build_info.metadata,
        "metadata",
        lambda name: SimpleNamespace(get=lambda header: NEUTRON_REVISION),
    )
    info = build_info.build_info(
        {
            "OMNI_BUILD_REVISION": OTHER_REVISION,
            "NEUTRON_BUILD_REVISION": NEUTRON_REVISION,
        }
    )

    assert info == {
        "omni_revision": OTHER_REVISION,
        "neutron_revision": NEUTRON_REVISION,
        "installed_neutron_revision": NEUTRON_REVISION,
        "verified": True,
    }


def test_runtime_verification_refuses_stale_wheel_but_local_editable_stays_available(monkeypatch):
    monkeypatch.setattr(
        build_info.metadata,
        "metadata",
        lambda name: SimpleNamespace(get=lambda header: OTHER_REVISION),
    )
    stale = build_info.build_info(
        {
            "OMNI_BUILD_REVISION": OTHER_REVISION,
            "NEUTRON_BUILD_REVISION": NEUTRON_REVISION,
        }
    )
    local = build_info.build_info({})

    assert stale["verified"] is False
    assert stale["installed_neutron_revision"] == OTHER_REVISION
    assert local["omni_revision"] is None
    assert local["neutron_revision"] is None
    assert local["verified"] is False
