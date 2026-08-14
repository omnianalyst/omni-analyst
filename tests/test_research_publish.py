"""The research record is mirrored without a second writer, and reports the
same bar the harness would.

The defect these guard against is not a crash. It is the API quietly computing
its own significance bar from its own copy of the arithmetic, drifting from the
one `evaluate()` judged results against, and showing a plausible number that no
result was ever measured under.
"""

import asyncio
import json

import asyncpg
import pytest
from neutron.test import TestClient

from omni.main import create_app
from omni.research.publish import mirror_registry, read_history, summarise
from omni.research.registry import Registry


class _Lifespan:
    def __init__(self, app):
        self.app = app
        self.receive = asyncio.Queue()
        self.send = asyncio.Queue()

    async def __aenter__(self):
        self.task = asyncio.create_task(
            self.app({"type": "lifespan"}, self.receive.get, self.send.put)
        )
        await self.receive.put({"type": "lifespan.startup"})
        assert (await self.send.get())["type"] == "lifespan.startup.complete"
        return self.app

    async def __aexit__(self, *exc):
        await self.receive.put({"type": "lifespan.shutdown"})
        await self.send.get()
        await self.task


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    await db.pool.execute("TRUNCATE hypothesis_test")
    yield


def _registry(tmp_path, entries):
    reg = Registry(path=tmp_path / "registry.jsonl")
    for name, cells, verdict, t in entries:
        reg.record(
            name=name,
            source="test_source",
            cells=cells,
            verdict=verdict,
            detail={"best_recent_third_t": t},
        )
    return reg


async def _setup(client):
    response = await client.post(
        "/auth/setup",
        json={"email": "operator@example.com", "password": "a" * 16},
    )
    assert response.status_code == 200, response.text
    return {"authorization": f"Bearer {response.json()['token']}"}


async def _member_headers(client, operator_headers):
    created = await client.post(
        "/auth/register",
        json={"email": "member@example.com", "password": "b" * 16},
        headers=operator_headers,
    )
    assert created.status_code == 201, created.text
    login = await client.post(
        "/auth/login",
        json={"email": "member@example.com", "password": "b" * 16},
    )
    assert login.status_code == 200, login.text
    return {"authorization": f"Bearer {login.json()['token']}"}


async def test_research_record_requires_authentication(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        response = await client.get("/research/hypotheses")
    assert response.status_code == 401


async def test_research_record_refuses_authenticated_member(database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        operator_headers = await _setup(client)
        member_headers = await _member_headers(client, operator_headers)
        response = await client.get(
            "/research/hypotheses", headers=member_headers
        )
    assert response.status_code == 403


async def test_mirroring_twice_inserts_nothing_the_second_time(db, tmp_path):
    reg = _registry(tmp_path, [("a", 4, "fail", 1.1), ("b", 2, "fail", 0.4)])

    first = await mirror_registry(db.pool, registry=reg)
    second = await mirror_registry(db.pool, registry=reg)

    assert first.inserted == 2
    assert first.read == 2
    assert second.inserted == 0, "a re-run must be a no-op, not a duplicate"
    assert second.already_present == 2
    assert len(await read_history(db.pool)) == 2


async def test_a_retest_of_the_same_hypothesis_is_a_new_row(db, tmp_path):
    """Identity is (name, recorded_at), not name -- retesting is real history."""
    reg = _registry(tmp_path, [("trend.sma", 4, "fail", 1.9)])
    await mirror_registry(db.pool, registry=reg)

    reg.record(
        name="trend.sma",
        source="test_source",
        cells=4,
        verdict="fail",
        detail={"best_recent_third_t": 2.1},
    )
    report = await mirror_registry(db.pool, registry=reg)

    assert report.inserted == 1
    history = await read_history(db.pool)
    assert len(history) == 2
    assert {e["name"] for e in history} == {"trend.sma"}


async def test_a_naive_timestamp_is_refused_rather_than_shifted(db, tmp_path):
    """A naive recorded_at would shift the instant and break idempotency."""
    path = tmp_path / "registry.jsonl"
    path.write_text(
        json.dumps(
            {
                "name": "naive",
                "source": "s",
                "cells": 1,
                "verdict": "fail",
                "recorded_at": "2026-08-10T22:06:48.402039",
                "detail": {},
            }
        )
        + "\n"
    )

    with pytest.raises(ValueError, match="naive recorded_at"):
        await mirror_registry(db.pool, registry=Registry(path=path))

    assert await read_history(db.pool) == []


async def test_a_test_with_no_statistics_cannot_reach_the_table(db):
    """cells >= 1 is enforced in the schema as well as in Registry.record.

    A zero-cell row would understate the search and therefore deflate the bar
    for every result judged after it.
    """
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db.pool.execute(
            """
            INSERT INTO hypothesis_test (name, source, cells, verdict, recorded_at)
            VALUES ('empty', 's', 0, 'fail', now())
            """
        )


async def test_reported_bar_equals_the_bar_the_harness_would_apply(db, tmp_path):
    """The API must not carry its own copy of the significance arithmetic.

    This is the test that discriminates: reimplementing `sqrt(2 ln N)` in the
    API would still produce a plausible number, and only comparing it against
    the registry's own answer catches the drift.
    """
    reg = _registry(
        tmp_path,
        [("a", 12, "fail", 1.4), ("b", 30, "fail", 2.2), ("c", 7, "fail", 0.9)],
    )
    await mirror_registry(db.pool, registry=reg)

    summary = summarise(await read_history(db.pool))

    assert summary["cells"] == 49
    assert summary["bar"] == reg.bar(pending_cells=0)
    assert summary["fdr_bar"] == reg.fdr_bar(pending_cells=0)
    assert summary["bar"] > 2.5, "49 cells should exceed the null floor"


async def test_a_pass_is_not_reported_as_a_failure(db, tmp_path):
    """The harness writes 'PASS'; a case-sensitive count would lose it."""
    reg = _registry(
        tmp_path, [("winner", 4, "PASS", 4.2), ("loser", 4, "fail", 0.3)]
    )
    await mirror_registry(db.pool, registry=reg)

    summary = summarise(await read_history(db.pool))

    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["best_t"] == pytest.approx(4.2)


async def test_endpoint_serves_the_mirrored_record(database_url, db, tmp_path):
    reg = _registry(tmp_path, [("carry.crosssectional", 4, "fail", 1.8)])
    await mirror_registry(db.pool, registry=reg)

    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        headers = await _setup(client)
        response = await client.get("/research/hypotheses", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["tests"] == 1
    assert body["summary"]["cells"] == 4
    assert body["tests"][0]["name"] == "carry.crosssectional"
    assert body["tests"][0]["detail"]["best_recent_third_t"] == pytest.approx(1.8)


async def test_an_empty_record_reports_the_null_floor_not_a_made_up_bar(db):
    summary = summarise(await read_history(db.pool))

    assert summary["tests"] == 0
    assert summary["bar"] == 2.5
    assert summary["fdr_bar"] == 2.5
    assert summary["best_t"] is None
    assert summary["last_recorded_at"] is None
