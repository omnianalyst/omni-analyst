"""Phase B: the macro regime assessment loop.

Two test classes verify the pure composition functions (weights, classifiers)
and the drift guard against the capability. ``TestAssessMacroRegime`` proves
the loop reads raw FRED macro_series_point claims, computes signals inline,
and writes a regime_assessment claim -- abstaining honestly when data is
insufficient.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from omni.autonomous.macro import (
    CLAIM_TYPE,
    assess_macro_regime,
    cycle_phase,
    inflation_regime,
    policy_stance,
    recession_probability,
    risk_regime,
)


async def _seed_macro_entity(db):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('macro', 'US_MACRO', 'US Macroeconomy') RETURNING id"
    )


async def _seed_fred(db, entity_id, key, values, *, interval_days=30):
    """Seed macro_series_point claims for a FRED series, oldest-first."""
    now = datetime.now(UTC)
    base = now - timedelta(days=len(values) * interval_days + interval_days)
    for i, v in enumerate(values):
        event = base + timedelta(days=i * interval_days)
        knowledge = event + timedelta(days=1)
        await db.pool.execute(
            """
            INSERT INTO claim (entity_id, claim_type, key, value, source,
                               event_date, knowledge_date, confidence,
                               redistributable, audience_user_id, derivation)
            VALUES ($1, 'macro_series_point', $2, $3::jsonb, 'fred',
                    $4, $5, 1.0, 'allowed', NULL, 'ingested')
            ON CONFLICT DO NOTHING
            """,
            entity_id, key, json.dumps({"value": v}), event, knowledge,
        )


def _full_fred_setup(
    *,
    dgs2=4.0, dgs10=4.2,
    unrate=None, cpi=None, gdp=23000.0, pot=22500.0, lei=None,
):
    """Build a complete set of FRED series values for the regime assessment."""
    if unrate is None:
        unrate = [3.8, 3.9, 3.9, 4.0, 4.0, 4.1, 4.0, 3.9, 3.8, 3.8, 3.9, 4.0]
    if cpi is None:
        cpi = [300.0 + i * 0.5 for i in range(13)]
    if lei is None:
        lei = [100.0 + i * 0.1 for i in range(7)]
    return {
        "DGS2": [dgs2] * 5,
        "DGS10": [dgs10] * 5,
        "UNRATE": unrate,
        "CPIAUCSL": cpi,
        "GDPC1": [gdp],
        "GDPPOT": [pot],
        "USSLIND": lei,
    }


async def _seed_all_fred(db, entity_id, setup):
    for key, values in setup.items():
        await _seed_fred(db, entity_id, key, values)


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestWeightDrift:
    """The loop hardcodes recession-probability weights that mirror
    ``macro.recession_probability`` (macro.py). If the capability's weights
    change, this test fails."""

    async def test_loop_weights_match_capability(self):
        from omni.capabilities.macro import recession_probability as cap_fn

        for yc in (True, False):
            for sahm in (True, False):
                for lei in (True, False):
                    cap_result = await cap_fn(
                        yield_curve_inverted=yc,
                        sahm_triggered=sahm,
                        lei_signals=["negative"] if lei else [],
                    )
                    loop_prob, loop_band = recession_probability(yc, sahm, lei)
                    assert abs(cap_result["probability"] - loop_prob) < 1e-9, (
                        f"weights drifted for yc={yc}, sahm={sahm}, lei={lei}"
                    )
                    assert cap_result["assessment"] == loop_band


class TestComposition:
    def test_no_signals_means_low_probability_expansion(self):
        prob, band = recession_probability(False, False, False)
        assert prob == 0.0
        assert band == "low"
        assert cycle_phase(False, False, False) == "expansion"

    def test_sahm_alone_is_moderate_contraction(self):
        prob, band = recession_probability(False, True, False)
        assert prob == 0.4
        assert band == "elevated"
        assert cycle_phase(False, True, False) == "contraction"

    def test_all_three_signals_is_high(self):
        prob, band = recession_probability(True, True, True)
        assert prob == 1.0
        assert band == "high"

    def test_recession_weights_sum_to_one(self):
        prob, _ = recession_probability(True, True, True)
        assert abs(prob - 1.0) < 1e-9

    def test_risk_off_on_stagflation(self):
        assert risk_regime(0.1, 4.0, -1.0) == "risk_off"

    def test_risk_on_when_calm(self):
        assert risk_regime(0.1, 2.0, 0.5) == "risk_on"

    def test_inflation_cooling(self):
        assert inflation_regime(1.5) == "cooling"

    def test_hawkish_on_overheating(self):
        assert policy_stance(3.0, 1.0) == "hawkish"

    def test_dovish_on_slack(self):
        assert policy_stance(1.0, -1.0) == "dovish"


class TestAssessMacroRegime:
    async def test_abstains_when_macro_entity_missing(self, db):
        result = await assess_macro_regime(db.pool)
        assert result is None

    async def test_abstains_when_insufficient_cpi(self, db):
        entity = await _seed_macro_entity(db)
        setup = _full_fred_setup()
        setup["CPIAUCSL"] = [300.0, 301.0]  # only 2, need 13
        await _seed_all_fred(db, entity, setup)

        result = await assess_macro_regime(db.pool)
        assert result is None

    async def test_writes_regime_with_full_data(self, db):
        entity = await _seed_macro_entity(db)
        await _seed_all_fred(db, entity, _full_fred_setup(dgs2=4.0, dgs10=4.2))

        claim_id = await assess_macro_regime(db.pool)
        assert claim_id is not None

        row = await db.pool.fetchrow(
            "SELECT value, source FROM claim WHERE id = $1", claim_id
        )
        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        assert row["source"] == "omni.autonomous"
        assert value["cycle_phase"] == "expansion"
        assert value["risk_regime"] == "risk_on"
        assert value["inflation_yoy"] > 0
        assert value["recession_probability"] == 0.0

    async def test_inverted_yield_curve_produces_peak(self, db):
        entity = await _seed_macro_entity(db)
        await _seed_all_fred(db, entity, _full_fred_setup(dgs2=4.5, dgs10=4.0))

        claim_id = await assess_macro_regime(db.pool)
        row = await db.pool.fetchrow("SELECT value FROM claim WHERE id = $1", claim_id)
        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        assert value["cycle_phase"] == "peak"
        assert value["yield_curve_inverted"] is True
        assert value["recession_probability"] > 0

    async def test_claim_inputs_link_to_fred_data(self, db):
        entity = await _seed_macro_entity(db)
        await _seed_all_fred(db, entity, _full_fred_setup())

        claim_id = await assess_macro_regime(db.pool)
        edges = await db.pool.fetch(
            "SELECT input_id FROM claim_input WHERE claim_id = $1", claim_id
        )
        assert len(edges) >= 1
        for e in edges:
            src = await db.pool.fetchval(
                "SELECT source FROM claim WHERE id = $1", e["input_id"]
            )
            assert src == "fred"

    async def test_sahm_triggered_on_unemployment_spike(self, db):
        entity = await _seed_macro_entity(db)
        unrate = [3.5] * 9 + [4.0, 4.2, 4.5]
        await _seed_all_fred(db, entity, _full_fred_setup(unrate=unrate))

        claim_id = await assess_macro_regime(db.pool)
        row = await db.pool.fetchrow("SELECT value FROM claim WHERE id = $1", claim_id)
        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        assert value["sahm_triggered"] is True
        assert value["recession_probability"] >= 0.4
