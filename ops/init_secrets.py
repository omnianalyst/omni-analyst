from __future__ import annotations

import argparse
import re
import secrets
from pathlib import Path


def ensure_secrets(env_file: Path, production: bool) -> None:
    content = env_file.read_text() if env_file.exists() else ""
    if not content:
        content = "DATABASE_URL=postgresql://postgres:postgres@localhost:5434/omni_v2\nDEBUG=false\n"

    names = ["OMNI_JWT_SECRET"]
    if production:
        names.append("POSTGRES_PASSWORD")

    changed = not env_file.exists()
    for name in names:
        secret = secrets.token_urlsafe(48)
        blank = rf"(?m)^{name}=[ \t]*$"
        if re.search(blank, content):
            content = re.sub(blank, f"{name}={secret}", content)
            changed = True
        elif not re.search(rf"(?m)^{name}=", content):
            content = content.rstrip("\n") + f"\n{name}={secret}\n"
            changed = True

    if changed:
        env_file.write_text(content)
    env_file.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--production", action="store_true")
    args = parser.parse_args()
    ensure_secrets(args.env_file, args.production)


if __name__ == "__main__":
    main()
