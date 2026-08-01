"""D14 -- the Sahm rule signal, end to end.

The second application of D2's step-5 template (D10 was the first): demand ->
gap -> fill -> derived claim, with provenance edges and a licence resolved from
the inputs rather than guessed. Mirrors ``test_yield_curve_signal.py``'s
discipline but for ``macro.sahm_rule_signal`` producing ``sahm_rule_signal``
from one ``macro_series_point`` series (UNRATE).

Where D10's template needed a daily-vs-monthly adjustment: DGS2/DGS10 are daily
treasury yields (252 obs ~= one trading year); UNRATE is a MONTHLY series, so
the Sahm rule's 12-observation floor already spans ~one calendar year. The
``window``/``min_obs`` here are sized for monthly observations, not copied from
D10.

What each test proves, not just demonstrates:
  - the happy path writes a real claim of the new type, shared (allowed, no
    audience) because the input is FRED-sourced, with claim_input edges exactly
    equal to the ids materialization reported (the D5/D7 invariant), windowed
    to the trailing 18 observations
  - a series short of min_obs abstains honestly: unfillable, reason names the
    short series, no claim and no edges written, and sahm_rule is never called
    (the spec's job is to abstain before the capability ever sees a starved
    argument -- proven with a spy, not an assumption)
  - resolve_derived_licence over the materialized rows resolves to shareable,
    not because the descriptor says so but because the input is allowed
  - 12 *daily* observations satisfy the count-based spec -- documented here as a
    limitation of ArgumentSpec (which operates on counts, not calendar spacing),
    not asserted as a guarantee it does not give
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

import omni.capability.derived as derived_mod
from omni.capability.arguments import Materialized, materialize
from omni.capability.derived import SAHM, SAHM_ARGUMENTS, SAHM_KEY
from omni.coverage.visibility import visible_claims
from omni.demand.ledger import direct_attention
from omni.fill.derived import fill_analysis
from omni.fill.pipeline import claim_next_gap
from omni.perception.divergence import resolve_derived_licence
from omni.scheduler.worker import sweep_once

BASE = datetime(2024, 1, 1, tzinfo=UTC)
# Monthly observations (~31-day spacing). 24 seeded > window=18, so the window
# genuinely truncates to the trailing 18 -- exercising windowing, not just
# min_obs.
N_MONTHS = 24
DAYS_PER_MONTH = 31

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
        db.pool, entity_id=entity_id, claim_type="sahm_rule_signal",
        key=SAHM_KEY,
    )
    n = await sweep_once(db.pool)
    assert n > 0, "sweep recorded no gap for the sahm_rule_signal demand"
    gap = await claim_next_gap(db.pool, worker_id="test-sahm")
    assert gap is not None, "claim_next_gap leased nothing after the sweep"
    return gap


def _monthly_dates(n, *, spaced_days=DAYS_PER_MONTH):
    return [BASE + timedelta(days=spaced_days * i) for i in range(n)]


# --------------------------------------------------------------- the happy path


class TestEndToEnd:
    async def test_demand_gap_fill_writes_a_shared_claim_with_input_edges(
        self, db
    ):
        entity_id = await _entity(db)
        # 24 monthly observations; window=18 takes the trailing 18. The last 3
        # months sit at 4.5 after a flat 3.5 run, so the 3-month average (4.5)
        # minus the 12-month low (3.5) = 1.0 -> triggered.
        dates = _monthly_dates(N_MONTHS)
        values = [3.5] * (N_MONTHS - 3) + [4.5] * 3
        seeded = set(
            await _insert_series(db, entity_id, dates, values, key="UNRATE")
        )

        gap = await _demand_and_lease(db, entity_id)

        result = await fill_analysis(db.pool, gap, capability=SAHM)
        assert result.outcome == "filled", result.reason
        assert result.capability == "macro.sahm_rule_signal"
        assert len(result.claim_ids) == 1
        claim_id = result.claim_ids[0]

        row = await db.pool.fetchrow(
            "SELECT claim_type::text, key, value, unit, derivation, "
            "redistributable, audience_user_id FROM claim WHERE id = $1",
            claim_id,
        )
        assert row["claim_type"] == "sahm_rule_signal"
        assert row["key"] == SAHM_KEY
        assert row["derivation"] == "derived"
        # Licence resolved from the input (FRED/allowed) -> shared.
        assert row["redistributable"] == "allowed"
        assert row["audience_user_id"] is None
        assert row["unit"] == "percent"

        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        assert value["indicator"] == pytest.approx(1.0)
        assert value["triggered"] is True

        # A shared (allowed, no audience) derived claim reaches shared coverage.
        assert await db.pool.fetchval(
            "SELECT count(*) FROM shared_coverage WHERE id = $1", claim_id
        ) == 1

        # The gap was closed and the fill was recorded.
        assert await db.pool.fetchval(
            "SELECT resolved_at IS NOT NULL FROM gap WHERE id = $1", gap["id"]
        )

        # claim_input edges equal the ids materialization reported -- the
        # D5/D7 invariant, asserted against the *windowed* materialization
        # (window=18 truncates the 24 seeded to the trailing 18). Materialize
        # independently and compare, do not assume.
        m = await materialize(
            SAHM_ARGUMENTS[0], db.pool, entity_id=entity_id, audience=None
        )
        assert isinstance(m, Materialized)
        materialized_ids = set(m.claim_ids)
        assert len(materialized_ids) == 18  # windowed, not all 24
        edge_ids = {
            r["input_id"] for r in await db.pool.fetch(
                "SELECT input_id FROM claim_input WHERE claim_id = $1", claim_id
            )
        }
        assert edge_ids == materialized_ids
        # The window dropped the 6 oldest observations: they are in the seeded
        # set but not in the edges.
        assert edge_ids < seeded
        assert len(seeded) == 24
        assert len(edge_ids) == 18

        # The owner (no audience) sees the claim through the sanctioned reader.
        visible = await visible_claims(
            db.pool, audience=None, claim_type="sahm_rule_signal"
        )
        assert any(r["id"] == claim_id for r in visible)


# ----------------------------------- honest abstention: min_obs + the spy


class TestShortSeriesAbstains:
    async def test_below_min_obs_is_unfillable_and_sahm_rule_is_never_called(
        self, db, monkeypatch
    ):
        entity_id = await _entity(db)
        # 8 monthly observations, short of the 12-observation floor.
        dates = _monthly_dates(8)
        await _insert_series(db, entity_id, dates, [3.5] * 8, key="UNRATE")

        # Spy on sahm_rule to prove the spec abstains BEFORE the capability is
        # ever handed a starved argument. The point of min_obs is that the
        # capability never sees the short series; the spy makes that provable
        # rather than assumed.
        calls: list = []
        real = derived_mod.sahm_rule

        async def spy(unemployment_values):
            calls.append(list(unemployment_values))
            return await real(unemployment_values)

        monkeypatch.setattr(derived_mod, "sahm_rule", spy)

        gap = await _demand_and_lease(db, entity_id)

        result = await fill_analysis(db.pool, gap, capability=SAHM)
        assert result.outcome == "unfillable"
        assert result.claim_ids == []
        # The reason must name which argument fell short and by how much --
        # not a generic "could not compute".
        assert "unemployment" in result.reason
        assert "8 of 12" in result.reason

        # sahm_rule was never reached: the spec abstained at materialization.
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
        """``touches_byo=False`` on the descriptor is a planning hint; the
        written claim's licence comes from ``resolve_derived_licence`` over the
        materialized rows. UNRATE is FRED (allowed, no audience), so the
        derivation must resolve to shareable -- confirmed, not assumed."""
        entity_id = await _entity(db)
        dates = _monthly_dates(N_MONTHS)
        await _insert_series(
            db, entity_id, dates, [3.5] * N_MONTHS, key="UNRATE"
        )

        m = await materialize(
            SAHM_ARGUMENTS[0], db.pool, entity_id=entity_id, audience=None
        )
        assert isinstance(m, Materialized)

        rows = [*m.rows]
        redistributable, audience = resolve_derived_licence(rows)

        assert redistributable == "allowed"
        assert audience is None
        # Cross-check: every carried row is allowed with no audience -- there is
        # no byo_only input that would make this resolve otherwise.
        assert all(r.redistributable == "allowed" for r in rows)
        assert all(r.audience_user_id is None for r in rows)


# ----------------------------------------- evidence carries supporting state


class TestEvidenceShape:
    async def test_value_is_durable_state_evidence_carries_supporting_state(
        self, db
    ):
        """``value`` is the durable state a consumer reads (the indicator and
        the triggered flag ``recession_probability`` consumes); the supporting
        ``current_unemployment`` / ``12m_low`` are reconstructable from the
        inputs, so they live in ``evidence`` alongside the input ids."""
        entity_id = await _entity(db)
        dates = _monthly_dates(N_MONTHS)
        values = [3.5] * (N_MONTHS - 3) + [4.5] * 3
        await _insert_series(db, entity_id, dates, values, key="UNRATE")

        gap = await _demand_and_lease(db, entity_id)
        result = await fill_analysis(db.pool, gap, capability=SAHM)
        assert result.outcome == "filled", result.reason

        row = await db.pool.fetchrow(
            "SELECT value, evidence FROM claim WHERE id = $1",
            result.claim_ids[0],
        )
        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        evidence = json.loads(row["evidence"]) if isinstance(row["evidence"], str) else row["evidence"]

        # value is exactly the durable state a consumer reads -- no supporting
        # context bloating it.
        assert set(value) == {"indicator", "triggered"}

        # evidence carries the supporting state and the input ids.
        assert evidence["series"] == ["UNRATE"]
        assert evidence["current_unemployment"] == pytest.approx(4.5)
        assert evidence["12m_low"] == pytest.approx(3.5)
        # window=18, so the input ids are the trailing 18 observations.
        assert len(evidence["input_claim_ids"]) == 18
        for s in evidence["input_claim_ids"]:
            UUID(s)  # raises if not a valid uuid string


# --------------------- monthly spacing: the limitation the spec cannot enforce


class TestMonthlySpacing:
    async def test_twelve_daily_observations_are_rejected_by_the_calendar_guard(
        self, db
    ):
        """``min_calendar_days`` closes the count-only limitation this test used
        to document: 12 DAILY observations (span ~11 days) clear ``min_obs=12``
        but fall short of ``min_calendar_days=330`` (~11 months), so the spec
        abstains and ``sahm_rule`` is never handed a year of daily data
        masquerading as monthly. The reason names the calendar shortfall."""
        entity_id = await _entity(db)
        # 12 DAILY observations (1-day spacing), not monthly.
        dates = [BASE + timedelta(days=i) for i in range(12)]
        await _insert_series(db, entity_id, dates, [3.5] * 12, key="UNRATE")

        gap = await _demand_and_lease(db, entity_id)

        result = await fill_analysis(db.pool, gap, capability=SAHM)
        assert result.outcome == "unfillable"
        assert result.claim_ids == []
        assert "unemployment" in result.reason
        assert "calendar days" in result.reason
