import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gate_runs_historical_upgrades_and_production_smoke():
    workflow = (ROOT / ".github" / "workflows" / "gate.yml").read_text()

    assert "uv run python ci/historical_schema_upgrade.py" in workflow
    assert "production-smoke:" in workflow
    assert "repository: neutron-build/neutron" in workflow
    assert "path: Neutron" in workflow
    assert "./ci/production_smoke.sh ../../Neutron/python" in workflow
    assert workflow.count("working-directory: omni-analyst/app-v2") >= 5


def test_historical_snapshots_are_real_migration_cutoffs_with_persisted_records():
    fixtures = ROOT / "ci" / "fixtures" / "historical"
    snapshots = json.loads((fixtures / "snapshots.json").read_text())
    versions = [snapshot["version"] for snapshot in snapshots]
    current = max(int(path.name.split("_", 1)[0]) for path in (ROOT / "migrations").glob("*.sql"))

    assert versions == [52, 58]
    assert all((ROOT / "migrations" / next(
        path.name
        for path in (ROOT / "migrations").glob(f"{version:03d}_*.sql")
    )).is_file() for version in versions)
    assert all(52 <= version < current for version in versions)

    fixture = (fixtures / "representative_coverage.sql").read_text()
    for table in ("users", "entity", "claim", "prediction", "finding", "user_settings"):
        assert f"INSERT INTO {table}" in fixture
    assert fixture.count("INSERT INTO claim") == 1
    assert "'allowed'" in fixture
    assert "'byo_only'" in fixture
    assert "enc:v1:synthetic-ci-ciphertext" in fixture
    assert "historical-upgrade@invalid.example" in fixture


def test_production_smoke_contract_is_static_and_offline():
    script = ROOT / "ci" / "production_smoke.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    source = script.read_text()
    overlay = (ROOT / "ci" / "compose.production-smoke.yml").read_text()
    environment = (ROOT / "ci" / "fixtures" / "production-smoke.env").read_text()

    assert 'ops/build_neutron_wheel.py" build' in source
    assert 'git -C "$neutron_repo" rev-parse HEAD' in source
    assert '--build-arg "OMNI_REVISION=$app_revision"' in source
    assert '--build-arg "NEUTRON_REVISION=$neutron_revision"' in source
    assert "com.omnianalyst.neutron.revision" in source
    assert "com.omnianalyst.neutron.wheel.sha256" in source
    assert '--file "$root/Dockerfile" --tag omni-api:latest' in source
    assert '--file "$root/Dockerfile.scheduler" --tag omni-scheduler:latest' in source
    assert 'up --detach --no-build postgres' in source
    assert 'up --detach --no-build api' in source
    assert 'up --detach --no-build scheduler' in source
    assert "pg_isready" in source
    assert '"http://127.0.0.1:8000/health"' in source
    assert "SELECT max(version) FROM _neutron_migrations" in source
    assert "internal: true" in overlay
    assert "COMPOSE_DISABLE_ENV_FILE=true" in source

    assert "synthetic-ci-postgres-password" in environment
    assert "synthetic-ci-jwt-secret" in environment
    for provider in (
        "FRED_API_KEY",
        "POLYGON_API_KEY",
        "COINGECKO_API_KEY",
        "ETHERSCAN_API_KEY",
        "SEC_USER_AGENT",
        "HYPERLIQUID_WALLET_ADDRESS",
        "HYPERLIQUID_PRIVATE_KEY",
    ):
        assert f"{provider}=\n" in environment
        assert f"export {provider}=\n" in source
