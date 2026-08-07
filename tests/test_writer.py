"""The claim writer — the one place licence class and audience are decided.

Adapters cannot express either, so these tests are the whole enforcement
surface for "which tier does this data land in".
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.coverage.visibility import visible_claims
from omni.coverage.writer import (
    MissingCredentialOwner,
    ProhibitedSource,
    resolve_audience,
    write_claims,
)
from omni.ingest.protocol import ClaimDraft

NOW = datetime(2026, 7, 27, tzinfo=UTC)


def _draft(key="GDP", claim_type="macro_series_point", value=1.0):
    return ClaimDraft(
        claim_type=claim_type,
        key=key,
        value={"value": value},
        event_date=NOW - timedelta(days=2),
        knowledge_date=NOW - timedelta(days=1),
        confidence=1.0,
    )


async def _entity(db):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company','AAPL','A') "
        "RETURNING id"
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestResolveAudience:
    def test_an_allowed_source_becomes_shared_coverage(self):
        assert resolve_audience("fred", credential_owner=None) == ("allowed", None)

    def test_an_allowed_source_stays_shared_even_when_a_key_was_used(self):
        """The data is redistributable, so it belongs to the network."""
        assert resolve_audience("fred", credential_owner=uuid4()) == ("allowed", None)

    def test_a_byo_source_is_pinned_to_its_credential_owner(self):
        owner = uuid4()
        assert resolve_audience("polygon", credential_owner=owner) == (
            "byo_only",
            owner,
        )

    def test_a_byo_source_without_an_owner_is_refused(self):
        """Writing it anyway would be the leak; the schema would reject it too."""
        with pytest.raises(MissingCredentialOwner):
            resolve_audience("polygon", credential_owner=None)

    def test_a_prohibited_source_is_never_written(self):
        with pytest.raises(ProhibitedSource):
            resolve_audience("yahoo", credential_owner=uuid4())

    def test_an_unknown_provider_raises_rather_than_defaulting(self):
        with pytest.raises(KeyError):
            resolve_audience("some_new_vendor", credential_owner=None)

    def test_an_operator_licence_promotes_a_byo_source(self):
        assert resolve_audience(
            "polygon", credential_owner=uuid4(), licensed=("polygon",)
        ) == ("allowed", None)

    def test_an_operator_licence_cannot_promote_a_prohibited_source(self):
        with pytest.raises(ProhibitedSource):
            resolve_audience(
                "yahoo", credential_owner=uuid4(), licensed=("yahoo",)
            )


class TestWriteClaims:
    async def test_an_allowed_source_lands_in_shared_coverage(self, db):
        entity_id = await _entity(db)
        written = await write_claims(
            db.pool, [_draft()], entity_id=entity_id,
            source="fred", provider_key="fred",
        )
        assert len(written) == 1
        rows = await visible_claims(db.pool, audience=None)
        assert len(rows) == 1

    async def test_a_byo_source_is_invisible_to_everyone_else(self, db):
        """The end-to-end version of the licence rule."""
        entity_id = await _entity(db)
        owner = uuid4()
        await write_claims(
            db.pool, [_draft(claim_type="price_snapshot", key="AAPL")],
            entity_id=entity_id, source="polygon", provider_key="polygon",
            credential_owner=owner,
        )
        assert await visible_claims(db.pool, audience=owner) != []
        assert await visible_claims(db.pool, audience=uuid4()) == []
        assert await visible_claims(db.pool, audience=None) == []

    async def test_reingesting_the_same_observation_writes_nothing(self, db):
        entity_id = await _entity(db)
        common = {"entity_id": entity_id, "source": "fred", "provider_key": "fred"}
        first = await write_claims(db.pool, [_draft()], **common)
        second = await write_claims(db.pool, [_draft()], **common)
        assert len(first) == 1
        assert second == []
        assert await db.pool.fetchval("SELECT count(*) FROM claim") == 1

    async def test_a_later_vintage_is_a_new_claim(self, db):
        entity_id = await _entity(db)
        common = {"entity_id": entity_id, "source": "fred", "provider_key": "fred"}
        await write_claims(db.pool, [_draft()], **common)

        revised = ClaimDraft(
            claim_type="macro_series_point", key="GDP", value={"value": 2.0},
            event_date=NOW - timedelta(days=2), knowledge_date=NOW,
            confidence=1.0,
        )
        await write_claims(db.pool, [revised], **common)
        assert await db.pool.fetchval("SELECT count(*) FROM claim") == 2

    async def test_a_prohibited_source_writes_nothing_at_all(self, db):
        entity_id = await _entity(db)
        with pytest.raises(ProhibitedSource):
            await write_claims(
                db.pool, [_draft()], entity_id=entity_id,
                source="yahoo", provider_key="yahoo",
            )
        assert await db.pool.fetchval("SELECT count(*) FROM claim") == 0

    async def test_a_batch_is_all_or_nothing(self, db):
        """One bad draft must not leave half a series in the store."""
        entity_id = await _entity(db)
        bad = ClaimDraft(
            claim_type="macro_series_point", key="GDP", value={"value": 1.0},
            event_date=NOW, knowledge_date=NOW, confidence=1.0,
        )
        object.__setattr__(bad, "claim_type", "not_a_real_claim_type")
        with pytest.raises(Exception):
            await write_claims(
                db.pool, [_draft(key="A"), bad, _draft(key="B")],
                entity_id=entity_id, source="fred", provider_key="fred",
            )
        assert await db.pool.fetchval("SELECT count(*) FROM claim") == 0
