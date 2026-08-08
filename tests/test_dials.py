"""Editorial dials must read as of a point in time, and must not invent one.

The two failures this file exists to catch are a read that can see a dial set
after `as_of` (which makes every backtest apply today's editorial judgement to
the past) and a read that answers with a value nobody set.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from omni.dials.store import Dial, get_dial, history, set_dial

T0 = datetime(2019, 1, 1, tzinfo=UTC)
T1 = datetime(2019, 6, 1, tzinfo=UTC)
T2 = datetime(2021, 6, 1, tzinfo=UTC)
V1 = "editorial-model-v1"


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _entity(db, symbol="BRA"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('country', $1, $1) "
        "RETURNING id",
        symbol,
    )


async def _set(db, *, value, knowledge_date, name="baseline_risk",
               entity_id=None, methodology_version=V1, audience_user_id=None):
    return await set_dial(
        db.pool,
        name=name,
        entity_id=entity_id,
        value=Decimal(value),
        methodology_version=methodology_version,
        event_date=T0,
        knowledge_date=knowledge_date,
        audience_user_id=audience_user_id,
    )


class TestRoundTrip:
    async def test_set_then_get_returns_the_value(self, db):
        entity_id = await _entity(db)
        await _set(db, value="0.37", knowledge_date=T1, entity_id=entity_id)

        dial = await get_dial(
            db.pool, name="baseline_risk", entity_id=entity_id, as_of=T2
        )
        assert dial is not None
        assert dial.value == Decimal("0.37")
        assert dial.name == "baseline_risk"
        assert dial.entity_id == entity_id
        assert dial.methodology_version == V1
        assert dial.event_date == T0
        assert dial.knowledge_date == T1

    async def test_value_is_decimal_not_float(self, db):
        """0.1 + 0.2 == 0.3 must hold, which it does not through binary64."""
        entity_id = await _entity(db)
        await _set(db, value="0.1", knowledge_date=T1, entity_id=entity_id)

        dial = await get_dial(
            db.pool, name="baseline_risk", entity_id=entity_id, as_of=T2
        )
        assert isinstance(dial.value, Decimal)
        assert dial.value + Decimal("0.2") == Decimal("0.3")


class TestPointInTime:
    async def test_a_read_between_two_versions_returns_the_first(self, db):
        """The headline property. A dial set at T2 must be invisible to a read
        positioned before T2, or every backtest silently uses today's opinion."""
        entity_id = await _entity(db)
        await _set(db, value="0.20", knowledge_date=T1, entity_id=entity_id)
        await _set(db, value="0.65", knowledge_date=T2, entity_id=entity_id)

        between = T1 + (T2 - T1) / 2
        earlier = await get_dial(
            db.pool, name="baseline_risk", entity_id=entity_id, as_of=between
        )
        later = await get_dial(
            db.pool, name="baseline_risk", entity_id=entity_id,
            as_of=T2 + timedelta(days=1),
        )

        assert earlier.value == Decimal("0.20")
        assert earlier.knowledge_date == T1
        assert later.value == Decimal("0.65")
        assert later.knowledge_date == T2

    async def test_a_read_exactly_at_a_knowledge_date_sees_that_version(self, db):
        entity_id = await _entity(db)
        await _set(db, value="0.20", knowledge_date=T1, entity_id=entity_id)
        await _set(db, value="0.65", knowledge_date=T2, entity_id=entity_id)

        dial = await get_dial(
            db.pool, name="baseline_risk", entity_id=entity_id, as_of=T2
        )
        assert dial.value == Decimal("0.65")

    async def test_a_read_before_the_first_version_returns_none(self, db):
        """Not the earliest value. Before it was set, there was no dial."""
        entity_id = await _entity(db)
        await _set(db, value="0.20", knowledge_date=T1, entity_id=entity_id)

        dial = await get_dial(
            db.pool, name="baseline_risk", entity_id=entity_id,
            as_of=T1 - timedelta(seconds=1),
        )
        assert dial is None

    async def test_as_of_has_no_default(self, db):
        """A default of now() is how point-in-time discipline erodes."""
        entity_id = await _entity(db)
        with pytest.raises(TypeError):
            await get_dial(db.pool, name="baseline_risk", entity_id=entity_id)


class TestNoInventedDefault:
    async def test_an_unset_dial_returns_none(self, db):
        entity_id = await _entity(db)
        assert await get_dial(
            db.pool, name="never_set", entity_id=entity_id, as_of=T2
        ) is None

    async def test_an_unset_global_dial_returns_none(self, db):
        """With another global dial already recorded, so the miss is the dial's
        own absence rather than the global scope not existing yet."""
        await _set(db, value="0.10", knowledge_date=T1, entity_id=None)
        assert await get_dial(
            db.pool, name="never_set", entity_id=None, as_of=T2
        ) is None

    async def test_a_dial_set_for_one_entity_is_not_returned_for_another(self, db):
        first = await _entity(db, "BRA")
        second = await _entity(db, "TUR")
        await _set(db, value="0.20", knowledge_date=T1, entity_id=first)

        assert await get_dial(
            db.pool, name="baseline_risk", entity_id=second, as_of=T2
        ) is None


class TestHistory:
    async def test_every_version_is_returned_oldest_first(self, db):
        entity_id = await _entity(db)
        mid = datetime(2020, 1, 1, tzinfo=UTC)
        await _set(db, value="0.20", knowledge_date=T1, entity_id=entity_id)
        await _set(db, value="0.44", knowledge_date=mid, entity_id=entity_id)
        await _set(db, value="0.65", knowledge_date=T2, entity_id=entity_id)

        versions = await history(
            db.pool, name="baseline_risk", entity_id=entity_id
        )
        assert [d.value for d in versions] == [
            Decimal("0.20"), Decimal("0.44"), Decimal("0.65")
        ]
        assert [d.knowledge_date for d in versions] == [T1, mid, T2]

    async def test_history_of_an_unset_dial_is_empty(self, db):
        entity_id = await _entity(db)
        assert await history(
            db.pool, name="never_set", entity_id=entity_id
        ) == []


class TestScoping:
    async def test_a_global_dial_and_a_per_entity_dial_do_not_collide(self, db):
        entity_id = await _entity(db)
        await _set(db, value="0.10", knowledge_date=T1, entity_id=None)
        await _set(db, value="0.90", knowledge_date=T1, entity_id=entity_id)

        globally = await get_dial(
            db.pool, name="baseline_risk", entity_id=None, as_of=T2
        )
        per_entity = await get_dial(
            db.pool, name="baseline_risk", entity_id=entity_id, as_of=T2
        )

        assert globally.value == Decimal("0.10")
        assert globally.entity_id is None
        assert per_entity.value == Decimal("0.90")
        assert per_entity.entity_id == entity_id

    async def test_a_global_dial_is_point_in_time_too(self, db):
        await _set(db, value="0.10", knowledge_date=T1, entity_id=None)
        await _set(db, value="0.80", knowledge_date=T2, entity_id=None)

        dial = await get_dial(
            db.pool, name="baseline_risk", entity_id=None,
            as_of=datetime(2020, 1, 1, tzinfo=UTC),
        )
        assert dial.value == Decimal("0.10")


class TestMethodologyVersion:
    async def test_two_methodologies_of_one_dial_name_coexist(self, db):
        entity_id = await _entity(db)
        await _set(db, value="0.20", knowledge_date=T1, entity_id=entity_id)
        await _set(
            db, value="0.55", knowledge_date=T1, entity_id=entity_id,
            methodology_version="editorial-model-v2",
        )

        versions = await history(
            db.pool, name="baseline_risk", entity_id=entity_id
        )
        assert {(d.methodology_version, d.value) for d in versions} == {
            (V1, Decimal("0.20")),
            ("editorial-model-v2", Decimal("0.55")),
        }


class TestRefusals:
    async def test_nan_is_refused(self, db):
        entity_id = await _entity(db)
        with pytest.raises(ValueError, match="not finite"):
            await _set(db, value="NaN", knowledge_date=T1, entity_id=entity_id)
        assert await history(
            db.pool, name="baseline_risk", entity_id=entity_id
        ) == []

    async def test_infinity_is_refused(self, db):
        entity_id = await _entity(db)
        with pytest.raises(ValueError, match="not finite"):
            await _set(
                db, value="Infinity", knowledge_date=T1, entity_id=entity_id
            )

    async def test_a_float_value_is_refused(self, db):
        entity_id = await _entity(db)
        with pytest.raises(TypeError, match="must be Decimal"):
            await set_dial(
                db.pool,
                name="baseline_risk",
                entity_id=entity_id,
                value=0.37,
                methodology_version=V1,
                event_date=T0,
                knowledge_date=T1,
            )

    async def test_knowledge_date_before_event_date_is_refused(self, db):
        entity_id = await _entity(db)
        with pytest.raises(ValueError, match="precedes event_date"):
            await set_dial(
                db.pool,
                name="baseline_risk",
                entity_id=entity_id,
                value=Decimal("0.37"),
                methodology_version=V1,
                event_date=T2,
                knowledge_date=T1,
            )

    async def test_overwriting_a_recorded_version_in_place_is_refused(self, db):
        entity_id = await _entity(db)
        await _set(db, value="0.20", knowledge_date=T1, entity_id=entity_id)
        with pytest.raises(ValueError, match="new knowledge_date"):
            await _set(db, value="0.99", knowledge_date=T1, entity_id=entity_id)

        dial = await get_dial(
            db.pool, name="baseline_risk", entity_id=entity_id, as_of=T2
        )
        assert dial.value == Decimal("0.20")

    async def test_recording_the_same_value_twice_is_idempotent(self, db):
        entity_id = await _entity(db)
        first = await _set(
            db, value="0.20", knowledge_date=T1, entity_id=entity_id
        )
        second = await _set(
            db, value="0.20", knowledge_date=T1, entity_id=entity_id
        )
        assert first == second
        assert len(
            await history(db.pool, name="baseline_risk", entity_id=entity_id)
        ) == 1


class TestAudience:
    async def test_another_users_dial_is_not_visible(self, db):
        entity_id = await _entity(db)
        owner = uuid4()
        other = uuid4()
        await _set(
            db, value="0.42", knowledge_date=T1, entity_id=entity_id,
            audience_user_id=owner,
        )

        mine = await get_dial(
            db.pool, name="baseline_risk", entity_id=entity_id, as_of=T2,
            audience_user_id=owner,
        )
        theirs = await get_dial(
            db.pool, name="baseline_risk", entity_id=entity_id, as_of=T2,
            audience_user_id=other,
        )
        shared = await get_dial(
            db.pool, name="baseline_risk", entity_id=entity_id, as_of=T2
        )

        assert mine.value == Decimal("0.42")
        assert theirs is None
        assert shared is None

    async def test_a_private_dial_does_not_appear_in_another_users_history(self, db):
        entity_id = await _entity(db)
        owner = uuid4()
        await _set(
            db, value="0.42", knowledge_date=T1, entity_id=entity_id,
            audience_user_id=owner,
        )
        assert await history(
            db.pool, name="baseline_risk", entity_id=entity_id,
            audience_user_id=uuid4(),
        ) == []


class TestDialDataclass:
    async def test_a_corrupt_stored_value_raises_rather_than_reading_back(self, db):
        """Reconstruction re-runs the validation, so a NaN written around this
        module by hand cannot be read back as a confident parameter."""
        entity_id = await _entity(db)
        with pytest.raises(ValueError, match="not finite"):
            Dial(
                name="baseline_risk",
                entity_id=entity_id,
                value=Decimal("NaN"),
                methodology_version=V1,
                event_date=T0,
                knowledge_date=T1,
            )
