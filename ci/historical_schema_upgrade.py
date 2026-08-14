from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from neutron.nucleus.migrate import Migrator

from omni.db import connect, migrate

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
FIXTURES = ROOT / "ci" / "fixtures" / "historical"
SNAPSHOTS = FIXTURES / "snapshots.json"
DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5434/omni_v2_test"

USER_ID = "10000000-0000-0000-0000-000000000001"
ENTITY_ID = "20000000-0000-0000-0000-000000000001"
PUBLIC_CLAIM_ID = "30000000-0000-0000-0000-000000000001"
PRIVATE_CLAIM_ID = "30000000-0000-0000-0000-000000000002"
PREDICTION_ID = "40000000-0000-0000-0000-000000000001"
FINDING_ID = "50000000-0000-0000-0000-000000000001"


def _database_url(base_url: str, database: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit(parts._replace(path=f"/{database}"))


def _migration_files() -> list[tuple[int, Path]]:
    files = []
    for path in MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"):
        files.append((int(path.name.split("_", 1)[0]), path))
    return sorted(files)


async def _verify(client, expected_versions: list[int]) -> None:
    versions = await client.pool.fetch(
        "SELECT version FROM _neutron_migrations ORDER BY version"
    )
    assert [row["version"] for row in versions] == expected_versions

    counts = await client.pool.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM claim) AS claims,
            (SELECT count(*) FROM finding) AS findings,
            (SELECT count(*) FROM prediction) AS predictions,
            (SELECT count(*) FROM user_settings) AS credentials
        """
    )
    assert tuple(counts) == (2, 1, 1, 1)

    public_claim = await client.pool.fetchrow(
        """
        SELECT source, event_date::text, knowledge_date::text, confidence,
               credential_owner, redistributable::text, audience_user_id,
               value::text
        FROM claim WHERE id = $1
        """,
        PUBLIC_CLAIM_ID,
    )
    assert public_claim is not None
    assert public_claim["source"] == "sec_edgar"
    assert public_claim["event_date"] == "2024-12-31 00:00:00+00"
    assert public_claim["knowledge_date"] == "2025-02-14 13:30:00+00"
    assert public_claim["confidence"] == 0.97
    assert public_claim["credential_owner"] is None
    assert public_claim["redistributable"] == "allowed"
    assert public_claim["audience_user_id"] is None
    assert json.loads(public_claim["value"]) == {"period": "FY2024", "value": "1250000"}

    private_claim = await client.pool.fetchrow(
        """
        SELECT credential_owner, redistributable::text, audience_user_id::text,
               source, value::text
        FROM claim WHERE id = $1
        """,
        PRIVATE_CLAIM_ID,
    )
    assert private_claim is not None
    assert private_claim["credential_owner"] == USER_ID
    assert private_claim["redistributable"] == "byo_only"
    assert private_claim["audience_user_id"] == USER_ID
    assert private_claim["source"] == "polygon"
    assert json.loads(private_claim["value"]) == {"price": "42.75"}

    prediction = await client.pool.fetchrow(
        """
        SELECT claim_id::text, audience_user_id::text, method, direction::text,
               entry_price::text, upper_barrier::text, lower_barrier::text,
               outcome::text, provenance::text
        FROM prediction WHERE id = $1
        """,
        PREDICTION_ID,
    )
    assert prediction is not None
    assert prediction["claim_id"] == PRIVATE_CLAIM_ID
    assert prediction["audience_user_id"] == USER_ID
    assert prediction["method"] == "ci.historical.signal"
    assert prediction["direction"] == "up"
    assert prediction["entry_price"] == "42.75"
    assert prediction["upper_barrier"] == "47.00"
    assert prediction["lower_barrier"] == "39.00"
    assert prediction["outcome"] == "pending"
    assert json.loads(prediction["provenance"])["fixture"] == "historical-schema-upgrade"

    finding = await client.pool.fetchrow(
        """
        SELECT claim_id::text, prediction_id::text, audience_user_id::text,
               status::text, supporting::text, disconfirming::text,
               deduction_chain::text, evidence_searched
        FROM finding WHERE id = $1
        """,
        FINDING_ID,
    )
    assert finding is not None
    assert finding["claim_id"] == PRIVATE_CLAIM_ID
    assert finding["prediction_id"] == PREDICTION_ID
    assert finding["audience_user_id"] == USER_ID
    assert finding["status"] == "surfaced"
    assert json.loads(finding["supporting"]) == ["private price claim retained"]
    assert json.loads(finding["disconfirming"]) == ["short history"]
    assert json.loads(finding["deduction_chain"]) == [
        {"layer": "price", "claim_id": PRIVATE_CLAIM_ID}
    ]
    assert finding["evidence_searched"] is True

    settings = await client.pool.fetchrow(
        "SELECT data::text, updated_at::text FROM user_settings WHERE user_id = $1",
        USER_ID,
    )
    assert settings is not None
    data = json.loads(settings["data"])
    assert data == {
        "providers": {},
        "venues": {
            "questrade": {
                "enabled": False,
                "credentials": {"refresh_token": "enc:v1:synthetic-ci-ciphertext"},
            }
        },
    }
    assert settings["updated_at"] == "2025-02-14 21:03:00+00"

    user = await client.pool.fetchrow(
        "SELECT id::text, email, role FROM users WHERE id = $1", USER_ID
    )
    assert tuple(user) == (USER_ID, "historical-upgrade@invalid.example", "operator")


async def _upgrade_snapshot(
    maintenance: asyncpg.Connection,
    base_url: str,
    snapshot: dict,
    migration_files: list[tuple[int, Path]],
) -> None:
    cutoff = int(snapshot["version"])
    database = f"omni_ci_upgrade_{cutoff}_{os.getpid()}"
    database_url = _database_url(base_url, database)

    await maintenance.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = $1 AND pid <> pg_backend_pid()",
        database,
    )
    await maintenance.execute(f'DROP DATABASE IF EXISTS "{database}"')
    await maintenance.execute(f'CREATE DATABASE "{database}"')

    client = await connect(database_url)
    try:
        with tempfile.TemporaryDirectory(prefix=f"omni-migrations-{cutoff}-") as directory:
            historical = Path(directory)
            for version, path in migration_files:
                if version <= cutoff:
                    shutil.copy2(path, historical / path.name)
            applied = await Migrator(client.pool).migrate(str(historical))
        assert len(applied) == cutoff

        fixture = FIXTURES / snapshot["fixture"]
        await client.pool.execute(fixture.read_text())

        upgraded = await migrate(client)
        expected_versions = [version for version, _ in migration_files]
        assert len(upgraded) == len(expected_versions) - cutoff
        await _verify(client, expected_versions)
        print(
            f"historical schema {cutoff} upgraded with persisted records "
            f"to migration {expected_versions[-1]}"
        )
    finally:
        await client.close()
        await maintenance.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


async def main() -> None:
    base_url = os.environ.get("TEST_DATABASE_URL", DEFAULT_DATABASE_URL)
    maintenance = await asyncpg.connect(_database_url(base_url, "postgres"))
    migration_files = _migration_files()
    snapshots = json.loads(SNAPSHOTS.read_text())
    try:
        for snapshot in snapshots:
            await _upgrade_snapshot(maintenance, base_url, snapshot, migration_files)
    finally:
        await maintenance.close()


if __name__ == "__main__":
    asyncio.run(main())
