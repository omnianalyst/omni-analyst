# Testing

## Running the suite

```bash
docker compose up -d postgres
uv sync --extra dev
uv run pytest -q
```

A single module is the usual unit of work:

```bash
uv run pytest tests/test_alerts.py -q
```

`uv sync` needs the Neutron source tree at `../../Neutron/python` — it is an
editable path dependency, not a published package. A checkout of this
repository alone cannot install its dependencies.

## TEST_DATABASE_URL

**Set it, and it is used exactly as given.** Nothing is created and nothing is
dropped. CI and `_orchestrator/run.sh` both name a database deliberately and
depend on that.

**Leave it unset, and the session gets a database of its own**, named
`omni_v2_test_<pid>` on `localhost:5434`. It is created and migrated the first
time a test asks for `db` or `database_url`, and dropped when the session ends.
A run that touches no database creates none.

This exists because pytest sessions run concurrently here as a matter of course
— the agent fleet, a gate run alongside a manual one. The per-test fixtures
`TRUNCATE`, so two sessions sharing one database delete each other's rows
mid-test. It surfaces as unique-constraint and foreign-key violations in files
neither run touched, which reads as a real defect and is not one. Two
simultaneous runs of the same four modules produced 16 and 27 spurious failures
before this; both now pass clean.

The fixtures themselves are unchanged. `TRUNCATE` between tests is correct and
fast; the bug was the shared database, not the cleanup.

A session killed before teardown leaves its database behind. It blocks nothing —
the next session has a different pid, and a session that reuses a dead pid
replaces the corpse — but they accumulate:

```bash
docker exec omni_v2_postgres psql -U postgres -tAc \
  "SELECT 'DROP DATABASE IF EXISTS \"' || datname || '\" WITH (FORCE);' \
   FROM pg_database WHERE datname LIKE 'omni_v2_test\_%'" \
  | docker exec -i omni_v2_postgres psql -U postgres
```

## Lint

`uv run ruff check src tests` is the gate, and it passes.

`ruff format --check` is **not** enforced and must not be added. The repository
is deliberately not formatter-clean: 209 of its 277 files would be reformatted.
Turning it on would make CI red on arrival and bury every real diff under a
whole-repo reformat.
