from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("init_secrets_ops", ROOT / "ops" / "init_secrets.py")
assert SPEC is not None and SPEC.loader is not None
init_secrets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(init_secrets)


def _values(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def test_fresh_dev_env_has_signing_secret_and_local_database(tmp_path):
    env_file = tmp_path / ".env"
    init_secrets.ensure_secrets(env_file, production=False)
    values = _values(env_file)

    assert values["DATABASE_URL"] == "postgresql://postgres:postgres@localhost:5434/omni_v2"
    assert len(values["OMNI_JWT_SECRET"]) >= 32
    assert "POSTGRES_PASSWORD" not in values
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_production_adds_database_password_without_rotating_existing_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OMNI_JWT_SECRET=existing-signing-secret-over-thirty-two-characters\nPOSTGRES_PASSWORD=\nFRED_API_KEY=operator-value\n")
    init_secrets.ensure_secrets(env_file, production=True)
    first = _values(env_file)
    init_secrets.ensure_secrets(env_file, production=True)
    second = _values(env_file)

    assert first == second
    assert first["OMNI_JWT_SECRET"] == "existing-signing-secret-over-thirty-two-characters"
    assert len(first["POSTGRES_PASSWORD"]) >= 32
    assert first["FRED_API_KEY"] == "operator-value"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
