"""The lei_signal DerivedCapability: FRED USSLIND -> a 6-month decline flag.

The fifth earned macro claim type (after yield_curve/sahm/inflation/output_gap
signals), and the one 2.3 exists for: it gives macro.recession_probability its
LEI term. The signal is the 6-month percent change of USSLIND; a decline (< 0)
is the ``is_negative`` flag recession_probability reads.

Binding floor is 7 (the 6-month-ago index at ``levels[-7]``), so ``min_obs=7``
-- the monthly analogue of inflation's ``min_obs=13`` year-ago floor. ``window =
13`` = 7 + 6 months margin; 24 seeded > window so the window genuinely truncates
to the trailing 13, exercising windowing not just min_obs.

Discriminating series: positions [-7]=100 and [-1]=98, so change_6m =
(98/100 - 1)*100 = -2.0%. A bug reading [-6] (=99) yields -1.0101% -- outside
the tolerance, so the assertion settles which index the compute uses.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from omni.capability.arguments import Materialized, materialize
from omni.capability.derived import LEI, LEI_ARGUMENTS, LEI_KEY, LEI_SPEC_KEY
from omni.demand.ledger import direct_attention
from omni.fill.derived import fill_analysis
from omni.fill.pipeline import claim_next_gap
from omni.perception.divergence import resolve_derived_licence
from omni.scheduler.worker import sweep_once

BASE = datetime(2024, 1, 1, tzinfo=UTC)
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
    db, entity_id, dates, values, *, key, source="fred",
    redistributable="allowed", audience_user_id=None,
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
    await direct_attention(
        db.pool, entity_id=entity_id, claim_type="lei_signal", key=LEI_KEY,
    )
    n = await sweep_once(db.pool)
    assert n > 0, "sweep recorded no gap for the lei_signal demand"
    gap = await claim_next_gap(db.pool, worker_id="test-lei")
    assert gap is not None, "claim_next_gap leased nothing after the sweep"
    return gap


def _monthly_dates(n, *, spaced_days=DAYS_PER_MONTH):
    return [BASE + timedelta(days=spaced_days * i) for i in range(n)]


# Trailing 13: 7x100, 5x99, 98. [-7]=100, [-1]=98 -> change_6m = -2.0%; [-6]=99,
# so a [-6]-instead-of-[-7] bug gives (98/99-1)*100 = -1.0101% -- discriminates.
# 11 flat 95.0 readings precede so count (24) > window (13) and windowing
# truncates; the trailing 13 still contains [-7], so the lookup is stable.
def _declining_usslind(n=N_MONTHS):
    return [95.0] * (n - 13) + [100.0] * 7 + [99.0] * 5 + [98.0]


class TestEndToEnd:
    async def test_demand_gap_fill_writes_a_decline_flag_claim(self, db):
        entity_id = await _entity(db)
        dates = _monthly_dates(N_MONTHS)
        values = _declining_usslind(N_MONTHS)
        seeded = await _insert_series(db, entity_id, dates, values, key=LEI_SPEC_KEY)

        gap = await _demand_and_lease(db, entity_id)
        result = await fill_analysis(db.pool, gap, capability=LEI)
        assert result.outcome == "filled", result.reason
        assert result.capability == "macro.lei_signal"
        assert len(result.claim_ids) == 1
        claim_id = result.claim_ids[0]

        row = await db.pool.fetchrow(
            "SELECT claim_type::text, key, value, unit, derivation, evidence, "
            "redistributable, audience_user_id FROM claim WHERE id = $1",
            claim_id,
        )
        assert row["claim_type"] == "lei_signal"
        assert row["key"] == LEI_KEY
        assert row["derivation"] == "derived"
        assert row["redistributable"] == "allowed"
        assert row["audience_user_id"] is None
        assert row["unit"] == "percent"

        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        # The 6-month change is the hand-computed -2.0% ([-7]=100, [-1]=98).
        # Discriminates a [-6]-instead-of-[-7] bug (-1.0101%).
        assert value["change_6m"] == pytest.approx(-2.0, abs=1e-6)
        assert value["is_negative"] is True

        # D5/D7 invariant: the evidence input ids are exactly the windowed
        # trailing 13 (window=13 truncates the 24 seeded), not the whole series.
        evidence = json.loads(row["evidence"]) if isinstance(row["evidence"], str) else row["evidence"]
        assert set(evidence["input_claim_ids"]) == {str(x) for x in seeded[-13:]}
        assert evidence["current"] == pytest.approx(98.0)
        assert evidence["six_months_ago"] == pytest.approx(100.0)


class TestRisingIndexIsNotNegative:
    async def test_a_rising_index_clears_is_negative(self, db):
        # [-7]=100, [-1]=102 -> change_6m = +2.0% -> is_negative False.
        entity_id = await _entity(db)
        rising = [95.0] * 11 + [100.0] * 7 + [101.0] * 5 + [102.0]
        await _insert_series(
            db, entity_id, _monthly_dates(N_MONTHS), rising, key=LEI_SPEC_KEY,
        )
        gap = await _demand_and_lease(db, entity_id)
        result = await fill_analysis(db.pool, gap, capability=LEI)
        assert result.outcome == "filled", result.reason
        row = await db.pool.fetchrow(
            "SELECT value FROM claim WHERE id=$1", result.claim_ids[0]
        )
        value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        assert value["change_6m"] == pytest.approx(2.0, abs=1e-6)
        assert value["is_negative"] is False


class TestShortSeriesAbstains:
    async def test_below_min_obs_is_unfillable(self, db):
        # 6 observations < min_obs=7: the [-7] lookup is not satisfiable.
        entity_id = await _entity(db)
        await _insert_series(
            db, entity_id, _monthly_dates(6), [100.0] * 6, key=LEI_SPEC_KEY,
        )
        gap = await _demand_and_lease(db, entity_id)
        result = await fill_analysis(db.pool, gap, capability=LEI)
        assert result.outcome == "unfillable"
        assert "lei" in (result.reason or "").lower() or "min" in (result.reason or "").lower()


class TestLicence:
    async def test_allowed_inputs_resolve_to_shareable(self, db):
        # The written claim's licence comes from resolve_derived_licence over the
        # materialized rows, not from FillResult. USSLIND is FRED (allowed), so
        # the derivation resolves to shareable -- confirmed, not assumed.
        entity_id = await _entity(db)
        await _insert_series(
            db, entity_id, _monthly_dates(N_MONTHS), _declining_usslind(),
            key=LEI_SPEC_KEY,
        )
        m = await materialize(
            LEI_ARGUMENTS[0], db.pool, entity_id=entity_id, audience=None
        )
        assert isinstance(m, Materialized)
        rows = [*m.rows]
        redistributable, audience = resolve_derived_licence(rows)
        assert redistributable == "allowed"
        assert audience is None
        assert all(r.redistributable == "allowed" for r in rows)
