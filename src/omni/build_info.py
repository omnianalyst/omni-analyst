from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping
from importlib import metadata

REVISION_HEADER = "X-Neutron-Revision"
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")


def build_info(environ: Mapping[str, str] | None = None) -> dict[str, str | bool | None]:
    environ = os.environ if environ is None else environ
    omni_revision = environ.get("OMNI_BUILD_REVISION")
    neutron_revision = environ.get("NEUTRON_BUILD_REVISION")
    try:
        installed_neutron_revision = metadata.metadata("neutron-py").get(REVISION_HEADER)
    except metadata.PackageNotFoundError:
        installed_neutron_revision = None

    verified = (
        omni_revision is not None
        and neutron_revision is not None
        and installed_neutron_revision is not None
        and REVISION_PATTERN.fullmatch(omni_revision) is not None
        and REVISION_PATTERN.fullmatch(neutron_revision) is not None
        and neutron_revision == installed_neutron_revision
    )
    return {
        "omni_revision": omni_revision,
        "neutron_revision": neutron_revision,
        "installed_neutron_revision": installed_neutron_revision,
        "verified": verified,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    info = build_info()
    print(json.dumps(info, sort_keys=True))
    if args.verify and not info["verified"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
