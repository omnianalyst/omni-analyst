"""Autonomous bootstrap coverage for the seeded allocation funds."""

from unittest.mock import AsyncMock
from uuid import uuid4

from omni.autonomous.runner import AutonomousRunner
from omni.entities._seed_data import ALLOCATION_ETFS
from omni.entities.seed import seed_market_universe


async def test_bootstrap_creates_attributed_price_demand_for_allocation_etfs(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    await db.pool.execute("TRUNCATE entity CASCADE")
    await seed_market_universe(db.pool)

    operator = uuid4()
    await db.pool.execute(
        "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, $3)",
        operator,
        "operator@test",
        "hash",
    )
    runner = AutonomousRunner(db.pool)
    runner._operator_user_id = operator

    await runner._bootstrap_macro_demand()

    rows = await db.pool.fetch(
        "SELECT e.symbol, d.requested_by FROM demand d "
        "JOIN entity e ON e.id = d.entity_id "
        "WHERE d.claim_type = 'price_snapshot' "
        "AND e.symbol = ANY($1::text[]) "
        "ORDER BY e.symbol",
        [symbol for symbol, _ in ALLOCATION_ETFS],
    )

    assert [(row["symbol"], row["requested_by"]) for row in rows] == [
        (symbol, operator) for symbol, _ in ALLOCATION_ETFS
    ]


async def test_startup_pass_records_each_autonomous_result_and_failure(db, monkeypatch):
    await db.pool.execute("TRUNCATE loop_health")
    runner = AutonomousRunner(db.pool)
    succeeded = AsyncMock(return_value=7)
    failed = AsyncMock(side_effect=RuntimeError("macro source unavailable"))
    monkeypatch.setattr(
        runner,
        "_loops",
        lambda: [("macro", failed, 86_400.0), ("sector", succeeded, 43_200.0)],
    )

    await runner._run_all()

    rows = await db.pool.fetch(
        "SELECT loop_name, last_status, last_error, last_result, "
        "expected_interval_seconds FROM loop_health ORDER BY loop_name"
    )
    assert [tuple(row.values()) for row in rows] == [
        (
            "autonomous.macro",
            "failure",
            "RuntimeError: macro source unavailable",
            None,
            86_400.0,
        ),
        ("autonomous.sector", "success", None, "7", 43_200.0),
    ]
