"""Phase B: the macro regime assessment loop.

Two test classes. ``TestComposition`` proves the pure functions are correct in
isolation -- the recession weights match the capability, each classifier maps
the right inputs to the right label. ``TestAssessMacroRegime`` proves the loop
reads claims, composes, writes, and abstains honestly when a signal is missing.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from omni.autonomous.macro import (
    CLAIM_TYPE,
    KEY,
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


async def _seed_signal(
    db,
    *,
    entity_id,
    claim_type,
    key,
    value,
    days_ago=0,
):
    now = datetime.now(UTC)
    event = now - timedelta(days=days_ago + 1)
    knowledge = now - timedelta(days=days_ago)
    return await db.pool.fetchval(
        """
        INSERT INTO claim (
            entity_id, claim_type, key, value, source,
            event_date, knowledge_date, confidence,
            redistributable, audience_user_id, derivation
        )
        VALUES ($1, $2::claim_type, $3, $4::jsonb, 'test.macro',
                $5, $6, 1.0, 'allowed', NULL, 'ingested')
        RETURNING id
        """,
        entity_id,
        claim_type,
        key,
        json.dumps(value),
        event,
        knowledge,
    )


def _full_signal_set(
    *,
    yc_inverted=False,
    sahm_triggered=False,
    lei_negative=False,
    yoy=2.5,
    output_gap=0.5,
):
    return {
        "yield_curve_signal": {
            "claim_type": "yield_curve_signal",
            "key": "yield_curve",
            "value": {
                "current_spread": -0.2 if yc_inverted else 0.5,
                "is_inverted": yc_inverted,
                "days_inverted_90d": 90 if yc_inverted else 0,
            },
        },
        "sahm_rule_signal": {
            "claim_type": "sahm_rule_signal",
            "key": "unrate",
            "value": {"indicator": 0.5 if sahm_triggered else 0.1, "triggered": sahm_triggered},
        },
        "inflation_signal": {
            "claim_type": "inflation_signal",
            "key": "cpi_all",
            "value": {"yoy": yoy, "mom_annualized": 0.3, "3m_annualized": 0.3},
        },
        "output_gap_signal": {
            "claim_type": "output_gap_signal",
            "key": "gdpc1_gdppot",
            "value": {"output_gap": output_gap},
        },
        "lei_signal": {
            "claim_type": "lei_signal",
            "key": "usslind",
            "value": {"is_negative": lei_negative, "change_6m": -1.0 if lei_negative else 1.0},
        },
    }


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestWeightDrift:
    """The loop hardcodes recession-probability weights that mirror
    ``macro.recession_probability`` (macro.py). If the capability's weights
    change, this test fails -- the two must agree on all 8 boolean
    combinations."""

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
                        f"weights drifted for yc={yc}, sahm={sahm}, lei={lei}: "
                        f"capability={cap_result['probability']}, loop={loop_prob}"
                    )
                    assert cap_result["assessment"] == loop_band


class TestComposition:
    def test_no_signals_means_low_probability_expansion(self):
        prob, band = recession_probability(False, False, False)
        assert prob == 0.0
        assert band == "low"
        assert cycle_phase(False, False, False) == "expansion"

    def test_sahm_alone_is_moderate_contraction(self):
        # Sahm weight is 0.4, so sahm alone = 0.4 = elevated, and phase=contraction
        prob, band = recession_probability(False, True, False)
        assert prob == 0.4
        assert band == "elevated"
        assert cycle_phase(False, True, False) == "contraction"

    def test_all_three_signals_is_high(self):
        prob, band = recession_probability(True, True, True)
        assert prob == 1.0
        assert band == "high"

    def test_yield_curve_alone_is_moderate_peak(self):
        prob, band = recession_probability(True, False, False)
        assert abs(prob - 0.3) < 1e-9
        assert band == "moderate"
        assert cycle_phase(True, False, False) == "peak"

    def test_lei_alone_is_moderate_peak(self):
        prob, band = recession_probability(False, False, True)
        assert abs(prob - 0.3) < 1e-9
        assert band == "moderate"
        assert cycle_phase(False, False, True) == "peak"

    def test_recession_weights_sum_to_one(self):
        prob, _ = recession_probability(True, True, True)
        assert abs(prob - 1.0) < 1e-9

    def test_risk_off_on_stagflation(self):
        assert risk_regime(0.1, 4.0, -1.0) == "risk_off"

    def test_risk_off_on_elevated_recession(self):
        assert risk_regime(0.5, 1.5, 0.0) == "risk_off"

    def test_risk_on_when_calm(self):
        assert risk_regime(0.1, 2.0, 0.5) == "risk_on"

    def test_transition_when_mixed(self):
        assert risk_regime(0.25, 2.5, 0.0) == "transition"

    def test_inflation_cooling(self):
        assert inflation_regime(1.5) == "cooling"

    def test_inflation_stable(self):
        assert inflation_regime(2.5) == "stable"

    def test_inflation_rising(self):
        assert inflation_regime(4.0) == "rising"

    def test_hawkish_on_overheating(self):
        assert policy_stance(3.0, 1.0) == "hawkish"

    def test_dovish_on_slack(self):
        assert policy_stance(1.0, -1.0) == "dovish"

    def test_neutral_on_mixed(self):
        assert policy_stance(2.5, -0.5) == "neutral"


class TestAssessMacroRegime:
    async def test_abstains_when_macro_entity_missing(self, db):
        result = await assess_macro_regime(db.pool)
        assert result is None

    async def test_abstains_when_a_signal_is_missing(self, db):
        entity = await _seed_macro_entity(db)
        signals = _full_signal_set()
        for sig in signals.values():
            await _seed_signal(
                db,
                entity_id=entity,
                claim_type=sig["claim_type"],
                key=sig["key"],
                value=sig["value"],
            )
        # Delete one signal
        await db.pool.execute(
            "DELETE FROM claim WHERE claim_type = 'lei_signal'"
        )
        result = await assess_macro_regime(db.pool)
        assert result is None

    async def test_writes_regime_assessment_with_full_signals(self, db):
        entity = await _seed_macro_entity(db)
        signals = _full_signal_set(yoy=2.5, output_gap=0.5)
        for sig in signals.values():
            await _seed_signal(
                db,
                entity_id=entity,
                claim_type=sig["claim_type"],
                key=sig["key"],
                value=sig["value"],
            )

        claim_id = await assess_macro_regime(db.pool)
        assert claim_id is not None

        row = await db.pool.fetchrow(
            "SELECT value, evidence, source, claim_type FROM claim WHERE id = $1",
            claim_id,
        )
        assert row["claim_type"] == CLAIM_TYPE
        assert row["source"] == "omni.autonomous"
        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        assert value["cycle_phase"] == "expansion"
        assert value["risk_regime"] == "risk_on"
        assert value["inflation_regime"] == "stable"
        assert value["policy_stance"] == "hawkish"
        assert value["recession_probability"] == 0.0

    async def test_recession_signals_produce_contraction(self, db):
        entity = await _seed_macro_entity(db)
        signals = _full_signal_set(
            yc_inverted=True, sahm_triggered=True, lei_negative=True, yoy=4.0, output_gap=-1.0
        )
        for sig in signals.values():
            await _seed_signal(
                db,
                entity_id=entity,
                claim_type=sig["claim_type"],
                key=sig["key"],
                value=sig["value"],
            )

        claim_id = await assess_macro_regime(db.pool)
        row = await db.pool.fetchrow("SELECT value FROM claim WHERE id = $1", claim_id)
        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        assert value["cycle_phase"] == "contraction"
        assert value["risk_regime"] == "risk_off"
        assert value["recession_probability"] == 1.0
        assert value["recession_assessment"] == "high"

    async def test_idempotent_on_unchanged_inputs(self, db):
        entity = await _seed_macro_entity(db)
        signals = _full_signal_set()
        for sig in signals.values():
            await _seed_signal(
                db,
                entity_id=entity,
                claim_type=sig["claim_type"],
                key=sig["key"],
                value=sig["value"],
            )

        first = await assess_macro_regime(db.pool)
        second = await assess_macro_regime(db.pool)

        assert second == first
        count = await db.pool.fetchval(
            "SELECT count(*)::int FROM claim WHERE claim_type = $1",
            CLAIM_TYPE,
        )
        assert count == 1

    async def test_claim_inputs_link_to_the_signals(self, db):
        entity = await _seed_macro_entity(db)
        signals = _full_signal_set()
        seeded_ids = []
        for sig in signals.values():
            cid = await _seed_signal(
                db,
                entity_id=entity,
                claim_type=sig["claim_type"],
                key=sig["key"],
                value=sig["value"],
            )
            seeded_ids.append(cid)

        claim_id = await assess_macro_regime(db.pool)

        edges = await db.pool.fetch(
            "SELECT input_id FROM claim_input WHERE claim_id = $1", claim_id
        )
        edge_ids = {e["input_id"] for e in edges}
        for sid in seeded_ids:
            assert UUID(str(sid)) in edge_ids or sid in edge_ids
