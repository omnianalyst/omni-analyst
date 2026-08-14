from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "ops" / "backup.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _environment(tmp_path: Path, *, target: str = "backup-host:/srv/omni") -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "commands.log"
    config = tmp_path / "backup.env"
    config.write_text(
        f"OMNI_RSYNC_TARGET={target}\n"
        f"OMNI_BACKUP_DIR={tmp_path / 'backups'}\n"
        "OMNI_BACKUP_RETENTION=14\n"
        "OMNI_PG_CONTAINER=test_postgres\n"
        "PGUSER=postgres\n"
        "PGDATABASE=omni_v2\n"
    )
    _write_executable(
        bin_dir / "docker",
        """#!/usr/bin/env bash
set -u
printf 'docker %s\n' "$*" >> "$COMMAND_LOG"
if [[ "$*" == *" pg_dump "* ]]; then
  printf 'PGDMP test archive'
  exit "${DUMP_EXIT:-0}"
fi
if [[ "$*" == *"pg_restore --list"* ]]; then
  exit "${VALIDATE_EXIT:-0}"
fi
if [[ "$*" == *"pg_restore --exit-on-error"* ]]; then
  read -r _ || true
  exit "${RESTORE_EXIT:-0}"
fi
if [[ "$*" == *" psql "* ]]; then
  printf '%s\n' "${MIGRATION_VERSION:-60}"
fi
exit 0
""",
    )
    _write_executable(
        bin_dir / "rsync",
        """#!/usr/bin/env bash
printf 'rsync %s\n' "$*" >> "$COMMAND_LOG"
exit "${RSYNC_EXIT:-0}"
""",
    )
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COMMAND_LOG": str(command_log),
        "OMNI_BACKUP_CONFIG": str(config),
    }


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_backup_validates_archive_then_requires_successful_replication(tmp_path):
    env = _environment(tmp_path)

    result = _run(env, "backup")

    assert result.returncode == 0, result.stderr
    dumps = list((tmp_path / "backups").glob("omni_v2-*.dump"))
    assert len(dumps) == 1
    assert not list((tmp_path / "backups").glob("*.partial"))
    commands = (tmp_path / "commands.log").read_text()
    assert commands.index("pg_dump -Fc") < commands.index("pg_restore --list")
    assert commands.index("pg_restore --list") < commands.index("rsync -a --delete")
    assert "backup-host:/srv/omni/" in commands
    assert "created catalog-readable custom archive" in result.stdout
    assert "replicated" in result.stdout


@pytest.mark.parametrize("target", ["", "/srv/local", "localhost:/srv/omni"])
def test_backup_refuses_missing_or_local_replication_target(tmp_path, target):
    env = _environment(tmp_path, target=target)

    result = _run(env, "backup")

    assert result.returncode != 0
    assert "OMNI_RSYNC_TARGET" in result.stderr
    assert not (tmp_path / "commands.log").exists()


def test_backup_propagates_replication_failure_without_discarding_local_dump(tmp_path):
    env = {**_environment(tmp_path), "RSYNC_EXIT": "23"}

    result = _run(env, "backup")

    assert result.returncode == 23
    assert "off-box replication failed" in result.stderr
    assert len(list((tmp_path / "backups").glob("omni_v2-*.dump"))) == 1
    assert "replicated" not in result.stdout


def test_backup_rejects_archive_when_pg_restore_cannot_read_catalog(tmp_path):
    env = {**_environment(tmp_path), "VALIDATE_EXIT": "9"}

    result = _run(env, "backup")

    assert result.returncode == 9
    assert "pg_restore could not read the custom archive catalog" in result.stderr
    assert not list((tmp_path / "backups").glob("*"))
    assert "rsync" not in (tmp_path / "commands.log").read_text()


def test_validate_rejects_non_custom_archive_before_catalog_check(tmp_path):
    env = _environment(tmp_path)
    archive = tmp_path / "plain.sql"
    archive.write_text("select 1;\n")

    result = _run(env, "validate", str(archive))

    assert result.returncode != 0
    assert "archive is not PostgreSQL custom format" in result.stderr
    assert not (tmp_path / "commands.log").exists()


def test_restore_drill_restores_checks_and_drops_disposable_database(tmp_path):
    env = {**_environment(tmp_path), "OMNI_RESTORE_MIN_MIGRATION": "58"}
    archive = tmp_path / "known-good.dump"
    archive.write_bytes(b"PGDMP test archive")

    result = _run(env, "drill", str(archive))

    assert result.returncode == 0, result.stderr
    commands = (tmp_path / "commands.log").read_text().splitlines()
    createdb = next(line for line in commands if " createdb " in line)
    restore = next(line for line in commands if "pg_restore --exit-on-error" in line)
    psql = next(line for line in commands if " psql " in line)
    dropdb = next(line for line in commands if " dropdb " in line)
    database = createdb.rsplit(" ", 1)[1]
    assert database.startswith("omni_restore_verify_")
    assert database != "omni_v2"
    assert database in restore
    assert database in psql
    assert database in dropdb
    assert "restore drill passed at migration 60" in result.stdout


def test_restore_drill_propagates_restore_failure_and_drops_partial_database(tmp_path):
    env = {**_environment(tmp_path), "RESTORE_EXIT": "17"}
    archive = tmp_path / "broken.dump"
    archive.write_bytes(b"PGDMP broken archive")

    result = _run(env, "drill", str(archive))

    assert result.returncode == 17
    assert "restore failed; removed partial database" in result.stderr
    commands = (tmp_path / "commands.log").read_text()
    assert " pg_restore --exit-on-error" in commands
    assert " dropdb --if-exists" in commands
    assert " psql " not in commands


def test_restore_drill_rejects_stale_migration_and_drops_database(tmp_path):
    env = {
        **_environment(tmp_path),
        "MIGRATION_VERSION": "57",
        "OMNI_RESTORE_MIN_MIGRATION": "58",
    }
    archive = tmp_path / "stale.dump"
    archive.write_bytes(b"PGDMP stale archive")

    result = _run(env, "drill", str(archive))

    assert result.returncode != 0
    assert "restored migration 57 is older than required 58" in result.stderr
    commands = (tmp_path / "commands.log").read_text()
    assert " psql " in commands
    assert " dropdb --if-exists" in commands


def test_restore_refuses_to_overwrite_source_database(tmp_path):
    env = _environment(tmp_path)
    archive = tmp_path / "known-good.dump"
    archive.write_bytes(b"PGDMP test archive")

    result = _run(env, "restore", str(archive), "omni_v2")

    assert result.returncode != 0
    assert "refusing to restore over source database omni_v2" in result.stderr
    assert not (tmp_path / "commands.log").exists()


def test_install_writes_cron_that_invokes_versioned_script_directly(tmp_path):
    env = _environment(tmp_path)
    cron_file = tmp_path / "omni-backup.cron"
    env.update(
        {
            "OMNI_BACKUP_CRON_FILE": str(cron_file),
            "OMNI_BACKUP_LOG": str(tmp_path / "backup.log"),
        }
    )

    result = _run(env, "install")

    assert result.returncode == 0, result.stderr
    cron = cron_file.read_text()
    assert cron.startswith("SHELL=/bin/bash\n0 3 * * * root ")
    assert str(SCRIPT).replace(" ", "\\ ") + " backup" in cron
    assert "/opt/omni-backup.sh" not in cron
    assert os.access(SCRIPT, os.X_OK)
