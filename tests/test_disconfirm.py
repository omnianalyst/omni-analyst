"""The disconfirming search. Every test here is about the difference between
"we looked and found nothing" and "we never looked".

The bug this file exists to catch: ``searched_for_disconfirming`` was a
hardcoded ``True`` at the only production call site, so the gate's refusal of
one-sided findings could never fire and the UI's "disconfirming: none found"
asserted a search that had not happened.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.conviction.disconfirm import Evidence, gather_evidence


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    await db.pool.execute("TRUNCATE claim CASCADE")
    yield


BASE = datetime(2025, 1, 1, tzinfo=UTC)


def _noisy_uptrend(n: int, *, start: float = 100.0, drift: float = 1.0) -> list[float]:
    """A rising series with real session-to-session variation.

    A straight ramp is not a usable fixture for anything reading realized vol:
    its stdev is near zero, so any vol-normalised quantity explodes. The
    deterministic zig-zag keeps the test reproducible while giving the vol
    something to measure.
    """
    return [start + i * drift + (1.5 if i % 2 else -1.5) for i in range(n)]


async def _entity(db, symbol="X"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company',$1,$1) RETURNING id",
        symbol,
    )


async def _price(db, entity_id, close, event_date):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'price_snapshot','close',$2::jsonb,'poly',$3,$3,1.0,'allowed')",
        entity_id, json.dumps({"close": close}), event_date,
    )


async def _series(db, entity_id, closes, start=BASE):
    for i, c in enumerate(closes):
        await _price(db, entity_id, c, start + timedelta(days=i))
    return start + timedelta(days=len(closes) - 1)


async def _regime(db, *, known_at=BASE, **value):
    macro = await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('macro','US_MACRO','US macro') RETURNING id"
    )
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, event_date, "
        "knowledge_date, confidence, redistributable) "
        "VALUES ($1,'regime_assessment','us_macro',$2::jsonb,'fred',$3,$3,1.0,'allowed')",
        macro, json.dumps(value), known_at,
    )


class TestSearchedIsNotAConstant:
    async def test_no_price_history_means_nothing_was_searched(self, db):
        """The load-bearing case. Without the price window there is no search,
        and the gate must be told so rather than handed a default True."""
        e = await _entity(db)
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=BASE,
        )
        assert ev.searched is False
        assert ev.supporting == ()
        assert ev.disconfirming == ()

    async def test_short_history_means_nothing_was_searched(self, db):
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i for i in range(20)])
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert ev.searched is False

    async def test_enough_history_means_the_search_ran(self, db):
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i for i in range(60)])
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert ev.searched is True

    async def test_a_flat_series_is_not_a_search_however_long(self, db):
        """The poison-row case. Fifty identical closes give a zero vol (check 2
        skipped) and a price sitting on its own average (check 1 finds no
        direction), so nothing was actually examined. Reporting searched=True
        here produced a candidate the gate passed with an empty evidence list,
        which `surfaced_findings_name_their_evidence` then rejected at insert
        time -- aborting the surfacing pass and, because rows are ordered by
        entity, blocking every prediction behind it on every later pass too."""
        e = await _entity(db)
        as_of = await _series(db, e, [100.0] * 60)
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert ev.searched is False
        assert ev.supporting == ()
        assert ev.disconfirming == ()

    async def test_a_supported_method_with_a_live_series_still_searches(self, db):
        """The guard above must not have made the whole search unreachable."""
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i for i in range(60)])
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma.w50", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert ev.searched is True
        assert ev.supporting

    async def test_an_unsupported_method_reports_no_search(self, db):
        """These checks read a moving average because that is what a trend call
        rests on. A DCF call is predicated on cash flows -- the same sentences
        would state the wrong thing about what invalidates it. No search exists
        for that method, so it is refused rather than given borrowed evidence.

        `supported` is reported separately from `searched`: an unwritten search
        is an unfinished part of the product, and reporting it as "could not
        gather evidence" would file a build gap under a data gap and hide it.
        """
        e = await _entity(db)
        as_of = await _series(db, e, _noisy_uptrend(60))
        ev = await gather_evidence(
            db.pool, entity_id=e, method="fundamentals.dcf_valuation",
            direction="up", audience=None, as_of=as_of, window=50,
        )
        assert ev.searched is False
        assert ev.supported is False
        assert ev.supporting == ()
        assert ev.disconfirming == ()

    async def test_a_data_gap_and_a_missing_search_are_different_refusals(self, db):
        """Both refuse, but for different reasons an operator acts on
        differently: one is fixed by ingesting prices, the other by writing
        code."""
        e = await _entity(db)
        no_prices = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=BASE,
        )
        assert no_prices.searched is False
        assert no_prices.supported is True, "trend.sma has a search; the data was missing"

    async def test_the_search_is_point_in_time(self, db):
        """Evidence gathered from prices the call could not have seen would be
        judging it with hindsight. An as_of before the series is no search."""
        e = await _entity(db)
        await _series(db, e, [100.0 + i for i in range(60)])
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=BASE - timedelta(days=1), window=50,
        )
        assert ev.searched is False


class TestSlowerTrendCheck:
    async def test_a_reversal_disconfirms_the_call(self, db):
        """100 closes that fell hard then part-recovered: the fast 50-day trend
        is up, but price is still below the 100-day average, so the slow trend
        is down. The call must carry that against it."""
        e = await _entity(db)
        closes = [300.0 - i for i in range(50)] + [190.0 + i for i in range(50)]
        as_of = await _series(db, e, closes)
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert ev.searched is True
        assert any("slower" in d and "down" in d for d in ev.disconfirming), (
            ev.disconfirming
        )

    async def test_an_aligned_slow_trend_supports_instead(self, db):
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i for i in range(100)])
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert any("slower" in s for s in ev.supporting), ev.supporting
        assert not any("slower" in d for d in ev.disconfirming), ev.disconfirming


class TestProximityToInvalidation:
    async def test_a_call_hugging_its_moving_average_is_disconfirmed(self, db):
        """The MA is the invalidation barrier. Entering next to it means one
        ordinary session proves the call wrong, however the confidence reads."""
        e = await _entity(db)
        # A noisy flat series: last close lands within a fraction of a vol of
        # the mean, so the distance to invalidation is tiny.
        closes = [100.0 + (2.0 if i % 2 else -2.0) for i in range(60)]
        closes[-1] = 100.05
        as_of = await _series(db, e, closes)
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert any("volatilities from the average" in d for d in ev.disconfirming), (
            ev.disconfirming
        )

    async def test_an_absurd_vol_ratio_is_not_quoted_as_a_number(self, db):
        """A perfectly smooth ramp has a vol near zero without being flat, so
        the distance ratio explodes -- the scale benchmark surfaced "price is
        434.5 volatilities up of its 50-day average", which is arithmetically
        true and useless to read. Real analogues: a pegged rate, a halted quote,
        an interpolated series."""
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i * 0.5 for i in range(60)])
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert ev.searched is True
        joined = " ".join(ev.supporting)
        assert "too smooth for the ratio to be meaningful" in joined, ev.supporting
        # The fact still travels -- direction is reported, only the ratio is not.
        assert "far above" in joined

    async def test_a_call_far_from_its_average_is_supported(self, db):
        e = await _entity(db)
        # A noisy uptrend, not a straight line: a perfectly linear ramp has a
        # vol near zero and trips the smoothness guard instead.
        as_of = await _series(db, e, _noisy_uptrend(60))
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert any("volatilities above its" in s for s in ev.supporting), ev.supporting


class TestMacroOpposition:
    async def test_risk_off_argues_against_an_up_call(self, db):
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i for i in range(60)])
        await _regime(db, risk_regime="risk_off", cycle_phase="contraction")
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert any("risk off" in d for d in ev.disconfirming), ev.disconfirming
        assert any("contraction" in d for d in ev.disconfirming), ev.disconfirming

    async def test_risk_off_supports_a_down_call(self, db):
        e = await _entity(db)
        as_of = await _series(db, e, [200.0 - i for i in range(60)])
        await _regime(db, risk_regime="risk_off", cycle_phase="contraction")
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="down",
            audience=None, as_of=as_of, window=50,
        )
        assert not any("risk off" in d for d in ev.disconfirming), ev.disconfirming

    async def test_a_regime_published_after_the_call_is_invisible(self, db):
        """Point-in-time, like every other read. A call written in March judged
        against June's regime is being second-guessed with information that did
        not exist when it was made -- which would make a historical backfill
        sweep look better than the live path it is meant to predict."""
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i for i in range(60)])
        await _regime(
            db,
            known_at=as_of + timedelta(days=30),
            risk_regime="risk_off",
            cycle_phase="contraction",
        )
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert ev.searched is True
        assert not any("risk off" in d for d in ev.disconfirming), ev.disconfirming
        assert not any("contraction" in d for d in ev.disconfirming), ev.disconfirming

    async def test_absent_regime_removes_the_check_not_the_search(self, db):
        """No macro coverage is a legitimate state. It must not make the whole
        search read as un-run, or the system can never speak before FRED lands."""
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i for i in range(60)])
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert ev.searched is True
        assert not any("regime" in d for d in ev.disconfirming)


class TestEntityRecord:
    async def _resolved(
        self, db, entity_id, *, n, hits, direction="up", resolved_at=None
    ):
        at = resolved_at or BASE + timedelta(days=1)
        for i in range(n):
            outcome = "upper" if i < hits else "lower"
            await db.pool.execute(
                "INSERT INTO prediction (entity_id, method, direction, confidence, "
                "entry_price, upper_barrier, lower_barrier, horizon_ends_at, "
                "outcome, resolved_at, provenance) "
                "VALUES ($1,'trend.sma',$2,0.7,100,110,90,$3,$4,$3,'{}'::jsonb)",
                entity_id, direction, at, outcome,
            )

    async def test_a_record_resolved_after_the_call_is_invisible(self, db):
        """A call cannot cite a track record assembled after it was made. The
        same hindsight leak as the regime check, on the other input."""
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i for i in range(60)])
        await self._resolved(
            db, e, n=12, hits=3, resolved_at=as_of + timedelta(days=30)
        )
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert ev.searched is True
        assert not any("of the time" in d for d in ev.disconfirming), ev.disconfirming

    async def test_a_poor_record_on_this_name_disconfirms(self, db):
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i for i in range(60)])
        await self._resolved(db, e, n=12, hits=3)
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert any("only 25% of the time" in d for d in ev.disconfirming), (
            ev.disconfirming
        )

    async def test_a_thin_record_says_nothing_either_way(self, db):
        """Below the calibration floor the record is not evidence. Reporting it
        would let three lucky calls read as a track record."""
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i for i in range(60)])
        await self._resolved(db, e, n=3, hits=0)
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert not any("of the time" in d for d in ev.disconfirming), ev.disconfirming


class TestEvidenceShape:
    async def test_supporting_is_never_a_restatement_of_the_call(self, db):
        """The old supporting string was "up directional call from trend.sma" --
        the call restating itself as its own evidence. Every supporting reason
        must name a fact from coverage, not the conclusion."""
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i for i in range(60)])
        ev = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=None, as_of=as_of, window=50,
        )
        assert ev.supporting
        for s in ev.supporting:
            assert "directional call from" not in s

    async def test_an_unsearched_result_carries_no_evidence_at_all(self, db):
        ev = Evidence(searched=False)
        assert ev.supporting == ()
        assert ev.disconfirming == ()

    async def test_no_output_matches_the_retired_tautology_phrasing(self, db):
        """Migration 032 deletes pre-search findings by matching the exact
        phrase the old producer emitted. If the new search ever emitted it too,
        that migration would delete honest rows on every deploy."""
        e = await _entity(db)
        as_of = await _series(db, e, [100.0 + i for i in range(60)])
        for direction in ("up", "down"):
            ev = await gather_evidence(
                db.pool, entity_id=e, method="trend.sma", direction=direction,
                audience=None, as_of=as_of, window=50,
            )
            for text in (*ev.supporting, *ev.disconfirming):
                assert "directional call from" not in text


class TestAudienceScoping:
    async def test_another_users_prices_are_not_visible(self, db):
        """byo_only price coverage belongs to its owner. Gathering evidence from
        someone else's prices would make this deployment the redistributor."""
        e = await _entity(db)
        owner = uuid4()
        await db.pool.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ($1,$2,'x')",
            owner, f"{owner}@x.com",
        )
        for i in range(60):
            await db.pool.execute(
                "INSERT INTO claim (entity_id, claim_type, key, value, source, "
                "event_date, knowledge_date, confidence, redistributable, "
                "audience_user_id) VALUES "
                "($1,'price_snapshot','close',$2::jsonb,'poly',$3,$3,1.0,"
                "'byo_only',$4)",
                e, json.dumps({"close": 100.0 + i}), BASE + timedelta(days=i), owner,
            )
        as_of = BASE + timedelta(days=59)

        mine = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=owner, as_of=as_of, window=50,
        )
        theirs = await gather_evidence(
            db.pool, entity_id=e, method="trend.sma", direction="up",
            audience=uuid4(), as_of=as_of, window=50,
        )
        assert mine.searched is True
        assert theirs.searched is False
