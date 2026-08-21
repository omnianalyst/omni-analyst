"""Alerts as conditions over coverage.

An alert is a fixed predicate over the claims the owner may see, evaluated
through visible_claims, and a firing is recorded so it cannot repeat. These
tests hold the behaviours the work order names: each of the four conditions
firing and not firing; an alert that does not re-fire on the same claim; the
redistribution leak an alert set on another user's private claim must not
produce; an unknown condition refused at the gate; and a staleness alert firing
from the passage of time alone, which no value threshold can express.
"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from neutron.auth.jwt import create_token
from neutron.test import TestClient

from omni.alerts.rules import KNOWN_KINDS, InvalidCondition, evaluate, validate_condition
from omni.api.alerts import build_router
from omni.main import create_app
from omni.scheduler.worker import evaluate_alerts_once


async def _user(db, email) -> uuid4:
    return await db.pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id",
        email,
    )


async def _entity(db, kind="company", symbol="AAPL", name=None) -> uuid4:
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ($1, $2, $3) RETURNING id",
        kind,
        symbol,
        name or symbol,
    )


async def _claim(
    db,
    entity_id,
    key,
    *,
    value,
    source="src_a",
    owner=None,
    claim_type="price_snapshot",
    event_date=None,
    knowledge_date=None,
    confidence=0.9,
) -> uuid4:
    shared = owner is None
    now = datetime.now(UTC)
    kd = knowledge_date or now
    ed = event_date or (kd - timedelta(days=1))
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10)
        RETURNING id
        """,
        entity_id,
        claim_type,
        key,
        json.dumps(value),
        source,
        ed,
        kd,
        confidence,
        "allowed" if shared else "byo_only",
        owner,
    )


async def _alert(db, *, user_id, entity_id, claim_type, condition):
    return await db.pool.fetchrow(
        "INSERT INTO alert (user_id, entity_id, claim_type, condition) "
        "VALUES ($1, $2, $3::claim_type, $4::jsonb) RETURNING *",
        user_id,
        entity_id,
        claim_type,
        json.dumps(condition),
    )


def _ids(rows) -> list:
    return [r["id"] for r in rows]


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, users CASCADE")
    yield


# --- value_above / value_below --------------------------------------------


class TestValueAbove:
    async def test_fires_when_above(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        claim = await _claim(db, entity, "close", value={"value": 150})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )
        fired = await evaluate(db.pool, alert, audience=user)
        assert _ids(fired) == [claim]

    async def test_does_not_fire_when_below(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        await _claim(db, entity, "close", value={"value": 50})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )
        assert await evaluate(db.pool, alert, audience=user) == []

    async def test_threshold_is_strict(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        await _claim(db, entity, "close", value={"value": 100})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )
        # Equal is not above; crossing is strict.
        assert await evaluate(db.pool, alert, audience=user) == []


class TestValueBelow:
    async def test_fires_when_below(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        claim = await _claim(db, entity, "close", value={"value": 50})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_below", "threshold": 100},
        )
        fired = await evaluate(db.pool, alert, audience=user)
        assert _ids(fired) == [claim]

    async def test_does_not_fire_when_above(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        await _claim(db, entity, "close", value={"value": 150})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_below", "threshold": 100},
        )
        assert await evaluate(db.pool, alert, audience=user) == []


# --- staleness_exceeds -----------------------------------------------------


class TestStalenessExceeds:
    async def test_fires_when_coverage_is_too_old(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        now = datetime.now(UTC)
        claim = await _claim(
            db,
            entity,
            "close",
            value={"value": 100},
            knowledge_date=now - timedelta(days=30),
        )
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "staleness_exceeds", "seconds": 10 * 86400},
        )
        fired = await evaluate(db.pool, alert, audience=user)
        assert _ids(fired) == [claim]

    async def test_does_not_fire_when_fresh(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        now = datetime.now(UTC)
        await _claim(
            db,
            entity,
            "close",
            value={"value": 100},
            knowledge_date=now - timedelta(hours=1),
        )
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "staleness_exceeds", "seconds": 10 * 86400},
        )
        assert await evaluate(db.pool, alert, audience=user) == []

    async def test_fires_when_nothing_new_has_arrived(self, db):
        """The headline behaviour: a staleness alert fires from the passage of
        time alone. No new claim appears between creation and evaluation -- the
        firing is driven by the absence of freshness, which no value threshold
        can express. A value_above twin on the same coverage stays silent."""
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        now = datetime.now(UTC)
        stale = await _claim(
            db,
            entity,
            "close",
            value={"value": 100},
            knowledge_date=now - timedelta(days=30),
        )

        stale_alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "staleness_exceeds", "seconds": 10 * 86400},
        )
        # A threshold twin: the value never crosses 100, so it can never fire
        # from the same state that the staleness alert fires from.
        price_alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 200},
        )

        # Nothing is inserted here. Time alone has made the coverage stale.
        fired = await evaluate(db.pool, stale_alert, audience=user)
        assert _ids(fired) == [stale]
        assert await evaluate(db.pool, price_alert, audience=user) == []

    async def test_does_not_fire_when_there_is_no_coverage(self, db):
        """No visible claim means a *missing* gap, not a stale one. Staleness
        needs a reference claim to be older than; with none, it stays quiet and
        leaves 'missing' to the gap engine."""
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "staleness_exceeds", "seconds": 1},
        )
        assert await evaluate(db.pool, alert, audience=user) == []


# --- contradiction ---------------------------------------------------------


class TestContradiction:
    async def test_fires_when_two_sources_disagree(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        now = datetime.now(UTC)
        event = now - timedelta(days=1)
        a = await _claim(
            db,
            entity,
            "close",
            value={"value": 100},
            source="src_a",
            event_date=event,
            knowledge_date=now,
        )
        b = await _claim(
            db,
            entity,
            "close",
            value={"value": 120},
            source="src_b",
            event_date=event,
            knowledge_date=now,
        )
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "contradiction"},
        )
        fired = await evaluate(db.pool, alert, audience=user)
        assert set(_ids(fired)) == {a, b}

    async def test_does_not_fire_when_sources_agree(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        now = datetime.now(UTC)
        event = now - timedelta(days=1)
        await _claim(
            db,
            entity,
            "close",
            value={"value": 100},
            source="src_a",
            event_date=event,
            knowledge_date=now,
        )
        await _claim(
            db,
            entity,
            "close",
            value={"value": 100},
            source="src_b",
            event_date=event,
            knowledge_date=now,
        )
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "contradiction"},
        )
        assert await evaluate(db.pool, alert, audience=user) == []

    async def test_does_not_fire_with_a_single_source(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        await _claim(db, entity, "close", value={"value": 100}, source="src_a")
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "contradiction"},
        )
        assert await evaluate(db.pool, alert, audience=user) == []


# --- the dedup: no double fire --------------------------------------------


class TestNoDoubleFire:
    async def test_a_claim_fires_once_across_repeated_evaluations(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        claim = await _claim(db, entity, "close", value={"value": 150})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )

        first = await evaluate(db.pool, alert, audience=user)
        second = await evaluate(db.pool, alert, audience=user)

        assert _ids(first) == [claim]
        assert second == [], "a condition still true must not re-fire"

        # And exactly one firing row exists, enforced by the primary key.
        n = await db.pool.fetchval(
            "SELECT count(*) FROM alert_firing WHERE alert_id = $1", alert["id"]
        )
        assert n == 1

    async def test_a_later_satisfying_claim_fires_only_on_a_re_cross(self, db):
        # The fires-on-every-claim defect, fixed 2026-08-21: a daily close
        # above the level used to mean a firing per day, forever. A level
        # condition fires on the CROSSING -- and re-arms only after the value
        # has gone back to the other side.
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        first = await _claim(db, entity, "close", value={"value": 150})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )
        assert _ids(await evaluate(db.pool, alert, audience=user)) == [first]

        # Still above: not a crossing, no firing.
        await _claim(db, entity, "close", value={"value": 160})
        assert await evaluate(db.pool, alert, audience=user) == []

        # Below again: the alert re-arms silently.
        await _claim(db, entity, "close", value={"value": 90})
        assert await evaluate(db.pool, alert, audience=user) == []

        # The next crossing fires -- and only it.
        re_cross = await _claim(db, entity, "close", value={"value": 180})
        assert _ids(await evaluate(db.pool, alert, audience=user)) == [re_cross]

        n = await db.pool.fetchval(
            "SELECT count(*) FROM alert_firing WHERE alert_id = $1", alert["id"]
        )
        assert n == 2


# --- the leak test ---------------------------------------------------------


class TestRedistributionLeak:
    async def test_an_alert_never_fires_on_another_users_private_claim(self, db):
        """The leak this layer exists to prevent. A sets a private (byo_only)
        claim; B sets an alert on the same entity whose condition the claim
        would satisfy. B's evaluation reads only through visible_claims, so the
        private claim is invisible to B and the alert stays silent -- it does
        not become a channel for serving A's licensed data to B."""
        owner = await _user(db, "owner@example.com")
        other = await _user(db, "other@example.com")
        entity = await _entity(db)
        private = await _claim(
            db,
            entity,
            "close",
            value={"value": 150},
            source="polygon",
            owner=owner,
        )

        others_alert = await _alert(
            db,
            user_id=other,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )
        assert await evaluate(db.pool, others_alert, audience=other) == []

        # Control: the owner sees their own private claim and the alert fires.
        owners_alert = await _alert(
            db,
            user_id=owner,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )
        assert _ids(await evaluate(db.pool, owners_alert, audience=owner)) == [private]

        # No firing was recorded against the other user's alert.
        n = await db.pool.fetchval(
            "SELECT count(*) FROM alert_firing WHERE alert_id = $1",
            others_alert["id"],
        )
        assert n == 0


# --- unknown condition refused at the gate ---------------------------------


class TestClosedConditionSet:
    def test_the_set_is_exactly_the_six_kinds(self):
        assert KNOWN_KINDS == frozenset(
            {
                "value_above", "value_below",
                "pct_change_above", "pct_change_below",
                "staleness_exceeds", "contradiction",
            }
        )

    @pytest.mark.parametrize(
        "bad",
        [
            {"kind": "price_crosses"},
            {"kind": "value_above"},
            {"kind": "value_above", "threshold": "high"},
            {"kind": "value_above", "threshold": True},
            {"kind": "staleness_exceeds", "seconds": -1},
            {"kind": "staleness_exceeds"},
            {"kind": "expression", "body": "value * 2 > 100"},
            "not even an object",
        ],
    )
    def test_unknown_or_malformed_conditions_are_rejected(self, bad):
        with pytest.raises(InvalidCondition):
            validate_condition(bad)

    def test_normalisation_applies_the_value_field_default(self):
        assert validate_condition({"kind": "value_above", "threshold": 5}) == {
            "kind": "value_above",
            "threshold": 5.0,
            "field": "value",
        }


# --- firing raises demand (the second effect of firing) --------------------


class TestFiringRaisesDemand:
    async def test_the_first_firing_raises_demand_for_the_coverage(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        await _claim(db, entity, "close", value={"value": 150})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )

        assert await evaluate(db.pool, alert, audience=user)
        row = await db.pool.fetchrow(
            "SELECT requested_by, claim_type::text AS claim_type, active "
            "FROM demand WHERE entity_id = $1 AND requested_by = $2",
            entity,
            user,
        )
        assert row is not None
        assert row["claim_type"] == "price_snapshot"
        assert row["active"] is True

    async def test_demand_is_raised_once_not_per_firing(self, db):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        await _claim(db, entity, "close", value={"value": 150})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )
        await evaluate(db.pool, alert, audience=user)
        # A second, different satisfying claim fires again but must not raise a
        # second demand row: the coverage is already demanded.
        await _claim(db, entity, "close", value={"value": 160})
        await evaluate(db.pool, alert, audience=user)

        n = await db.pool.fetchval(
            "SELECT count(*) FROM demand WHERE entity_id = $1 AND requested_by = $2",
            entity,
            user,
        )
        assert n == 1


# --- HTTP layer ------------------------------------------------------------


class _Lifespan:
    def __init__(self, app):
        self._app = app
        self._receive = asyncio.Queue()
        self._send = asyncio.Queue()
        self._task = None

    async def __aenter__(self):
        self._task = asyncio.create_task(
            self._app({"type": "lifespan"}, self._receive.get, self._send.put)
        )
        await self._receive.put({"type": "lifespan.startup"})
        message = await self._send.get()
        assert message["type"] == "lifespan.startup.complete", message
        return self._app

    async def __aexit__(self, *exc):
        await self._receive.put({"type": "lifespan.shutdown"})
        await self._send.get()
        await self._task


def _make_app(database_url):
    app = create_app(database_url)
    app.include_router(build_router(app))
    return app


def _token(user_id) -> dict:
    os.environ.setdefault("OMNI_JWT_SECRET", "t" * 48)
    token = create_token({"sub": str(user_id)}, os.environ["OMNI_JWT_SECRET"])
    return {"Authorization": f"Bearer {token}"}


class TestHttp:
    async def test_anonymous_caller_is_refused(self, db, database_url):
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            post = await client.post(
                "/alerts",
                json={
                    "entity_id": str(uuid4()),
                    "claim_type": "price_snapshot",
                    "condition": {"kind": "value_above", "threshold": 1},
                },
            )
            get = await client.get("/alerts")
        assert post.status_code == 401
        assert get.status_code == 401

    async def test_an_unknown_condition_is_refused_at_creation(self, db, database_url):
        user = await _user(db, "a@example.com")
        entity = await _entity(db)
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post(
                "/alerts",
                json={
                    "entity_id": str(entity),
                    "claim_type": "price_snapshot",
                    "condition": {"kind": "price_crosses", "threshold": 1},
                },
                headers=_token(user),
            )
        assert r.status_code == 400, r.text
        assert "price_crosses" in r.text

    async def test_crud_and_ownership(self, db, database_url):
        owner = await _user(db, "owner@example.com")
        other = await _user(db, "other@example.com")
        entity = await _entity(db)
        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            created = await client.post(
                "/alerts",
                json={
                    "entity_id": str(entity),
                    "claim_type": "price_snapshot",
                    "condition": {"kind": "value_above", "threshold": 100},
                },
                headers=_token(owner),
            )
            assert created.status_code == 201, created.text
            alert_id = created.json()["id"]
            assert created.json()["condition"] == {
                "kind": "value_above",
                "threshold": 100.0,
                "field": "value",
            }

            listed = await client.get("/alerts", headers=_token(owner))
            assert listed.status_code == 200
            assert [a["id"] for a in listed.json()["alerts"]] == [alert_id]

            # The owner can deactivate and re-read it.
            patched = await client.patch(
                f"/alerts/{alert_id}",
                json={"active": False},
                headers=_token(owner),
            )
            assert patched.status_code == 200
            assert patched.json()["active"] is False

            # Another user cannot see, touch, or delete it -- 404, not 403.
            assert (
                await client.get(f"/alerts/{alert_id}", headers=_token(other))
            ).status_code == 404
            assert (
                await client.delete(f"/alerts/{alert_id}", headers=_token(other))
            ).status_code == 404

            deleted = await client.delete(f"/alerts/{alert_id}", headers=_token(owner))
            assert deleted.status_code == 204

    async def test_firings_endpoint_lists_recorded_firings_owner_only(self, db, database_url):
        from omni.alerts.rules import evaluate

        owner = await _user(db, "owner@example.com")
        other = await _user(db, "other@example.com")
        entity = await _entity(db)
        claim = await _claim(db, entity, "close", value={"value": 150})
        alert = await _alert(
            db,
            user_id=owner,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )
        await evaluate(db.pool, alert, audience=owner)

        app = _make_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            rows = await client.get(f"/alerts/{alert['id']}/firings", headers=_token(owner))
            assert rows.status_code == 200, rows.text
            firings = rows.json()["firings"]
            assert [f["claim_id"] for f in firings] == [str(claim)]

            # Another user reads no firings on an alert that is not theirs: the
            # ownership join returns nothing, and the endpoint does not confirm
            # the alert's existence.
            leak = await client.get(f"/alerts/{alert['id']}/firings", headers=_token(other))
            assert leak.status_code == 200
            assert leak.json()["firings"] == []


class TestSchedulerEvaluation:
    """The scheduler-level loader that turns the inert alerts feature into a
    live one. Without it, /alerts/{id}/firings was always empty -- the evaluate
    function existed but no loop called it."""

    async def test_evaluate_alerts_once_fires_every_active_alert_against_coverage(self, db):

        user = await _user(db, "op@example.com")
        entity = await _entity(db)
        claim = await _claim(db, entity, "close", value={"value": 150})
        await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )

        fired = await evaluate_alerts_once(db.pool)
        assert fired == 1
        recorded = await db.pool.fetchval(
            "SELECT count(*) FROM alert_firing WHERE claim_id = $1", claim
        )
        assert recorded == 1

    async def test_a_failing_alert_does_not_stop_the_others(self, db):
        # One alert with a malformed condition (the loader does not validate at
        # load time) must not suppress a sibling that would fire.
        user = await _user(db, "op@example.com")
        entity = await _entity(db)
        good_claim = await _claim(db, entity, "close", value={"value": 150})
        await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )
        await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "totally_unknown_kind"},
        )

        fired = await evaluate_alerts_once(db.pool)
        # The good alert fired; the malformed one was skipped, not fatal.
        assert fired == 1
        recorded = await db.pool.fetchval(
            "SELECT count(*) FROM alert_firing WHERE claim_id = $1", good_claim
        )
        assert recorded == 1


# --- percent-change conditions ---------------------------------------------


class TestPctChangeConditions:
    async def test_fires_on_a_window_move_and_not_while_it_persists(self, db):
        user = await _user(db, "pct@example.com")
        entity = await _entity(db, symbol="AAPL")
        base_day = datetime(2026, 7, 1, tzinfo=UTC)

        # 100 at day 0, 112 at day 30: +12% over the 30d window -> crossing.
        await _claim(db, entity, "close", value={"value": 100},
                     knowledge_date=base_day)
        up = await _claim(db, entity, "close", value={"value": 112},
                          knowledge_date=base_day + timedelta(days=30))
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "pct_change_above", "pct": 10, "window_days": 30},
        )
        assert _ids(await evaluate(db.pool, alert, audience=user)) == [up]

        # Day 31, still ~12% over its own window: not a new crossing.
        await _claim(db, entity, "close", value={"value": 113},
                     knowledge_date=base_day + timedelta(days=31))
        assert await evaluate(db.pool, alert, audience=user) == []

    async def test_a_shorter_window_than_the_move_does_not_fire(self, db):
        # A +12% move accumulated over 30 days, with daily claims: over the
        # final 7-day window the base is the day-23 close (~104), so the 7-day
        # move is under the bar even though the 30-day move clears it.
        user = await _user(db, "pct2@example.com")
        entity = await _entity(db, symbol="MSFT")
        base_day = datetime(2026, 7, 1, tzinfo=UTC)
        for day in range(31):
            # 100 -> 112 linearly over 30 days: ~0.4/day.
            await _claim(db, entity, "close",
                         value={"value": round(100 + 12 * day / 30, 4)},
                         knowledge_date=base_day + timedelta(days=day))
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "pct_change_above", "pct": 10, "window_days": 7},
        )
        assert await evaluate(db.pool, alert, audience=user) == []

        # The same history over the 30-day window does cross.
        alert30 = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "pct_change_above", "pct": 10, "window_days": 30},
        )
        assert await evaluate(db.pool, alert30, audience=user) != []

    def test_validation_rejects_nonpositive_pct_and_short_windows(self):
        with pytest.raises(InvalidCondition):
            validate_condition({"kind": "pct_change_above", "pct": 0, "window_days": 30})
        with pytest.raises(InvalidCondition):
            validate_condition({"kind": "pct_change_below", "pct": -5, "window_days": 30})
        with pytest.raises(InvalidCondition):
            validate_condition({"kind": "pct_change_above", "pct": 10, "window_days": 0})


# --- one-shot alerts --------------------------------------------------------


class TestOneShot:
    async def test_deactivates_itself_on_firing(self, db):
        user = await _user(db, "once@example.com")
        entity = await _entity(db, symbol="TSLA")
        await _claim(db, entity, "close", value={"value": 150})
        alert = await db.pool.fetchrow(
            "INSERT INTO alert (user_id, entity_id, claim_type, condition, one_shot) "
            "VALUES ($1, $2, 'price_snapshot', $3::jsonb, true) RETURNING *",
            user, entity,
            json.dumps({"kind": "value_above", "threshold": 100}),
        )
        assert await evaluate(db.pool, alert, audience=user) != []
        active = await db.pool.fetchval(
            "SELECT active FROM alert WHERE id = $1", alert["id"]
        )
        assert active is False, "a one-shot must disarm in the same transaction"

    async def test_a_one_shot_that_never_fires_stays_armed(self, db):
        user = await _user(db, "once2@example.com")
        entity = await _entity(db, symbol="TSLA")
        await _claim(db, entity, "close", value={"value": 50})
        alert = await db.pool.fetchrow(
            "INSERT INTO alert (user_id, entity_id, claim_type, condition, one_shot) "
            "VALUES ($1, $2, 'price_snapshot', $3::jsonb, true) RETURNING *",
            user, entity,
            json.dumps({"kind": "value_above", "threshold": 100}),
        )
        assert await evaluate(db.pool, alert, audience=user) == []
        assert await db.pool.fetchval(
            "SELECT active FROM alert WHERE id = $1", alert["id"]
        ) is True


# --- the firing inbox and acknowledgement -----------------------------------


class TestFiringInbox:
    async def test_inbox_lists_unread_first_and_ack_clears_it(self, db, database_url):
        user = await _user(db, "inbox@example.com")
        other = await _user(db, "inbox-other@example.com")
        entity = await _entity(db, symbol="NVDA")
        c1 = await _claim(db, entity, "close", value={"value": 150})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )
        assert await evaluate(db.pool, alert, audience=user) != []

        app = create_app(database_url)
        app.include_router(build_router(app))
        async with _Lifespan(app), TestClient(app) as client:
            headers = _token(user)

            listed = await client.get("/alert-firings", headers=headers)
            assert listed.status_code == 200
            body = listed.json()
            assert body["unread"] == 1
            assert body["firings"][0]["entity_symbol"] == "NVDA"
            assert body["firings"][0]["condition"]["kind"] == "value_above"

            acked = await client.post(
                f"/alert-firings/{alert['id']}/{c1}/ack", headers=headers,
            )
            assert acked.status_code == 201

            after = await client.get("/alert-firings", headers=headers)
            assert after.json()["unread"] == 0

            # Another user's ack is a 404, not a 403 -- same enumeration rule.
            denied = await client.post(
                f"/alert-firings/{alert['id']}/{c1}/ack",
                headers=_token(other),
            )
            assert denied.status_code == 404

    async def test_ack_all_clears_the_badge(self, db, database_url):
        user = await _user(db, "ackall@example.com")
        entity = await _entity(db, symbol="AMD")
        await _claim(db, entity, "close", value={"value": 150})
        alert = await _alert(
            db,
            user_id=user,
            entity_id=entity,
            claim_type="price_snapshot",
            condition={"kind": "value_above", "threshold": 100},
        )
        assert await evaluate(db.pool, alert, audience=user) != []

        app = create_app(database_url)
        app.include_router(build_router(app))
        async with _Lifespan(app), TestClient(app) as client:
            headers = _token(user)
            result = await client.post(
                f"/alerts/{alert['id']}/ack-all", headers=headers,
            )
            assert result.status_code == 201
            assert result.json()["updated"] == 1


# --- notification configuration ----------------------------------------------


class TestNotifySettings:
    async def test_report_and_save_without_leaking_the_url(self, db, database_url):
        from omni.api.settings import build_router as build_settings_router

        user = await _user(db, "notify@example.com")
        app = create_app(database_url)
        app.include_router(build_settings_router(app))
        async with _Lifespan(app), TestClient(app) as client:
            headers = _token(user)

            initial = await client.get("/settings/notifications", headers=headers)
            assert initial.json() == {
                "webhook_configured": False, "email": None, "smtp_available": False,
            }

            saved = await client.put(
                "/settings/notifications",
                json={"webhook_url": "https://hooks.example.com/x/SECRET",
                      "email": "me@example.com"},
                headers=headers,
            )
            assert saved.status_code == 200
            body = saved.json()
            assert body["webhook_configured"] is True
            assert body["email"] == "me@example.com"
            # The URL never comes back -- it can embed a token.
            assert "SECRET" not in json.dumps(body)

            reread = await client.get("/settings/notifications", headers=headers)
            assert reread.json()["webhook_configured"] is True
