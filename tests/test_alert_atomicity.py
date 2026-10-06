"""Alert firing atomicity and numeric hardening (audit findings A09, A16).

Firing and notification enqueueing used to be separate transactions with a
returned set that did not match what was written under concurrency: a crash
between them lost the notification forever (the firing record makes
evaluation skip the claim next time), and two racing evaluations could each
report the other's firings. Numeric inputs could be non-finite or so large
that evaluation itself raised every cycle. And sequence conditions were
computed over the interleaving of unrelated publishers' series,
manufacturing crossings out of two steady series.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.alerts import notify
from omni.alerts.rules import InvalidCondition, evaluate, validate_condition

WEBHOOK = "https://hooks.example.com/d/abc"


async def _user(db, email) -> uuid4:
    return await db.pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id",
        email,
    )


async def _entity(db, symbol="AAPL") -> uuid4:
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company', $1, $1) RETURNING id",
        symbol,
    )


async def _claim(
    db, entity_id, key, *, value, source="src_a", knowledge_date=None
) -> uuid4:
    now = datetime.now(UTC)
    kd = knowledge_date or now
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1,'price_snapshot',$2,$3::jsonb,$4,$5,$6,0.9,'allowed',NULL)
        RETURNING id
        """,
        entity_id,
        key,
        json.dumps(value),
        source,
        kd - timedelta(days=1),
        kd,
    )


async def _alert(db, *, user_id, entity_id, condition):
    return await db.pool.fetchrow(
        "INSERT INTO alert (user_id, entity_id, claim_type, condition) "
        "VALUES ($1, $2, 'price_snapshot', $3::jsonb) RETURNING *",
        user_id,
        entity_id,
        json.dumps(condition),
    )


async def _user_with_webhook(db, user_id) -> None:
    await db.pool.execute(
        """
        INSERT INTO user_settings (user_id, data)
        VALUES ($1, jsonb_build_object('notify', jsonb_build_object(
            'webhook_url', $2::text
        )))
        """,
        user_id,
        WEBHOOK,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, users CASCADE")
    yield


class TestFiringAndEnqueueAreOneTransaction:
    async def test_a_crash_in_enqueue_rolls_the_firing_back(self, db):
        # The A09 durability core: if the notification rows cannot be
        # written, the firing must not commit either -- a committed firing
        # with no notification is undeliverable forever, because evaluation
        # skips already-fired claims.
        user = await _user(db, "atomic@example.com")
        entity = await _entity(db)
        await _claim(db, entity, "close", value={"value": 150})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            condition={"kind": "value_above", "threshold": 100},
        )
        await _user_with_webhook(db, user)

        async def _broken_notify(conn, new):
            raise RuntimeError("queue write failed")

        with pytest.raises(RuntimeError):
            await evaluate(db.pool, alert, audience=user, notify=_broken_notify)

        firings = await db.pool.fetchval(
            "SELECT count(*) FROM alert_firing WHERE alert_id = $1", alert["id"]
        )
        queued = await db.pool.fetchval(
            "SELECT count(*) FROM notification_delivery WHERE alert_id = $1",
            alert["id"],
        )
        assert firings == 0, "a firing committed without its notification"
        assert queued == 0

    async def test_the_scheduler_enqueue_hook_runs_inside_the_transaction(self, db):
        from omni.scheduler.worker import evaluate_alerts_once

        user = await _user(db, "worker-atomic@example.com")
        entity = await _entity(db, symbol="MSFT")
        await _claim(db, entity, "close", value={"value": 150})
        await _alert(
            db,
            user_id=user,
            entity_id=entity,
            condition={"kind": "value_above", "threshold": 100},
        )
        await _user_with_webhook(db, user)

        fired = await evaluate_alerts_once(db.pool)
        assert fired == 1
        queued = await db.pool.fetchval(
            "SELECT count(*) FROM notification_delivery WHERE user_id = $1",
            user,
        )
        assert queued == 1, "evaluate_alerts_once fired without enqueueing"

    async def test_racing_evaluations_do_not_double_enqueue(self, db):
        # Both evaluations see the satisfying claim; the INSERT ...
        # RETURNING dedup means only the evaluation that actually inserted
        # reports the firing and runs the enqueue hook. The unique index
        # arbitrates even when both are mid-transaction: the loser's INSERT
        # waits for the winner's commit and then conflicts to nothing.
        user = await _user(db, "race@example.com")
        entity = await _entity(db, symbol="TSLA")
        await _claim(db, entity, "close", value={"value": 150})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            condition={"kind": "value_above", "threshold": 100},
        )
        await _user_with_webhook(db, user)

        enqueued: list[int] = []

        async def _notify(conn, new):
            # The real enqueue, on the firing transaction's connection.
            enqueued.append(len(new))
            await notify.dispatch(db.pool, alert, new, conn=conn)
            await asyncio.sleep(0.05)

        async def run():
            return await evaluate(db.pool, alert, audience=user, notify=_notify)

        first, second = await asyncio.gather(run(), run())
        assert len(first) + len(second) == 1, (
            "two racing evaluations reported the same firing"
        )
        assert sum(enqueued) == 1

        queued = await db.pool.fetchval(
            "SELECT count(*) FROM notification_delivery WHERE alert_id = $1",
            alert["id"],
        )
        assert queued == 1


class TestNumericValidation:
    def test_nan_and_infinities_are_refused_not_stored(self):
        # json.loads parses these literals; a NaN threshold passes every
        # isinstance check and then silently never fires.
        with pytest.raises(InvalidCondition):
            validate_condition(
                {"kind": "value_above", "threshold": float("nan")}
            )
        with pytest.raises(InvalidCondition):
            validate_condition(
                {"kind": "value_above", "threshold": float("inf")}
            )

    def test_a_huge_integer_threshold_is_refused_not_overflowed(self):
        # int -> float conversion of 10**400 raises OverflowError, which
        # used to escape as a 500 at creation time.
        with pytest.raises(InvalidCondition):
            validate_condition(
                {"kind": "value_above", "threshold": 10**400}
            )

    def test_pct_and_window_bounds(self):
        with pytest.raises(InvalidCondition):
            validate_condition(
                {"kind": "pct_change_above", "pct": float("inf"), "window_days": 30}
            )
        with pytest.raises(InvalidCondition):
            validate_condition(
                {"kind": "pct_change_above", "pct": 10, "window_days": 10**30}
            )

    def test_staleness_seconds_are_bounded(self):
        with pytest.raises(InvalidCondition):
            validate_condition(
                {"kind": "staleness_exceeds", "seconds": 10**30}
            )

    def test_honest_values_still_pass(self):
        assert validate_condition(
            {"kind": "value_above", "threshold": 100.5}
        ) == {"kind": "value_above", "threshold": 100.5, "field": "value"}
        assert validate_condition(
            {"kind": "pct_change_below", "pct": 2.5, "window_days": 30}
        ) == {
            "kind": "pct_change_below",
            "pct": 2.5,
            "window_days": 30.0,
            "field": "value",
        }


class TestPerSourceSeries:
    async def test_two_steady_publishers_do_not_manufacture_crossings(self, db):
        # Publisher A sits above the threshold on every claim; publisher B
        # sits below on every claim. Interleaved, every A-after-B pair read
        # as a re-crossing: four firings from two series that each moved
        # once. Per-source, A crosses once and B never does.
        user = await _user(db, "series@example.com")
        entity = await _entity(db, symbol="NVDA")
        base = datetime(2026, 7, 1, tzinfo=UTC)
        a_ids = []
        for day in range(4):
            a_ids.append(
                await _claim(
                    db, entity, "close",
                    value={"value": 150 + day},
                    source="alpha",
                    knowledge_date=base + timedelta(days=day),
                )
            )
            await _claim(
                db, entity, "close",
                value={"value": 50 + day},
                source="beta",
                knowledge_date=base + timedelta(days=day),
            )
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            condition={"kind": "value_above", "threshold": 100},
        )
        fired = await evaluate(db.pool, alert, audience=user)
        assert [c["id"] for c in fired] == [a_ids[0]], (
            f"interleaved unrelated series produced {len(fired)} crossings"
        )

    async def test_a_pct_change_base_comes_from_the_same_series(self, db):
        # Alpha rises 100 -> 140 (+40%); beta holds 100 flat and is newer.
        # Interleaved, the "most recent claim a window older" could land on
        # a beta row and the alpha move would be diluted or missed.
        user = await _user(db, "pct-series@example.com")
        entity = await _entity(db, symbol="AMD")
        base = datetime(2026, 7, 1, tzinfo=UTC)
        await _claim(
            db, entity, "close", value={"value": 100},
            source="alpha", knowledge_date=base,
        )
        top = await _claim(
            db, entity, "close", value={"value": 140},
            source="alpha", knowledge_date=base + timedelta(days=30),
        )
        await _claim(
            db, entity, "close", value={"value": 100},
            source="beta", knowledge_date=base + timedelta(days=15),
        )
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            condition={
                "kind": "pct_change_above", "pct": 30, "window_days": 30
            },
        )
        fired = await evaluate(db.pool, alert, audience=user)
        assert [c["id"] for c in fired] == [top]

    async def test_claims_without_the_watched_field_are_not_bases(self, db):
        # A claim whose value object lacks the field is a different series
        # as far as the base window is concerned.
        user = await _user(db, "fieldless@example.com")
        entity = await _entity(db, symbol="INTC")
        base = datetime(2026, 7, 1, tzinfo=UTC)
        await _claim(
            db, entity, "close",
            value={"value": 200},
            source="alpha",
            knowledge_date=base + timedelta(days=40),
        )
        await db.pool.execute(
            """
            INSERT INTO claim (entity_id, claim_type, key, value, source,
                               event_date, knowledge_date, confidence,
                               redistributable, audience_user_id)
            VALUES ($1,'price_snapshot','close',$2::jsonb,'alpha',$3,$4,0.9,
                    'allowed',NULL)
            """,
            entity,
            json.dumps({"other_field": 100}),
            base + timedelta(days=9),
            base + timedelta(days=10),
        )
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            condition={
                "kind": "pct_change_above", "pct": 50, "window_days": 30
            },
        )
        # No in-series base at least 30 days older exists: honest refusal.
        assert await evaluate(db.pool, alert, audience=user) == []
