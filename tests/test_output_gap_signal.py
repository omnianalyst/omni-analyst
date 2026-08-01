"""The output-gap signal, end to end.

The fourth earned macro claim type (after yield_curve_signal, sahm_rule_signal,
inflation_signal) and the one that unblocks ``macro.taylor_rule`` as a
composite consuming TWO earned claim types (inflation_signal + this one).
demand -> gap -> fill -> derived claim, with provenance edges and a licence
resolved from the inputs. Mirrors ``test_inflation_signal.py``'s discipline.

Structurally the credit_risk / inflation_expectations scalar shape (two latest
observations), but claim-producing: the output gap is a durable STATE
``macro.taylor_rule`` consumes, so it earns a claim type (``output_gap_signal``).

The headline ``output_gap = (gdp - potential) / potential * 100`` is
hand-computed for the seed (gdp=27000, potential=26500 -> 1.887%), which
discriminates the denominator (a ``/gdp`` bug gives 1.852%) and the sign (a
``(potential - gdp)`` bug gives -1.887%).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

import omni.capability.derived as derived_mod
from omni.capability.arguments import Materialized, materialize
from omni.capability.derived import (
    OUTPUT_GAP,
    OUTPUT_GAP_ARGUMENTS,
    OUTPUT_GAP_KEY,
    OUTPUT_GAP_SPEC_KEYS,
)
from omni.coverage.visibility import visible_claims
from omni.demand.ledger import direct_attention
from omni.fill.derived import fill_analysis
from omni.fill.pipeline import claim_next_gap
from omni.perception.divergence import resolve_derived_licence
from omni.scheduler.worker import sweep_once

BASE = datetime(2024, 1, 1, tzinfo=UTC)

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


async def _insert_scalar(
    db, entity_id, value, *, key, event_date=BASE,
    source="fred", redistributable="allowed", audience_user_id=None,
):
    knowledge_date = event_date + timedelta(days=1)
    return await db.pool.fetchval(
        _INSERT_CLAIM, entity_id, "macro_series_point", key,
        json.dumps({"value": value}), source,
        event_date, knowledge_date, 1.0,
        redistributable, audience_user_id,
    )


async def _demand_and_lease(db, entity_id):
    await direct_attention(
        db.pool, entity_id=entity_id, claim_type="output_gap_signal",
        key=OUTPUT_GAP_KEY,
    )
    n = await sweep_once(db.pool)
    assert n > 0, "sweep recorded no gap for the output_gap_signal demand"
    gap = await claim_next_gap(db.pool, worker_id="test-output-gap")
    assert gap is not None, "claim_next_gap leased nothing after the sweep"
    return gap


# gdp=27000, potential=26500 -> gap = (27000-26500)/26500*100 = 1.88679...%
GDP = 27000.0
POTENTIAL = 26500.0
EXPECTED_GAP = (GDP - POTENTIAL) / POTENTIAL * 100.0


# --------------------------------------------------------------- the happy path


class TestEndToEnd:
    async def test_demand_gap_fill_writes_a_shared_claim_with_input_edges(
        self, db
    ):
        entity_id = await _entity(db)
        gdp_id = await _insert_scalar(db, entity_id, GDP, key="GDPC1")
        potential_id = await _insert_scalar(db, entity_id, POTENTIAL, key="GDPPOT")
        seeded = {gdp_id, potential_id}

        gap = await _demand_and_lease(db, entity_id)

        result = await fill_analysis(db.pool, gap, capability=OUTPUT_GAP)
        assert result.outcome == "filled", result.reason
        assert result.capability == "macro.output_gap_signal"
        assert len(result.claim_ids) == 1
        claim_id = result.claim_ids[0]

        row = await db.pool.fetchrow(
            "SELECT claim_type::text, key, value, unit, derivation, "
            "redistributable, audience_user_id FROM claim WHERE id = $1",
            claim_id,
        )
        assert row["claim_type"] == "output_gap_signal"
        assert row["key"] == OUTPUT_GAP_KEY
        assert row["derivation"] == "derived"
        # Licence resolved from the inputs (FRED/allowed) -> shared.
        assert row["redistributable"] == "allowed"
        assert row["audience_user_id"] is None
        assert row["unit"] == "percent"

        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        # The headline gap is the hand-computed CBO level ratio. This
        # discriminates the denominator (a /gdp bug -> 1.852%) and the sign.
        assert value["output_gap"] == pytest.approx(EXPECTED_GAP, abs=1e-6)

        # A shared (allowed, no audience) derived claim reaches shared coverage.
        assert await db.pool.fetchval(
            "SELECT count(*) FROM shared_coverage WHERE id = $1", claim_id
        ) == 1

        # The gap was closed and the fill was recorded.
        assert await db.pool.fetchval(
            "SELECT resolved_at IS NOT NULL FROM gap WHERE id = $1", gap["id"]
        )

        # claim_input edges equal the ids materialization reported -- the
        # D5/D7 invariant. Two scalar inputs, one observation each -> 2 edges.
        m_gdp = await materialize(
            OUTPUT_GAP_ARGUMENTS[0], db.pool, entity_id=entity_id, audience=None
        )
        m_pot = await materialize(
            OUTPUT_GAP_ARGUMENTS[1], db.pool, entity_id=entity_id, audience=None
        )
        assert isinstance(m_gdp, Materialized) and isinstance(m_pot, Materialized)
        materialized_ids = set(m_gdp.claim_ids) | set(m_pot.claim_ids)
        assert materialized_ids == seeded
        edge_ids = {
            r["input_id"] for r in await db.pool.fetch(
                "SELECT input_id FROM claim_input WHERE claim_id = $1", claim_id
            )
        }
        assert edge_ids == materialized_ids
        assert len(edge_ids) == 2

        # The owner (no audience) sees the claim through the sanctioned reader.
        visible = await visible_claims(
            db.pool, audience=None, claim_type="output_gap_signal"
        )
        assert any(r["id"] == claim_id for r in visible)


# ----------------------------------- honest abstention: min_obs + the spy


class TestShortSeriesAbstains:
    async def test_when_one_series_absent_is_unfillable_and_output_gap_never_called(
        self, db, monkeypatch
    ):
        entity_id = await _entity(db)
        # Only GDPC1 seeded; GDPPOT absent.
        await _insert_scalar(db, entity_id, GDP, key="GDPC1")

        # Spy on output_gap to prove the spec abstains BEFORE the capability is
        # ever handed a starved argument.
        calls: list = []
        real = derived_mod.output_gap

        async def spy(gdp_values, potential_values):
            calls.append((list(gdp_values), list(potential_values)))
            return await real(gdp_values, potential_values)

        monkeypatch.setattr(derived_mod, "output_gap", spy)

        gap = await _demand_and_lease(db, entity_id)

        result = await fill_analysis(db.pool, gap, capability=OUTPUT_GAP)
        assert result.outcome == "unfillable"
        assert result.claim_ids == []
        # The reason must name the absent argument (potential) and the shortfall.
        assert "potential" in result.reason
        assert "0 of 1" in result.reason

        # output_gap was never reached: the spec abstained at materialization.
        assert calls == []

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


# ----------------------------- licence resolved from inputs, not the descriptor


class TestLicenceResolvedFromInputs:
    async def test_allowed_inputs_resolve_to_shareable(self, db):
        """``touches_byo=False`` is a planning hint; the written claim's licence
        comes from ``resolve_derived_licence`` over the materialized rows.
        GDPC1/GDPPOT are FRED (allowed, no audience), so shareable -- confirmed."""
        entity_id = await _entity(db)
        await _insert_scalar(db, entity_id, GDP, key="GDPC1")
        await _insert_scalar(db, entity_id, POTENTIAL, key="GDPPOT")

        m_gdp = await materialize(
            OUTPUT_GAP_ARGUMENTS[0], db.pool, entity_id=entity_id, audience=None
        )
        m_pot = await materialize(
            OUTPUT_GAP_ARGUMENTS[1], db.pool, entity_id=entity_id, audience=None
        )
        assert isinstance(m_gdp, Materialized) and isinstance(m_pot, Materialized)

        rows = [*m_gdp.rows, *m_pot.rows]
        redistributable, audience = resolve_derived_licence(rows)

        assert redistributable == "allowed"
        assert audience is None
        assert all(r.redistributable == "allowed" for r in rows)
        assert all(r.audience_user_id is None for r in rows)


# ----------------------------------------- evidence carries supporting state


class TestEvidenceShape:
    async def test_value_is_durable_state_evidence_carries_supporting_state(
        self, db
    ):
        """``value`` is the durable state a consumer reads (the percent gap
        ``macro.taylor_rule`` consumes); the supporting ``gdp`` / ``potential``
        levels are reconstructable from the inputs, so they live in ``evidence``
        alongside the input ids."""
        entity_id = await _entity(db)
        await _insert_scalar(db, entity_id, GDP, key="GDPC1")
        await _insert_scalar(db, entity_id, POTENTIAL, key="GDPPOT")

        gap = await _demand_and_lease(db, entity_id)
        result = await fill_analysis(db.pool, gap, capability=OUTPUT_GAP)
        assert result.outcome == "filled", result.reason

        row = await db.pool.fetchrow(
            "SELECT value, evidence FROM claim WHERE id = $1",
            result.claim_ids[0],
        )
        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        evidence = json.loads(row["evidence"]) if isinstance(row["evidence"], str) else row["evidence"]

        # value is exactly the durable state a consumer reads.
        assert set(value) == {"output_gap"}

        # evidence carries the supporting levels and the input ids.
        assert evidence["series"] == list(OUTPUT_GAP_SPEC_KEYS)
        assert evidence["gdp"] == pytest.approx(GDP)
        assert evidence["potential"] == pytest.approx(POTENTIAL)
        assert len(evidence["input_claim_ids"]) == 2
        for s in evidence["input_claim_ids"]:
            UUID(s)  # raises if not a valid uuid string
