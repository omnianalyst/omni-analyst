"""Concurrent first-boot migrations must serialise safely (audit finding 5).

The report's premise -- that the migrator relies only on the
_neutron_migrations primary key with no advisory lock -- is stale against
the installed neutron-framework: Migrator.run_migrations wraps the whole run
(lock, versions read, every migration) in one transaction behind
pg_advisory_xact_lock. This test is the proof and the guard: two migrators
launched simultaneously against a blank database must both complete, the
schema must end up applied exactly once, and the versions table must agree
with the migration files. If a future dependency swap ever removes the
framework-side lock, this is the test that turns red.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

from omni.db import connect, migrate

_SERVER = "postgresql://postgres:postgres@localhost:5434"
_DATABASE = f"omni_v2_migrate_race_test_{os.getpid()}"


def _expected_migration_count() -> int:
    from pathlib import Path

    migrations_dir = Path(__file__).resolve().parents[1] / "migrations"
    return len([p for p in migrations_dir.iterdir() if p.suffix == ".sql"])


async def test_two_simultaneous_migrations_against_a_blank_database():
    admin = await asyncpg.connect(f"{_SERVER}/postgres")
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{_DATABASE}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{_DATABASE}"')
    finally:
        await admin.close()

    url = f"{_SERVER}/{_DATABASE}"
    try:
        client_a, client_b = await asyncio.gather(connect(url), connect(url))
        try:
            results = await asyncio.gather(
                migrate(client_a), migrate(client_b), return_exceptions=True
            )
            for result in results:
                assert not isinstance(result, BaseException), result

            for client in (client_a, client_b):
                applied = await client.pool.fetch(
                    "SELECT count(*) FROM _neutron_migrations"
                )
                assert int(applied[0]["count"]) == _expected_migration_count()
            # The users table the whole suite leans on: applied once, usable.
            for client in (client_a, client_b):
                await client.pool.execute(
                    "SELECT count(*) FROM users"
                )
        finally:
            await asyncio.gather(client_a.close(), client_b.close())
    finally:
        admin = await asyncpg.connect(f"{_SERVER}/postgres")
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{_DATABASE}" WITH (FORCE)')
        finally:
            await admin.close()
