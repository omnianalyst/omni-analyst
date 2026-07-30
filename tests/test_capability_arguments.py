"""D4 -- ArgumentSpec + materialize.

The six behaviours the work order names, each asserted on real numbers rather
than shape:

1. a ``log_return`` series equals the hand-computed formula (not simple_return,
   not level).
2. 12 of 20 required observations abstains and names the shortfall; 20 of 20
   computes. The pair is the discrimination proof for the ``min_obs`` floor.
3. a ``byo_only`` claim private to audience A is invisible to audience B --
   ``visible_claims`` scoping, not a comment.
4. transform-then-window: ``log_return`` + ``window=10`` over 20 levels yields
   exactly 10 returns, not 9 (window-before-transform) and not 19 (no window).
5. a null observation (FRED's ``"."``) in the middle is skipped without shifting
   the alignment of the rest.
6. two related-entity series align on the intersection and abstain when the
   intersection is below ``min_obs`` -- never a union with holes.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.capability.arguments import (
    Abstention,
    AlignedSeries,
    ArgumentSpec,
    Materialized,
    materialize,
)

BASE = datetime(2024, 1, 1, tzinfo=UTC)


_INSERT_CLAIM = """
INSERT INTO claim (entity_id, claim_type, key, value, source,
                   event_date, knowledge_date, confidence,
                   redistributable, audience_user_id, derivation)
VALUES ($1,$2::claim_type,$3,$4::jsonb,$5,$6,$7,$8,
        $9::redistribution,$10,'ingested')
RETURNING id
"""

_INSERT_EDGE_ROW = """
INSERT INTO entity_edge (from_entity, to_entity, relation, source)
VALUES ($1, $2, $3, $4)
"""


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company', $1, $1) "
        "RETURNING id",
        symbol,
    )


async def _seed_levels(
    db,
    entity_id,
    levels,
    *,
    claim_type="price_snapshot",
    key="close",
    source="test",
    redistributable="allowed",
    audience_user_id=None,
    start=BASE,
    knowledge_offset=1,
):
    """Insert one claim per level on consecutive days; nulls carry FRED's '.'."""
    ids = []
    for i, level in enumerate(levels):
        event_date = start + timedelta(days=i)
        knowledge_date = event_date + timedelta(days=knowledge_offset)
        value = json.dumps({"value": level}) if level is not None else json.dumps(
            {"value": "."}
        )
        cid = await db.pool.fetchval(
            _INSERT_CLAIM, entity_id, claim_type, key, value, source,
            event_date, knowledge_date, 1.0, redistributable, audience_user_id,
        )
        ids.append(cid)
    return ids


# --------------------------------------------------------------- 1. log_return


class TestLogReturnSeries:
    async def test_matches_hand_computed_formula(self, db):
        entity_id = await _entity(db)
        levels = [100.0, 110.0, 99.0, 108.9, 97.99]
        await _seed_levels(db, entity_id, levels)

        spec = ArgumentSpec(
            name="returns",
            claim_type="price_snapshot",
            shape="series",
            transform="log_return",
        )
        result = await materialize(spec, db.pool, entity_id=entity_id, audience=None)

        assert isinstance(result, Materialized)
        expected = [math.log(levels[i] / levels[i - 1]) for i in range(1, len(levels))]
        # Exact: the impl uses the identical float operation, so a correct
        # transform is bitwise equal. A simple_return or level fake diverges.
        assert result.value == expected
        # Spot-check it is a log return and not something else.
        assert result.value[0] == pytest.approx(math.log(1.1))
        assert result.value != [(levels[i] - levels[i - 1]) / levels[i - 1]
                                for i in range(1, len(levels))]


# ------------------------------------------- 2. the min_obs discrimination pair


class TestMinObsFloor:
    async def test_twelve_of_twenty_abstains_naming_the_shortfall(self, db):
        entity_id = await _entity(db)
        await _seed_levels(db, entity_id, [float(v) for v in range(12)])

        spec = ArgumentSpec(
            name="returns",
            claim_type="price_snapshot",
            shape="series",
            transform="level",
            min_obs=20,
        )
        result = await materialize(spec, db.pool, entity_id=entity_id, audience=None)

        assert isinstance(result, Abstention)
        assert result.argument == "returns"
        assert "12 of 20" in result.reason
        assert "short 8" in result.reason

    async def test_twenty_of_twenty_computes(self, db):
        entity_id = await _entity(db)
        await _seed_levels(db, entity_id, [float(v) for v in range(20)])

        spec = ArgumentSpec(
            name="returns",
            claim_type="price_snapshot",
            shape="series",
            transform="level",
            min_obs=20,
        )
        result = await materialize(spec, db.pool, entity_id=entity_id, audience=None)

        assert isinstance(result, Materialized)
        assert len(result.value) == 20


# ----------------------------------------------------------- 3. audience scope


class TestAudienceScoping:
    async def test_a_private_claim_is_invisible_to_another_audience(self, db):
        entity_id = await _entity(db)
        owner, other = uuid4(), uuid4()
        levels = [100.0, 101.0, 102.0]
        # price_snapshot fetched under byo_only terms -- owner's eyes only.
        await _seed_levels(
            db, entity_id, levels, redistributable="byo_only",
            audience_user_id=owner, source="polygon",
        )

        spec = ArgumentSpec(
            name="returns", claim_type="price_snapshot", shape="series",
        )

        # The owner sees their private series.
        owner_view = await materialize(
            spec, db.pool, entity_id=entity_id, audience=owner
        )
        assert isinstance(owner_view, Materialized)
        assert owner_view.value == levels

        # A different audience sees nothing -- the leak this path exists to
        # prevent, enforced by visible_claims, not by a comment.
        other_view = await materialize(
            spec, db.pool, entity_id=entity_id, audience=other
        )
        assert isinstance(other_view, Abstention)


# ----------------------------------------------------- 4. transform-then-window


class TestTransformThenWindow:
    async def test_window_takes_trailing_n_after_transform(self, db):
        entity_id = await _entity(db)
        levels = [100.0 + i for i in range(20)]  # 20 distinct levels
        await _seed_levels(db, entity_id, levels)

        spec = ArgumentSpec(
            name="returns",
            claim_type="price_snapshot",
            shape="series",
            transform="log_return",
            window=10,
        )
        result = await materialize(spec, db.pool, entity_id=entity_id, audience=None)

        assert isinstance(result, Materialized)
        # 20 levels -> 19 log returns (transform drops the first); window=10
        # takes the trailing 10 AFTER the transform. Window-before-transform
        # would yield 19 (window the 20 levels, then transform); a wrong
        # off-by-one would yield 9. Exactly 10 pins the ordering.
        assert len(result.value) == 10
        expected_trailing = [
            math.log(levels[i] / levels[i - 1]) for i in range(10, 20)
        ]
        assert result.value == expected_trailing


# ----------------------------------------------- 5. null observation is skipped


class TestNullObservationSkipped:
    async def test_a_null_in_the_middle_does_not_shift_the_rest(self, db):
        entity_id = await _entity(db)
        # index:     d0  d1  d2    d3  d4
        levels = [10.0, 20.0, None, 40.0, 50.0]
        await _seed_levels(db, entity_id, levels)

        spec = ArgumentSpec(
            name="levels", claim_type="price_snapshot", shape="series",
            transform="level", min_obs=1,
        )
        result = await materialize(spec, db.pool, entity_id=entity_id, audience=None)

        assert isinstance(result, Materialized)
        # The null period (FRED's ".") is coverage of a period, not an input:
        # dropped at extraction. The values either side keep their alignment --
        # 40 (d3) and 50 (d4) are still last, not shifted into the gap. A
        # fill-forward or a count-preserving fake produces length 5 or wrong
        # tail values; this is length 4 with the original d3/d4 tail.
        assert result.value == [10.0, 20.0, 40.0, 50.0]
        assert result.value[-2:] == [40.0, 50.0]


# --------------------------------------------- 6. related-entity intersection


class TestRelatedEntityAlignment:
    async def test_aligns_on_intersection_and_abstains_when_too_short(self, db):
        x = await _entity(db, "X")
        y = await _entity(db, "Y")
        z = await _entity(db, "Z")
        await db.pool.execute(_INSERT_EDGE_ROW, x, y, "peer", "test")
        await db.pool.execute(_INSERT_EDGE_ROW, x, z, "peer", "test")

        # Y covers d1..d5; Z covers d3..d7. Intersection is d3,d4,d5 (3 dates).
        await _seed_levels(
            db, y, [10.0, 11.0, 12.0, 13.0, 14.0], start=BASE + timedelta(days=1),
        )
        await _seed_levels(
            db, z, [20.0, 21.0, 22.0, 23.0, 24.0], start=BASE + timedelta(days=3),
        )

        computes = ArgumentSpec(
            name="peers",
            claim_type="price_snapshot",
            shape="series",
            transform="level",
            entity_scope="related",
            relation="peer",
            min_obs=3,
        )
        result = await materialize(
            computes, db.pool, entity_id=x, audience=None
        )
        assert isinstance(result, Materialized)
        assert isinstance(result.value, AlignedSeries)
        # Intersection, not union: three dates, the common ones.
        assert len(result.value.index) == 3
        assert result.value.index == tuple(
            BASE + timedelta(days=d) for d in (3, 4, 5)
        )
        assert result.value.by_entity[y] == (12.0, 13.0, 14.0)
        # Z starts at d3 (20,21,22,23,24); on the d3,d4,d5 intersection its
        # values are its first three -- proving each entity is restricted to
        # the common dates, not unioned.
        assert result.value.by_entity[z] == (20.0, 21.0, 22.0)

        # Same coverage, floor above the intersection size -> abstention.
        abstains = ArgumentSpec(
            name="peers",
            claim_type="price_snapshot",
            shape="series",
            transform="level",
            entity_scope="related",
            relation="peer",
            min_obs=4,
        )
        short = await materialize(
            abstains, db.pool, entity_id=x, audience=None
        )
        assert isinstance(short, Abstention)
        assert "3 of 4" in short.reason


# --------------------------------------------- 7. list shape (one value per peer)


class TestListShape:
    async def test_related_list_is_one_latest_value_per_entity(self, db):
        x = await _entity(db, "X")
        y = await _entity(db, "Y")
        z = await _entity(db, "Z")
        await db.pool.execute(_INSERT_EDGE_ROW, x, y, "peer", "test")
        await db.pool.execute(_INSERT_EDGE_ROW, x, z, "peer", "test")

        await _seed_levels(db, y, [10.0, 11.0, 12.0])
        await _seed_levels(db, z, [20.0, 21.0, 22.0])

        spec = ArgumentSpec(
            name="latest",
            claim_type="price_snapshot",
            shape="list",
            entity_scope="related",
            relation="peer",
            min_obs=2,
        )
        result = await materialize(spec, db.pool, entity_id=x, audience=None)

        assert isinstance(result, Materialized)
        # One scalar (the latest) per related entity, unordered collection.
        assert sorted(result.value) == [12.0, 22.0]
        assert len(result.claim_ids) == 2


# ----------------------------------------- 8. provenance audience scoping (D7)


class TestRowsAreAudienceScoped:
    async def test_another_users_private_claim_is_absent_from_rows(self, db):
        """The carried rows are exactly what ``visible_claims`` returned for the
        audience -- a claim private to another user is absent, not just from the
        values but from the provenance set the licence is computed over."""
        entity_id = await _entity(db)
        owner, other = uuid4(), uuid4()
        owner_ids = await _seed_levels(
            db, entity_id, [100.0, 101.0], start=BASE,
            redistributable="byo_only", audience_user_id=owner, source="polygon",
        )
        other_ids = await _seed_levels(
            db, entity_id, [200.0, 201.0], start=BASE + timedelta(days=2),
            redistributable="byo_only", audience_user_id=other, source="polygon",
        )

        spec = ArgumentSpec(
            name="series", claim_type="price_snapshot", shape="series", min_obs=1,
        )

        owner_view = await materialize(
            spec, db.pool, entity_id=entity_id, audience=owner
        )
        assert isinstance(owner_view, Materialized)
        row_ids = {r.id for r in owner_view.rows}
        # Owner's own private claims are carried.
        assert row_ids == set(owner_ids)
        # The other user's private claims never reached the rows -- the
        # audience scoping visible_claims enforces applies to the provenance
        # set, not just the values.
        assert row_ids.isdisjoint(other_ids)
        # Licence fields on the rows match the private claims.
        assert all(r.redistributable == "byo_only" for r in owner_view.rows)
        assert all(r.audience_user_id == owner for r in owner_view.rows)


# --------------------------------- 9. rows match post-transform post-window (D7)


class TestRowsMatchPostTransformPostWindow:
    async def test_rows_are_the_survivors_after_transform_and_window(self, db):
        """Over a spec that both transforms (log_return) and windows, the rows
        must be the post-transform, post-window set -- the same set claim_ids
        was derived from. Carrying the pre-transform or pre-window set would
        make the licence computed over inputs the value does not depend on."""
        entity_id = await _entity(db)
        levels = [100.0 + i for i in range(20)]
        ids = await _seed_levels(db, entity_id, levels)

        spec = ArgumentSpec(
            name="returns",
            claim_type="price_snapshot",
            shape="series",
            transform="log_return",
            window=10,
        )
        result = await materialize(spec, db.pool, entity_id=entity_id, audience=None)

        assert isinstance(result, Materialized)
        # 20 levels -> 19 log returns (transform drops the first) -> trailing 10.
        assert len(result.value) == 10
        assert len(result.rows) == 10
        assert len(result.claim_ids) == 10

        # log_return at position i is keyed to the end-of-period claim ids[i];
        # after transform the ids are ids[1:] (19), window takes ids[10:].
        expected_ids = ids[10:]
        assert list(result.claim_ids) == expected_ids
        assert [r.id for r in result.rows] == expected_ids

        # The dropped claims (position 0 + the first 9 returns' end-of-period
        # claims) are absent from the rows -- carrying them would widen the
        # licence set beyond the inputs the value depends on.
        assert set(ids[:10]).isdisjoint({r.id for r in result.rows})

        # Each row's value is the post-transform return, not the raw level.
        # A fake carrying pre-transform levels fails here.
        expected_returns = [
            math.log(levels[i] / levels[i - 1]) for i in range(10, 20)
        ]
        assert [r.value for r in result.rows] == expected_returns
        assert result.value == expected_returns


# ------------------------------------------- 10. key narrows to one series (D9)


class TestKeySpecNarrowsToOneSeries:
    """Two series sharing a claim_type (the live shape: eight keys under
    ``fundamental_metric``) must be selectable by ``key``. Without it,
    ``materialize`` blends them -- a silent landmine, not an error."""

    async def test_key_selects_only_the_matching_series_values(self, db):
        entity_id = await _entity(db)
        rev = [100.0, 110.0, 120.0, 130.0]
        nil = [10.0, 12.0, 14.0, 16.0]
        # Same event_dates, different keys -- the live collision shape. Both
        # series under one claim_type is exactly what ArgumentSpec could not
        # disambiguate before D9.
        await _seed_levels(
            db, entity_id, rev, claim_type="fundamental_metric", key="Revenues",
        )
        await _seed_levels(
            db, entity_id, nil, claim_type="fundamental_metric",
            key="NetIncomeLoss", knowledge_offset=2,
        )

        spec = ArgumentSpec(
            name="fundamentals",
            claim_type="fundamental_metric",
            key="Revenues",
            shape="series",
            transform="level",
            min_obs=1,
        )
        result = await materialize(
            spec, db.pool, entity_id=entity_id, audience=None
        )

        assert isinstance(result, Materialized)
        # Only the Revenues observations -- actual values, not just a count.
        assert result.value == rev

    async def test_no_key_blends_every_series_sharing_the_type(self, db):
        entity_id = await _entity(db)
        rev = [100.0, 110.0, 120.0, 130.0]
        nil = [10.0, 12.0, 14.0, 16.0]
        await _seed_levels(
            db, entity_id, rev, claim_type="fundamental_metric", key="Revenues",
        )
        await _seed_levels(
            db, entity_id, nil, claim_type="fundamental_metric",
            key="NetIncomeLoss", knowledge_offset=2,
        )

        # No key: today's behaviour, unchanged for any spec that does not set
        # it. The two series share every event_date; the latest-knowable dedup
        # collapses them, and NetIncomeLoss (the later-knowable series) silently
        # wins every date while Revenues is dropped entirely -- the landmine.
        spec = ArgumentSpec(
            name="fundamentals",
            claim_type="fundamental_metric",
            shape="series",
            transform="level",
            min_obs=1,
        )
        result = await materialize(
            spec, db.pool, entity_id=entity_id, audience=None
        )

        assert isinstance(result, Materialized)
        assert result.value != rev
        assert result.value == nil

    async def test_a_key_matching_nothing_abstains_via_min_obs(self, db):
        entity_id = await _entity(db)
        await _seed_levels(
            db, entity_id, [100.0, 110.0], claim_type="fundamental_metric",
            key="Revenues",
        )

        spec = ArgumentSpec(
            name="fundamentals",
            claim_type="fundamental_metric",
            key="Assets",
            shape="series",
            transform="level",
            min_obs=2,
        )
        result = await materialize(
            spec, db.pool, entity_id=entity_id, audience=None
        )

        # No matching series -> no observations -> the existing min_obs floor,
        # the same honest-refusal path every other shortfall takes, not a new
        # error type.
        assert isinstance(result, Abstention)
        assert result.argument == "fundamentals"
        assert "0 of 2" in result.reason
        assert "short 2" in result.reason

