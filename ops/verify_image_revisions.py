from __future__ import annotations

import argparse
import json
import re
import subprocess

OMNI_LABEL = "org.opencontainers.image.revision"
NEUTRON_LABEL = "com.omnianalyst.neutron.revision"
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")


def _revision(value: str) -> str:
    revision = value.strip().lower()
    if REVISION_PATTERN.fullmatch(revision) is None:
        raise ValueError(f"revision must be a full 40-character Git commit: {value!r}")
    return revision


def verify_image(image: str, omni_revision: str, neutron_revision: str) -> dict[str, object]:
    omni_revision = _revision(omni_revision)
    neutron_revision = _revision(neutron_revision)
    inspected = json.loads(
        subprocess.run(
            ["docker", "image", "inspect", image],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    labels = inspected[0].get("Config", {}).get("Labels", {})
    if labels.get(OMNI_LABEL) != omni_revision:
        raise RuntimeError(f"{image} Omni label does not match {omni_revision}")
    if labels.get(NEUTRON_LABEL) != neutron_revision:
        raise RuntimeError(f"{image} Neutron label does not match {neutron_revision}")

    runtime = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            image,
            "-m",
            "omni.build_info",
            "--verify",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    info = json.loads(runtime.stdout)
    if info.get("omni_revision") != omni_revision:
        raise RuntimeError(f"{image} runtime Omni revision does not match {omni_revision}")
    if info.get("neutron_revision") != neutron_revision:
        raise RuntimeError(f"{image} runtime Neutron revision does not match {neutron_revision}")
    return {"image": image, **info}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("images", nargs="+")
    parser.add_argument("--omni-revision", required=True)
    parser.add_argument("--neutron-revision", required=True)
    args = parser.parse_args()
    for image in args.images:
        print(
            json.dumps(
                verify_image(image, args.omni_revision, args.neutron_revision), sort_keys=True
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
