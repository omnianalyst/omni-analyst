"""D10 -- the yield-curve-inversion signal, end to end.

The template every future earned claim type copies (D2 step 5): demand -> gap ->
fill -> derived claim, with provenance edges and a licence resolved from the
inputs rather than guessed. Mirrors test_derived_fill.py's discipline but for
``macro.yield_curve_signal`` producing ``yield_curve_signal`` from two
``macro_series_point`` series (DGS2/DGS10).

What each test proves, not just demonstrates:
  - the happy path writes a real claim of the new type, shared (allowed, no
    audience) because both inputs are FRED-sourced, with claim_input edges
    exactly equal to the ids materialization reported (the D5/D7 invariant)
  - a series short of min_obs abstains honestly: unfillable, reason names the
    short series, no claim and no edges written
  - the 90-day count cannot be silently truncated: two series that each clear
    min_obs but barely overlap abstain at compute time rather than emit a
    "90d" count over a shorter window
  - resolve_derived_licence over the materialized rows resolves to shareable,
    not because the descriptor says so but because the inputs are all allowed
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from omni.capability.arguments import Materialized, materialize
from omni.capability.derived import YC_ARGUMENTS, YC_KEY, YIELD_CURVE
from omni.coverage.visibility import visible_claims
from omni.demand.ledger import direct_attention
from omni.fill.derived import fill_analysis
from omni.fill.pipeline import claim_next_gap
from omni.perception.divergence import resolve_derived_licence
from omni.scheduler.worker import sweep_once

BASE = datetime(2024, 1, 1, tzinfo=UTC)
# Enough daily observations to clear min_obs=90 and leave a full 90-day window.
N = 100

_INSERT_CLAIM = """
INSERT INTO claim (entity_id, claim_type, key, value, source,
                   event_date, knowledge_date, confidence,
                   redistributable, audience_user_id, derivation)
VALUES ($1,$2::claim_type,$3,$4::jsonb,$5,$6,$7,$8,
        $9::redistribution,$10,'ingested')
RETURNING id
"""


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _entity(db, symbol="US"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('macro', $1, $1) "
        "RETURNING id",
        symbol,
    )


async def _insert_series(
    db,
    entity_id,
    dates,
    values,
    *,
    key,
    source="fred",
    redistributable="allowed",
    audience_user_id=None,
):
    ids = []
    for event_date, value in zip(dates, values):
        knowledge_date = event_date + timedelta(days=1)
        cid = await db.pool.fetchval(
            _INSERT_CLAIM, entity_id, "macro_series_point", key,
            json.dumps({"value": value}), source,
            event_date, knowledge_date, 1.0,
            redistributable, audience_user_id,
        )
        ids.append(cid)
    return ids


async def _demand_and_lease(db, entity_id):
    """direct_attention -> sweep -> lease the resulting gap. Returns the gap
    dict the fill path consumes, or fails the test if no gap appeared."""
    await direct_attention(
        db.pool, entity_id=entity_id, claim_type="yield_curve_signal",
        key=YC_KEY,
    )
    n = await sweep_once(db.pool)
    assert n > 0, "sweep recorded no gap for the yield_curve_signal demand"
    gap = await claim_next_gap(db.pool, worker_id="test-yield-curve")
    assert gap is not None, "claim_next_gap leased nothing after the sweep"
    return gap


# --------------------------------------------------------------- the happy path


class TestEndToEnd:
    async def test_demand_gap_fill_writes_a_shared_claim_with_input_edges(
        self, db
    ):
        entity_id = await _entity(db)
        dates = [BASE + timedelta(days=i) for i in range(N)]
        # Inverted curve: 2Y (4.5) above 10Y (4.0) -> spread -0.5 < 0.
        dgs2_ids = await _insert_series(
            db, entity_id, dates, [4.5] * N, key="DGS2"
        )
        dgs10_ids = await _insert_series(
            db, entity_id, dates, [4.0] * N, key="DGS10"
        )
        seeded = set(dgs2_ids) | set(dgs10_ids)

        gap = await _demand_and_lease(db, entity_id)

        result = await fill_analysis(db.pool, gap, capability=YIELD_CURVE)
        assert result.outcome == "filled", result.reason
        assert result.capability == "macro.yield_curve_signal"
        assert len(result.claim_ids) == 1
        claim_id = result.claim_ids[0]

        row = await db.pool.fetchrow(
            "SELECT claim_type::text, key, value, unit, derivation, "
            "redistributable, audience_user_id FROM claim WHERE id = $1",
            claim_id,
        )
        assert row["claim_type"] == "yield_curve_signal"
        assert row["key"] == YC_KEY
        assert row["derivation"] == "derived"
        # Licence resolved from the inputs (both FRED/allowed) -> shared.
        assert row["redistributable"] == "allowed"
        assert row["audience_user_id"] is None
        assert row["unit"] == "percent"

        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        assert value["current_spread"] == pytest.approx(-0.5)
        assert value["is_inverted"] is True
        assert value["days_inverted_90d"] == 90

        # A shared (allowed, no audience) derived claim reaches shared coverage.
        assert await db.pool.fetchval(
            "SELECT count(*) FROM shared_coverage WHERE id = $1", claim_id
        ) == 1

        # The gap was closed and the fill was recorded.
        assert await db.pool.fetchval(
            "SELECT resolved_at IS NOT NULL FROM gap WHERE id = $1", gap["id"]
        )

        # claim_input edges equal the ids materialization reported -- the
        # D5/D7 invariant. Materialize independently and compare, do not assume.
        m2y = await materialize(
            YC_ARGUMENTS[0], db.pool, entity_id=entity_id, audience=None
        )
        m10y = await materialize(
            YC_ARGUMENTS[1], db.pool, entity_id=entity_id, audience=None
        )
        assert isinstance(m2y, Materialized) and isinstance(m10y, Materialized)
        materialized_ids = set(m2y.claim_ids) | set(m10y.claim_ids)
        edge_ids = {
            r["input_id"] for r in await db.pool.fetch(
                "SELECT input_id FROM claim_input WHERE claim_id = $1", claim_id
            )
        }
        assert edge_ids == materialized_ids == seeded

        # The owner (no audience) sees the claim through the sanctioned reader.
        visible = await visible_claims(
            db.pool, audience=None, claim_type="yield_curve_signal"
        )
        assert any(r["id"] == claim_id for r in visible)


# --------------------------------------------------- honest abstention: min_obs


class TestShortSeriesAbstains:
    async def test_below_min_obs_is_unfillable_reason_names_the_short_series(
        self, db
    ):
        entity_id = await _entity(db)
        dates = [BASE + timedelta(days=i) for i in range(N)]
        # DGS2 short of the 90-observation floor; DGS10 full.
        await _insert_series(
            db, entity_id, dates[:50], [4.5] * 50, key="DGS2"
        )
        await _insert_series(
            db, entity_id, dates, [4.0] * N, key="DGS10"
        )

        gap = await _demand_and_lease(db, entity_id)

        result = await fill_analysis(db.pool, gap, capability=YIELD_CURVE)
        assert result.outcome == "unfillable"
        assert result.claim_ids == []
        # The reason must name which argument fell short and by how much --
        # not a generic "could not compute".
        assert "series_2y" in result.reason
        assert "50 of 90" in result.reason

        # Nothing was written.
        assert await db.pool.fetchval(
            "SELECT count(*) FROM claim WHERE derivation = 'derived'"
        ) == 0
        assert await db.pool.fetchval("SELECT count(*) FROM claim_input") == 0

        attempt = await db.pool.fetchrow(
            "SELECT outcome, reason, claim_id FROM fill_attempt WHERE gap_id = $1",
            gap["id"],
        )
        assert attempt["outcome"] == "unfillable"
        assert attempt["claim_id"] is None
        assert attempt["reason"] == result.reason


# ----------------------------- honest abstention: the 90-day count untruncated


class TestNinetyDayCountNotSilentlyTruncated:
    async def test_two_long_series_that_barely_overlap_abstain(self, db):
        """Each series clears min_obs=90 independently, but their common
        intersection is below 90 -- so ``days_inverted_90d`` would be a count
        over a shorter window wearing a "90d" label. Compute abstains rather
        than emit the misleading signal."""
        entity_id = await _entity(db)
        # DGS2 covers days 0..89 (90 obs); DGS10 covers days 50..149 (100 obs).
        # Intersection is days 50..89 = 40 common dates.
        d2y_dates = [BASE + timedelta(days=i) for i in range(90)]
        d10y_dates = [BASE + timedelta(days=i) for i in range(50, 150)]
        await _insert_series(
            db, entity_id, d2y_dates, [4.5] * 90, key="DGS2"
        )
        await _insert_series(
            db, entity_id, d10y_dates, [4.0] * 100, key="DGS10"
        )

        gap = await _demand_and_lease(db, entity_id)

        result = await fill_analysis(db.pool, gap, capability=YIELD_CURVE)
        assert result.outcome == "unfillable"
        assert result.claim_ids == []
        assert await db.pool.fetchval(
            "SELECT count(*) FROM claim WHERE derivation = 'derived'"
        ) == 0


# ----------------------------- licence resolved from inputs, not the descriptor


class TestLicenceResolvedFromInputs:
    async def test_allowed_inputs_resolve_to_shareable(self, db):
        """``touches_byo=False`` on the descriptor is a planning hint; the
        written claim's licence comes from ``resolve_derived_licence`` over the
        materialized rows. Both DGS2/DGS10 are FRED (allowed, no audience), so
        the derivation must resolve to shareable -- confirmed, not assumed."""
        entity_id = await _entity(db)
        dates = [BASE + timedelta(days=i) for i in range(N)]
        await _insert_series(db, entity_id, dates, [4.5] * N, key="DGS2")
        await _insert_series(db, entity_id, dates, [4.0] * N, key="DGS10")

        m2y = await materialize(
            YC_ARGUMENTS[0], db.pool, entity_id=entity_id, audience=None
        )
        m10y = await materialize(
            YC_ARGUMENTS[1], db.pool, entity_id=entity_id, audience=None
        )
        assert isinstance(m2y, Materialized) and isinstance(m10y, Materialized)

        rows = [*m2y.rows, *m10y.rows]
        redistributable, audience = resolve_derived_licence(rows)

        assert redistributable == "allowed"
        assert audience is None
        # Cross-check: every carried row is allowed with no audience -- there is
        # no byo_only input that would make this resolve otherwise.
        assert all(r.redistributable == "allowed" for r in rows)
        assert all(r.audience_user_id is None for r in rows)


# ----------------------------------------- evidence carries the trajectory


class TestEvidenceShape:
    async def test_value_is_durable_state_evidence_carries_trajectory(self, db):
        """``value`` is the durable state a consumer reads; the bulky
        ``historical_spreads`` trajectory lives in ``evidence`` with dates
        serialized for JSONB, reconstructable from the inputs."""
        entity_id = await _entity(db)
        dates = [BASE + timedelta(days=i) for i in range(N)]
        await _insert_series(db, entity_id, dates, [4.5] * N, key="DGS2")
        await _insert_series(db, entity_id, dates, [4.0] * N, key="DGS10")

        gap = await _demand_and_lease(db, entity_id)
        result = await fill_analysis(db.pool, gap, capability=YIELD_CURVE)
        assert result.outcome == "filled", result.reason

        row = await db.pool.fetchrow(
            "SELECT value, evidence FROM claim WHERE id = $1",
            result.claim_ids[0],
        )
        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        evidence = json.loads(row["evidence"]) if isinstance(row["evidence"], str) else row["evidence"]

        # value is exactly the durable state -- no trajectory bloating it.
        assert set(value) == {"current_spread", "is_inverted", "days_inverted_90d"}

        # evidence carries the last-30 trajectory and the input ids.
        assert evidence["series"] == ["DGS2", "DGS10"]
        assert len(evidence["historical_spreads"]) == 30
        first = evidence["historical_spreads"][0]
        assert set(first) == {"date", "spread", "inverted"}
        # Dates survived the JSONB round-trip as ISO strings.
        assert isinstance(first["date"], str)
        assert first["spread"] == pytest.approx(-0.5)
        assert first["inverted"] is True
        # input_claim_ids are UUIDs in string form and cover both series.
        assert len(evidence["input_claim_ids"]) == 2 * N
        for s in evidence["input_claim_ids"]:
            UUID(s)  # raises if not a valid uuid string
