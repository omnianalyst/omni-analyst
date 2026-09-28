from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

from omni import build_info

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("neutron_package_ops", ROOT / "ops" / "neutron_package.py")
assert SPEC is not None and SPEC.loader is not None
neutron_package = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = neutron_package
SPEC.loader.exec_module(neutron_package)

OMNI_REVISION = "1" * 40
NEUTRON_VERSION = "0.1.0"
OTHER_VERSION = "0.0.9"


def test_neutron_pin_matches_locked_registry_package():
    version = neutron_package.pinned_version()
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    package = next(p for p in locked["package"] if p["name"] == "neutron-framework")

    assert version == NEUTRON_VERSION
    assert package["version"] == version
    assert package["source"] == {"registry": "https://pypi.org/simple"}


def test_locked_wheel_has_published_release_digest():
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    package = next(p for p in locked["package"] if p["name"] == "neutron-framework")
    wheel = next(w for w in package["wheels"] if w["url"].endswith("py3-none-any.whl"))

    assert wheel["hash"] == "sha256:d04695ca56f967f835a49ac70774dbf1460e6ad134bcf529a2d5a29461efbd61"
    assert package["sdist"]["hash"] == "sha256:93887220e04c021924b34d8d243ee854d1744d0d4370c66d57e88d24122bf9cd"


def test_pinned_version_rejects_unpinned_or_duplicate_dependency(tmp_path):
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\ndependencies = ["neutron-framework>=0.1.0"]\n')
    with pytest.raises(ValueError, match="exactly one exact"):
        neutron_package.pinned_version(project)

    project.write_text('[project]\ndependencies = ["neutron-framework==0.1.0", "neutron-framework==0.1.0"]\n')
    with pytest.raises(ValueError, match="exactly one exact"):
        neutron_package.pinned_version(project)


def test_runtime_verification_matches_expected_to_installed_package(monkeypatch):
    monkeypatch.setattr(build_info.metadata, "version", lambda name: NEUTRON_VERSION)
    info = build_info.build_info(
        {"OMNI_BUILD_REVISION": OMNI_REVISION, "NEUTRON_PACKAGE_VERSION": NEUTRON_VERSION}
    )

    assert info == {
        "omni_revision": OMNI_REVISION,
        "neutron_version": NEUTRON_VERSION,
        "installed_neutron_version": NEUTRON_VERSION,
        "verified": True,
    }


def test_runtime_verification_refuses_stale_package_but_local_editable_stays_available(monkeypatch):
    monkeypatch.setattr(build_info.metadata, "version", lambda name: OTHER_VERSION)
    stale = build_info.build_info(
        {"OMNI_BUILD_REVISION": OMNI_REVISION, "NEUTRON_PACKAGE_VERSION": NEUTRON_VERSION}
    )
    local = build_info.build_info({})

    assert stale["verified"] is False
    assert stale["installed_neutron_version"] == OTHER_VERSION
    assert local["omni_revision"] is None
    assert local["neutron_version"] is None
    assert local["verified"] is False
