"""Findings: what the system said, what it refused to say, and its scorecard."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from omni.conviction.gate import Calibration, Candidate, Refusal, assess
from omni.conviction.publish import (
    briefing,
    load_calibration,
    record,
    refusal_counts,
    scorecard,
)

NOW = datetime(2026, 7, 28, tzinfo=UTC)


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company',$1,$1) RETURNING id",
        symbol,
    )


async def _claim(db, entity_id, *, owner=None):
    shared = owner is None
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1,'manipulation_signal','sig','{}'::jsonb,$2,$3,$4,0.9,$5,$6)
        RETURNING id
        """,
        entity_id, "internal" if shared else "polygon",
        NOW - timedelta(days=1), NOW,
        "allowed" if shared else "byo_only", owner,
    )


async def _prediction(db, entity_id, *, direction="up", outcome=None):
    pid = await db.pool.fetchval(
        """
        INSERT INTO prediction (entity_id, method, direction, confidence,
                                entry_price, upper_barrier, lower_barrier,
                                horizon_ends_at, provenance)
        VALUES ($1,'detect',$2,0.85,100,110,90,$3,'{}'::jsonb) RETURNING id
        """,
        entity_id, direction, NOW + timedelta(days=5),
    )
    if outcome:
        await db.pool.execute(
            "UPDATE prediction SET outcome=$1::prediction_outcome, resolved_at=now() "
            "WHERE id=$2", outcome, pid,
        )
        # Resolved rows are a refresh boundary for the materialized buckets.
        from omni.conviction.stats_refresh import refresh_statistics

        await refresh_statistics(db.pool)
    return pid


def _bucket(low, n, hits):
    return Calibration("manipulation_signal", "detect", low,
                       round(low + 0.1, 2), n, hits)


def _candidate(claim_id, confidence=0.85, **kw):
    kw.setdefault("searched_for_disconfirming", True)
    # A candidate claiming a completed search must name what the search turned
    # up -- `searched_findings_report_what_they_found` enforces it, and the real
    # producer cannot violate it (a check that reaches a verdict always appends
    # to one side). A fixture asserting the search ran while supplying nothing
    # describes a state production cannot reach.
    if kw.get("searched_for_disconfirming", True):
        kw.setdefault("supporting", ("volume z=4.2",))
    kw.setdefault("falsifiable", True)
    return Candidate(claim_id=claim_id, claim_type="manipulation_signal",
                     method="detect", confidence=confidence, **kw)


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, demand CASCADE")
    yield


class TestRecording:
    async def test_a_surfaced_finding_is_stored_with_its_reasoning(self, db):
        e = await _entity(db)
        c = await _claim(db, e)
        p = await _prediction(db, e)
        # Buckets must cover the candidate's confidence for a rate to exist.
        v = assess(_candidate(c, supporting=("volume z=4.2",),
                              disconfirming=("earnings due",)),
                   [_bucket(0.7, 40, 34), _bucket(0.8, 40, 36)])
        assert v.surfaced
        fid = await record(db.pool, v, entity_id=e, prediction_id=p)

        row = await db.pool.fetchrow("SELECT * FROM finding WHERE id=$1", fid)
        assert row["status"] == "surfaced"
        assert row["threshold"] is not None
        assert row["calibrated_hit_rate"] is not None
        assert "earnings due" in row["disconfirming"]

    async def test_a_refusal_is_stored_with_the_gate_that_stopped_it(self, db):
        """The denominator. Without refusals the hit rate means nothing."""
        e = await _entity(db)
        c = await _claim(db, e)
        v = assess(_candidate(c), [])
        assert v.refusal is Refusal.UNCALIBRATED
        await record(db.pool, v, entity_id=e)

        row = await db.pool.fetchrow("SELECT status, refusal FROM finding")
        assert row["status"] == "refused"
        assert row["refusal"] == Refusal.UNCALIBRATED.value

    async def test_a_surfaced_finding_without_a_prediction_is_rejected(self, db):
        """Enforced by the schema, not by the caller: an unscoreable finding
        would let the published hit rate drift from what was claimed."""
        e = await _entity(db)
        c = await _claim(db, e)
        v = assess(_candidate(c), [_bucket(0.7, 40, 34)])
        with pytest.raises(asyncpg.IntegrityConstraintViolationError):
            await record(db.pool, v, entity_id=e, prediction_id=None)

    async def test_confidence_above_every_observed_bucket_reports_no_rate(self, db):
        """Surfaced on the threshold's evidence, but honest that this exact
        confidence has never been observed resolving. None, not an
        extrapolation."""
        e = await _entity(db)
        c = await _claim(db, e)
        p = await _prediction(db, e)
        v = assess(_candidate(c, confidence=0.95), [_bucket(0.7, 40, 34)])
        assert v.surfaced
        assert v.calibrated_hit_rate is None
        fid = await record(db.pool, v, entity_id=e, prediction_id=p)
        row = await db.pool.fetchrow("SELECT threshold, calibrated_hit_rate "
                                     "FROM finding WHERE id=$1", fid)
        assert row["threshold"] == pytest.approx(0.7)
        assert row["calibrated_hit_rate"] is None

    async def test_a_refusal_must_name_a_reason(self, db):
        e = await _entity(db)
        c = await _claim(db, e)
        with pytest.raises(asyncpg.IntegrityConstraintViolationError):
            await db.pool.execute(
                "INSERT INTO finding (claim_id, entity_id, status, method, "
                "confidence) VALUES ($1,$2,'refused','detect',0.5)", c, e,
            )


class TestCalibrationFromTheLedger:
    async def test_an_unproven_method_yields_no_buckets(self, db):
        assert await load_calibration(db.pool, claim_type="x", method="detect") == []

    async def test_resolved_predictions_become_buckets(self, db):
        e = await _entity(db)
        for _ in range(12):
            await _prediction(db, e, outcome="upper")
        buckets = await load_calibration(
            db.pool, claim_type="manipulation_signal", method="detect"
        )
        assert buckets
        assert sum(b.n for b in buckets) == 12
        assert buckets[0].hit_rate == pytest.approx(1.0)

    async def test_a_thin_history_still_refuses_at_the_gate(self, db):
        """End to end: too few resolved predictions means silence, whatever
        the candidate's confidence."""
        e = await _entity(db)
        c = await _claim(db, e)
        for _ in range(3):
            await _prediction(db, e, outcome="upper")
        buckets = await load_calibration(
            db.pool, claim_type="manipulation_signal", method="detect"
        )
        v = assess(_candidate(c, confidence=0.99), buckets)
        assert not v.surfaced
        assert v.refusal is Refusal.UNCALIBRATED


class TestBriefing:
    async def test_the_feed_shows_only_surfaced_findings(self, db):
        e = await _entity(db)
        c = await _claim(db, e)
        p = await _prediction(db, e)
        await record(db.pool, assess(_candidate(c), [_bucket(0.7, 40, 34)]),
                     entity_id=e, prediction_id=p)
        await record(db.pool, assess(_candidate(c), []), entity_id=e)

        feed = await briefing(db.pool)
        assert len(feed) == 1
        assert feed[0]["method"] == "detect"

    async def test_a_private_finding_is_not_in_another_users_feed(self, db):
        """The redistribution rule, reaching the thing that speaks unprompted."""
        e = await _entity(db)
        owner = uuid4()
        c = await _claim(db, e, owner=owner)
        p = await _prediction(db, e)
        await record(db.pool, assess(_candidate(c), [_bucket(0.7, 40, 34)]),
                     entity_id=e, audience_user_id=owner, prediction_id=p)

        assert len(await briefing(db.pool, audience=owner)) == 1
        assert await briefing(db.pool, audience=uuid4()) == []
        assert await briefing(db.pool) == []


class TestScorecard:
    async def _surface(self, db, e, c, outcome, direction="up"):
        p = await _prediction(db, e, direction=direction, outcome=outcome)
        await record(db.pool, assess(_candidate(c), [_bucket(0.7, 40, 34)]),
                     entity_id=e, prediction_id=p)

    async def test_accuracy_is_reported_on_what_was_surfaced(self, db):
        e = await _entity(db)
        c = await _claim(db, e)
        for _ in range(8):
            await self._surface(db, e, c, "upper")
        for _ in range(4):
            await self._surface(db, e, c, "lower")

        card = (await scorecard(db.pool))[0]
        assert card["surfaced"] == 12
        assert card["resolved"] == 12
        assert card["hits"] == 8
        assert card["hit_rate"] == pytest.approx(8 / 12)

    async def test_a_thin_scorecard_reports_no_rate(self, db):
        """A hit rate from three resolved predictions is noise wearing a
        percentage sign."""
        e = await _entity(db)
        c = await _claim(db, e)
        for _ in range(3):
            await self._surface(db, e, c, "upper")
        assert (await scorecard(db.pool))[0]["hit_rate"] is None

    async def test_unresolved_predictions_are_not_wins(self, db):
        e = await _entity(db)
        c = await _claim(db, e)
        for _ in range(10):
            await self._surface(db, e, c, None)
        card = (await scorecard(db.pool))[0]
        assert card["surfaced"] == 10
        assert card["resolved"] == 0
        assert card["hit_rate"] is None

    async def test_refusals_are_countable_by_reason(self, db):
        e = await _entity(db)
        c = await _claim(db, e)
        await record(db.pool, assess(_candidate(c), []), entity_id=e)
        await record(db.pool, assess(_candidate(c, falsifiable=False),
                                     [_bucket(0.7, 40, 34)]), entity_id=e)
        counts = await refusal_counts(db.pool)
        assert counts[Refusal.UNCALIBRATED.value] == 1
        assert counts[Refusal.NOT_FALSIFIABLE.value] == 1

    async def test_an_operators_byo_findings_do_not_leak_into_another_scorecard(self, db):
        """The redistribution rule reaches the scorecard. A second operator's
        byo-derived hit rate is a deterministic function of their licensed
        demand; aggregating it into another operator's published accuracy would
        make this deployment the redistributor -- the leak migration 028 closes.
        """
        e = await _entity(db)
        c = await _claim(db, e)
        owner = uuid4()
        other = uuid4()
        for _ in range(12):
            p = await _prediction(db, e, direction="up", outcome="upper")
            await record(db.pool, assess(_candidate(c), [_bucket(0.7, 40, 34)]),
                         entity_id=e, audience_user_id=owner, prediction_id=p)

        owner_card = (await scorecard(db.pool, audience=owner))[0]
        assert owner_card["surfaced"] == 12
        assert owner_card["hits"] == 12

        # A different operator sees none of the owner's byo-derived findings,
        # and so does an anonymous caller. A global aggregate (the pre-028
        # behaviour) would surface 12 to both.
        assert await scorecard(db.pool, audience=other) == []
        assert await scorecard(db.pool) == []

    async def test_an_operators_refusals_do_not_leak_to_another_operator(self, db):
        e = await _entity(db)
        c = await _claim(db, e)
        owner = uuid4()
        other = uuid4()
        await record(db.pool, assess(_candidate(c), []),
                     entity_id=e, audience_user_id=owner)

        assert await refusal_counts(db.pool, audience=owner) == {
            Refusal.UNCALIBRATED.value: 1
        }
        assert await refusal_counts(db.pool, audience=other) == {}
        assert await refusal_counts(db.pool) == {}


class TestClaimlessFindings:
    """A finding need not be anchored to a single claim. The schema now allows
    claim_id to be NULL provided `supporting` names the evidence -- but the
    pre-existing falsifiability invariant on surfaced findings is untouched, and
    the new CHECK rejects a claim-less finding that names nothing.
    """

    async def test_a_claimless_surfaced_finding_without_a_prediction_is_rejected(self, db):
        """Relaxing claim_id does not relax falsifiability: surfaced_findings_
        are_falsifiable is a separate CHECK on prediction_id. A surfaced,
        claim-less, prediction-less row is rejected exactly as before."""
        e = await _entity(db)
        with pytest.raises(asyncpg.IntegrityConstraintViolationError) as exc:
            await db.pool.execute(
                "INSERT INTO finding (claim_id, entity_id, status, method, "
                "confidence, threshold, supporting) "
                "VALUES (NULL,$1,'surfaced','detect',0.85,0.7,"
                "'[\"input claim a\"]'::jsonb)",
                e,
            )
        assert exc.value.constraint_name == "surfaced_findings_are_falsifiable"

    async def test_a_claimless_refused_finding_with_supporting_is_stored(self, db):
        """The new path: a refused finding (no prediction required) anchored to
        no claim, with its evidence named in `supporting`. Confirms a None
        claim_id passes through publish.record() into a nullable UUID column."""
        e = await _entity(db)
        v = assess(_candidate(None, supporting=("input claim a",)), [])
        assert v.refusal is Refusal.UNCALIBRATED
        fid = await record(db.pool, v, entity_id=e)

        row = await db.pool.fetchrow(
            "SELECT status, claim_id, supporting FROM finding WHERE id=$1", fid
        )
        assert row["status"] == "refused"
        assert row["claim_id"] is None
        assert "input claim a" in row["supporting"]

    async def test_a_claimless_surfaced_finding_with_empty_supporting_is_rejected(
        self, db
    ):
        """The whole point of the CHECK: a finding that speaks unprompted while
        anchored to no claim and naming no evidence is advocacy, and must not be
        stored. A real prediction is supplied so surfaced_findings_are_falsifiable
        does not fire -- the evidence CHECK is the only thing being exercised."""
        e = await _entity(db)
        p = await _prediction(db, e)
        with pytest.raises(asyncpg.IntegrityConstraintViolationError) as exc:
            await db.pool.execute(
                "INSERT INTO finding (claim_id, entity_id, status, method, "
                "confidence, threshold, prediction_id, supporting) "
                "VALUES (NULL,$1,'surfaced','detect',0.85,0.7,$2,'[]'::jsonb)",
                e, p,
            )
        assert exc.value.constraint_name == "surfaced_findings_name_their_evidence"

    async def test_a_refusal_needs_no_evidence_only_a_reason(self, db):
        """A refusal's traceability is its reason, not an evidence list. 031
        scoped the evidence CHECK to surfaced rows for exactly this: the gate
        refusing a candidate whose disconfirming search could not run has, by
        definition, no evidence to name."""
        e = await _entity(db)
        fid = await db.pool.fetchval(
            "INSERT INTO finding (claim_id, entity_id, status, method, "
            "confidence, refusal, supporting) "
            "VALUES (NULL,$1,'refused','detect',0.5,"
            "'no_disconfirming_evidence_was_gathered','[]'::jsonb) RETURNING id",
            e,
        )
        row = await db.pool.fetchrow(
            "SELECT status, supporting, refusal FROM finding WHERE id=$1", fid
        )
        assert row["status"] == "refused"
        assert row["supporting"] == "[]"
        assert row["refusal"] == "no_disconfirming_evidence_was_gathered"

    async def test_a_refusal_still_needs_a_reason(self, db):
        """Scoping the evidence CHECK must not have opened a hole: a refusal
        naming neither evidence nor reason is still untraceable and rejected."""
        e = await _entity(db)
        with pytest.raises(asyncpg.IntegrityConstraintViolationError) as exc:
            await db.pool.execute(
                "INSERT INTO finding (claim_id, entity_id, status, method, "
                "confidence, supporting) "
                "VALUES (NULL,$1,'refused','detect',0.5,'[]'::jsonb)",
                e,
            )
        assert exc.value.constraint_name == "refusal_names_a_reason"

    async def test_a_claimless_surfaced_finding_with_a_real_prediction_is_allowed(self, db):
        """Proves the relaxation does not block a surfaced claim-less finding in
        principle. The prediction here is a SYNTHETIC price triple-barrier
        fixture (a real entry_price/upper_barrier/lower_barrier); it demonstrates
        the CHECK permits the row, NOT that any real non-price analysis can
        produce a scorable prediction. That question is explicitly open -- see
        the D11 report."""
        e = await _entity(db)
        p = await _prediction(db, e)
        v = assess(
            _candidate(None, supporting=("input claim a",),
                       disconfirming=("risk event",)),
            [_bucket(0.7, 40, 34)],
        )
        assert v.surfaced
        fid = await record(db.pool, v, entity_id=e, prediction_id=p)

        row = await db.pool.fetchrow(
            "SELECT status, claim_id, prediction_id FROM finding WHERE id=$1", fid
        )
        assert row["status"] == "surfaced"
        assert row["claim_id"] is None
        assert row["prediction_id"] is not None


class TestPayoffGeometry:
    """B05: directional payoff accounting. Risk and reward distances are keyed
    on direction, expiry exits use the recorded exit_price, and neutral calls
    carry no directional payoff."""

    async def _surfaced(self, db, method, *, direction, outcome, entry, upper,
                        lower, exit_price=None, audience=None):
        e = await _entity(db, symbol=f"{method}-{uuid4().hex[:8]}")
        pid = await db.pool.fetchval(
            """
            INSERT INTO prediction (entity_id, method, direction, confidence,
                                    entry_price, upper_barrier, lower_barrier,
                                    horizon_ends_at, provenance, outcome,
                                    resolved_at, exit_price, exit_at)
            VALUES ($1,$2,$3,0.85,$4,$5,$6,now(),'{}'::jsonb,
                    $7::prediction_outcome, now(), $8, $9)
            RETURNING id
            """,
            e, method, direction, entry, upper, lower, outcome, exit_price,
            NOW if exit_price is not None else None,
        )
        await db.pool.execute(
            "INSERT INTO finding (claim_id, entity_id, prediction_id, status, "
            "method, confidence, threshold, supporting, audience_user_id) "
            "VALUES (NULL,$1,$2,'surfaced',$3,0.85,0.7,'[\"x\"]'::jsonb,$4)",
            e, pid, method, audience,
        )
        return pid

    async def _refresh(self, db):
        from omni.conviction.stats_refresh import refresh_statistics

        await refresh_statistics(db.pool)

    async def test_short_geometry_is_scored_not_excluded(self, db):
        """A short risks upper-entry and targets entry-lower. The 068 view
        demanded lower>entry for shorts -- impossible under the straddle
        constraint -- so every short silently vanished from payoff."""
        await self._surfaced(db, "short.hit", direction="down",
                             outcome="upper", entry=100, upper=110, lower=80)
        await self._refresh(db)

        row = await db.pool.fetchrow(
            "SELECT * FROM finding_payoff WHERE method='short.hit'"
        )
        assert row is not None, "the short must be scored, not excluded"
        assert float(row["avg_risk_pct"]) == pytest.approx(10.0)
        assert float(row["avg_payoff_pct"]) == pytest.approx(20.0)
        assert float(row["avg_realized_ratio"]) == pytest.approx(-1.0)
        assert row["geometry_n"] == 1
        assert row["realized_n"] == 1

    async def test_a_profitable_expiry_is_measured_not_scored_minus_one(self, db):
        """A long expiring at 105 with entry 100 / stop 90 is +0.5R. The 068
        view scored every non-hit -1.0."""
        await self._surfaced(db, "expiry.win", direction="up", outcome="expiry",
                             entry=100, upper=110, lower=90, exit_price=105)
        await self._refresh(db)

        row = await db.pool.fetchrow(
            "SELECT * FROM finding_payoff WHERE method='expiry.win'"
        )
        assert float(row["avg_realized_ratio"]) == pytest.approx(0.5)
        assert row["realized_n"] == 1

    async def test_an_unmeasured_expiry_has_no_realized_ratio(self, db):
        """Legacy expiry rows with no exit_price stay out of the realized
        average rather than being priced by invention."""
        await self._surfaced(db, "expiry.blind", direction="up",
                             outcome="expiry", entry=100, upper=110, lower=90)
        await self._refresh(db)

        row = await db.pool.fetchrow(
            "SELECT * FROM finding_payoff WHERE method='expiry.blind'"
        )
        assert row["geometry_n"] == 1
        assert row["realized_n"] == 0
        assert row["avg_realized_ratio"] is None

    async def test_a_neutral_call_carries_no_directional_payoff(self, db):
        """A neutral expiry is a classification hit (calibration_bucket keeps
        it) but has no risk leg; the 068 view gave it a directional payoff."""
        await self._surfaced(db, "neutral.expiry", direction="neutral",
                             outcome="expiry", entry=100, upper=110, lower=90)
        await self._refresh(db)

        row = await db.pool.fetchrow(
            "SELECT * FROM finding_payoff WHERE method='neutral.expiry'"
        )
        assert row is None

    async def test_a_long_stopout_scores_minus_one_r(self, db):
        await self._surfaced(db, "long.stop", direction="up", outcome="lower",
                             entry=100, upper=110, lower=90)
        await self._refresh(db)

        row = await db.pool.fetchrow(
            "SELECT * FROM finding_payoff WHERE method='long.stop'"
        )
        assert float(row["avg_risk_pct"]) == pytest.approx(10.0)
        assert float(row["avg_payoff_pct"]) == pytest.approx(10.0)
        assert float(row["avg_realized_ratio"]) == pytest.approx(-1.0)

    async def test_the_scorecard_weights_audiences_by_their_samples(self, db):
        """B06: a 2-observation private row must not outweigh its share of a
        100-observation shared row. Mean of means says 5.0; the weighted
        answer is 118/102."""
        for _ in range(100):
            await self._surfaced(db, "weighted.mix", direction="up",
                                 outcome="upper", entry=100, upper=110,
                                 lower=90)
        owner = uuid4()
        for _ in range(2):
            await self._surfaced(db, "weighted.mix", direction="up",
                                 outcome="upper", entry=100, upper=190,
                                 lower=90, audience=owner)
        await self._refresh(db)

        card = (await scorecard(db.pool, audience=owner))[0]
        assert card["method"] == "weighted.mix"
        assert card["payoff_ratio"] == pytest.approx(1.16, abs=0.005)
        assert card["avg_risk_pct"] == 10.0
        assert card["avg_payoff_pct"] == round((100 * 10.0 + 2 * 90.0) / 102, 2)
