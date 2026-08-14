"""Phase C: the sector scanner loop."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.autonomous.sector import (
    macro_alignment,
    scan_sectors,
)
from omni.entities._seed_data import SECTOR_ETFS


async def _seed_etfs(db):
    ids = {}
    for symbol, name, gics in SECTOR_ETFS:
        eid = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name, identifiers) "
            "VALUES ('sector_etf', $1, $2, $3::jsonb) "
            "ON CONFLICT (kind, symbol) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id",
            symbol, name,
            json.dumps({"polygon": symbol, "gics_sector": gics}),
        )
        ids[symbol] = eid
    return ids


async def _seed_prices(
    db,
    entity_id,
    closes,
    *,
    start_days_ago=70,
    base=None,
    knowledge_delay=timedelta(days=1),
    redistributable="allowed",
    audience_user_id=None,
    source="test",
):
    base = base or datetime.now(UTC) - timedelta(days=start_days_ago)
    claim_ids = []
    for i, close in enumerate(closes):
        event = base + timedelta(days=i)
        knowledge = event + knowledge_delay
        claim_id = await db.pool.fetchval(
            """
            INSERT INTO claim (
                entity_id, claim_type, key, value, source,
                event_date, knowledge_date, confidence,
                credential_owner, redistributable, audience_user_id, derivation
            )
            VALUES ($1, 'price_snapshot', $2, $3::jsonb, $4,
                    $5, $6, 1.0,
                    $7, $8::redistribution, $9, 'ingested')
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            entity_id, "TEST",
            json.dumps({"close": close, "open": close, "high": close, "low": close, "volume": 1000}),
            source, event, knowledge,
            f"{source}-credential" if audience_user_id else None,
            redistributable, audience_user_id,
        )
        claim_ids.append(claim_id)
    return claim_ids


async def _seed_regime(
    db,
    entity_id,
    cycle_phase="expansion",
    *,
    event_date=None,
    knowledge_date=None,
):
    event_date = event_date or datetime.now(UTC)
    knowledge_date = knowledge_date or event_date
    return await db.pool.fetchval(
        """
        INSERT INTO claim (
            entity_id, claim_type, key, value, source,
            event_date, knowledge_date, confidence,
            redistributable, audience_user_id, derivation
        )
        VALUES ($1, 'regime_assessment', 'us_macro', $2::jsonb, 'test',
                $3, $4, 1.0, 'allowed', NULL, 'ingested')
        RETURNING id
        """,
        entity_id,
        json.dumps({"cycle_phase": cycle_phase, "risk_regime": "risk_on"}),
        event_date,
        knowledge_date,
    )


def _uptrend_closes(n=65, start=100.0, drift=0.002):
    return [start * (1 + drift) ** i for i in range(n)]


def _downtrend_closes(n=65, start=100.0):
    # classify_trend compares MA(20) vs MA(60) of RETURNS. To register
    # downtrend, recent returns must be below longer-term returns -- an
    # accelerating decline, not a constant rate. First half flat, second
    # half declining steeply.
    closes = []
    midpoint = n // 2
    for i in range(n):
        if i < midpoint:
            closes.append(start)
        else:
            closes.append(start * (1 - 0.008 * (i - midpoint + 1)))
    return closes


def _flat_closes(n=65, level=100.0):
    return [level] * n


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestMacroAlignment:
    def test_expansion_favors_tech(self):
        assert macro_alignment("expansion", "XLK") == "favorable"

    def test_expansion_disfavors_staples(self):
        assert macro_alignment("expansion", "XLP") == "unfavorable"

    def test_contraction_favors_defensive(self):
        assert macro_alignment("contraction", "XLP") == "favorable"
        assert macro_alignment("contraction", "XLU") == "favorable"

    def test_no_regime_is_unknown(self):
        assert macro_alignment(None, "XLK") == "unknown"


class TestScanSectors:
    async def test_abstains_when_no_prices(self, db):
        await _seed_etfs(db)
        # The macro entity must exist for the sector scan to find a regime to
        # align against; its id is not needed here.
        await db.pool.execute(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('macro', 'US_MACRO', 'US Macro')"
        )
        report = await scan_sectors(db.pool)
        assert report.scored == 0
        assert report.abstained == 0

    async def test_non_positive_close_cannot_complete_the_required_window(self, db):
        etf_ids = await _seed_etfs(db)
        closes = _uptrend_closes(n=60) + [0.0]
        await _seed_prices(db, etf_ids["XLK"], closes)

        report = await scan_sectors(db.pool)

        assert report.scored == 0
        assert await db.pool.fetchval(
            "SELECT count(*) FROM claim WHERE claim_type = 'sector_score'"
        ) == 0

    async def test_scores_sectors_with_prices(self, db):
        etf_ids = await _seed_etfs(db)
        macro = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('macro', 'US_MACRO', 'US Macro') RETURNING id"
        )
        await _seed_regime(db, macro, "expansion")
        for symbol in ("XLK", "XLP", "XLE"):
            await _seed_prices(db, etf_ids[symbol], _uptrend_closes())

        report = await scan_sectors(db.pool)
        assert report.scored == 3

        xlk_score = await db.pool.fetchrow(
            "SELECT value FROM claim WHERE entity_id = $1 AND claim_type = 'sector_score'",
            etf_ids["XLK"],
        )
        assert xlk_score is not None
        value = json.loads(xlk_score["value"]) if isinstance(xlk_score["value"], str) else xlk_score["value"]
        assert value["trend"] == "uptrend"
        assert value["macro_alignment"] == "favorable"
        assert 0.0 <= value["rs_percentile"] <= 1.0

    async def test_top_performer_gets_highest_rs(self, db):
        etf_ids = await _seed_etfs(db)
        # The macro entity must exist for the sector scan to find a regime to
        # align against; its id is not needed here.
        await db.pool.execute(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('macro', 'US_MACRO', 'US Macro')"
        )
        await _seed_prices(db, etf_ids["XLK"], _uptrend_closes(drift=0.005))
        await _seed_prices(db, etf_ids["XLP"], _uptrend_closes(drift=0.001))

        report = await scan_sectors(db.pool)
        assert report.scored == 2

        xlk = await db.pool.fetchrow(
            "SELECT value FROM claim WHERE entity_id = $1 AND claim_type = 'sector_score'",
            etf_ids["XLK"],
        )
        xlp = await db.pool.fetchrow(
            "SELECT value FROM claim WHERE entity_id = $1 AND claim_type = 'sector_score'",
            etf_ids["XLP"],
        )
        xlk_v = json.loads(xlk["value"]) if isinstance(xlk["value"], str) else xlk["value"]
        xlp_v = json.loads(xlp["value"]) if isinstance(xlp["value"], str) else xlp["value"]
        assert xlk_v["rs_percentile"] > xlp_v["rs_percentile"]

    async def test_downtrend_detected(self, db):
        etf_ids = await _seed_etfs(db)
        # The macro entity must exist for the sector scan to find a regime to
        # align against; its id is not needed here.
        await db.pool.execute(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('macro', 'US_MACRO', 'US Macro')"
        )
        await _seed_prices(db, etf_ids["XLE"], _downtrend_closes())

        await scan_sectors(db.pool)
        score = await db.pool.fetchrow(
            "SELECT value FROM claim WHERE entity_id = $1 AND claim_type = 'sector_score'",
            etf_ids["XLE"],
        )
        value = json.loads(score["value"]) if isinstance(score["value"], str) else score["value"]
        assert value["trend"] == "downtrend"

    async def test_unknown_alignment_without_regime(self, db):
        etf_ids = await _seed_etfs(db)
        await _seed_prices(db, etf_ids["XLK"], _uptrend_closes())

        report = await scan_sectors(db.pool)
        assert report.scored == 1
        score = await db.pool.fetchrow(
            "SELECT value FROM claim WHERE entity_id = $1 AND claim_type = 'sector_score'",
            etf_ids["XLK"],
        )
        value = json.loads(score["value"]) if isinstance(score["value"], str) else score["value"]
        assert value["macro_alignment"] == "unknown"

    async def test_idempotent_on_unchanged_prices(self, db):
        etf_ids = await _seed_etfs(db)
        # The macro entity must exist for the sector scan to find a regime to
        # align against; its id is not needed here.
        await db.pool.execute(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('macro', 'US_MACRO', 'US Macro')"
        )
        await _seed_prices(db, etf_ids["XLK"], _uptrend_closes())

        report1 = await scan_sectors(db.pool)
        assert report1.scored == 1

        report2 = await scan_sectors(db.pool)
        assert report2.scored == 0
        assert report2.skipped_unchanged == 1

        count = await db.pool.fetchval(
            "SELECT count(*)::int FROM claim WHERE claim_type = 'sector_score'"
        )
        assert count == 1

    async def test_mixed_owner_rows_cannot_complete_the_minimum_window(self, db):
        etf_ids = await _seed_etfs(db)
        owner = uuid4()
        other_owner = uuid4()
        base = datetime(2025, 1, 1, tzinfo=UTC)
        await _seed_prices(
            db,
            etf_ids["XLK"],
            _uptrend_closes(n=60),
            base=base,
            redistributable="byo_only",
            audience_user_id=owner,
        )
        await _seed_prices(
            db,
            etf_ids["XLK"],
            [120.0],
            base=base + timedelta(days=60),
            redistributable="byo_only",
            audience_user_id=other_owner,
        )

        report = await scan_sectors(
            db.pool,
            as_of=base + timedelta(days=70),
            operator_user_id=owner,
        )

        assert report.scored == 0
        assert await db.pool.fetchval(
            "SELECT count(*)::int FROM claim WHERE claim_type = 'sector_score'"
        ) == 0

    async def test_peer_private_prices_taint_the_target_score(self, db):
        etf_ids = await _seed_etfs(db)
        owner = uuid4()
        target_ids = await _seed_prices(
            db, etf_ids["XLK"], _uptrend_closes(drift=0.005)
        )
        peer_ids = await _seed_prices(
            db,
            etf_ids["XLP"],
            _uptrend_closes(drift=0.001),
            redistributable="byo_only",
            audience_user_id=owner,
        )

        report = await scan_sectors(db.pool, operator_user_id=owner)
        score = await db.pool.fetchrow(
            "SELECT id, redistributable::text, audience_user_id FROM claim "
            "WHERE entity_id = $1 AND claim_type = 'sector_score'",
            etf_ids["XLK"],
        )
        input_ids = {
            row["input_id"]
            for row in await db.pool.fetch(
                "SELECT input_id FROM claim_input WHERE claim_id = $1", score["id"]
            )
        }

        assert report.scored == 2
        assert score["redistributable"] == "byo_only"
        assert score["audience_user_id"] == owner
        assert input_ids == set(target_ids + peer_ids)

    async def test_scan_creates_no_cross_owner_provenance_edges(self, db):
        etf_ids = await _seed_etfs(db)
        owner = uuid4()
        other_owner = uuid4()
        own_ids = await _seed_prices(
            db,
            etf_ids["XLK"],
            _uptrend_closes(),
            redistributable="byo_only",
            audience_user_id=owner,
        )
        other_ids = await _seed_prices(
            db,
            etf_ids["XLP"],
            _uptrend_closes(),
            redistributable="byo_only",
            audience_user_id=other_owner,
        )

        report = await scan_sectors(db.pool, operator_user_id=owner)
        score_id = await db.pool.fetchval(
            "SELECT id FROM claim WHERE entity_id = $1 "
            "AND claim_type = 'sector_score'",
            etf_ids["XLK"],
        )
        input_ids = {
            row["input_id"]
            for row in await db.pool.fetch(
                "SELECT input_id FROM claim_input WHERE claim_id = $1", score_id
            )
        }

        assert report.scored == 1
        assert input_ids == set(own_ids)
        assert input_ids.isdisjoint(other_ids)

    async def test_price_knowledge_cutoff_is_enforced(self, db):
        etf_ids = await _seed_etfs(db)
        base = datetime(2025, 1, 1, tzinfo=UTC)
        await _seed_prices(
            db, etf_ids["XLK"], _uptrend_closes(n=60), base=base
        )
        await _seed_prices(
            db,
            etf_ids["XLK"],
            [_uptrend_closes(n=61)[-1]],
            base=base + timedelta(days=60),
            knowledge_delay=timedelta(days=10),
        )

        before_publication = await scan_sectors(
            db.pool, as_of=base + timedelta(days=65)
        )
        after_publication = await scan_sectors(
            db.pool, as_of=base + timedelta(days=71)
        )

        assert before_publication.scored == 0
        assert after_publication.scored == 1

    async def test_declares_complete_target_peer_and_regime_provenance(self, db):
        etf_ids = await _seed_etfs(db)
        macro = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('macro', 'US_MACRO', 'US Macro') RETURNING id"
        )
        target_ids = await _seed_prices(db, etf_ids["XLK"], _uptrend_closes())
        peer_ids = await _seed_prices(db, etf_ids["XLP"], _uptrend_closes())
        regime_id = await _seed_regime(db, macro)

        await scan_sectors(db.pool)
        score_id = await db.pool.fetchval(
            "SELECT id FROM claim WHERE entity_id = $1 "
            "AND claim_type = 'sector_score'",
            etf_ids["XLK"],
        )
        input_ids = {
            row["input_id"]
            for row in await db.pool.fetch(
                "SELECT input_id FROM claim_input WHERE claim_id = $1", score_id
            )
        }

        assert input_ids == set(target_ids + peer_ids + [regime_id])

    async def test_output_dates_dominate_every_declared_input(self, db):
        etf_ids = await _seed_etfs(db)
        macro = await db.pool.fetchval(
            "INSERT INTO entity (kind, symbol, name) "
            "VALUES ('macro', 'US_MACRO', 'US Macro') RETURNING id"
        )
        base = datetime(2025, 1, 1, tzinfo=UTC)
        await _seed_prices(db, etf_ids["XLK"], _uptrend_closes(), base=base)
        await _seed_prices(
            db,
            etf_ids["XLP"],
            _uptrend_closes(),
            base=base + timedelta(days=4),
            knowledge_delay=timedelta(days=2),
        )
        await _seed_regime(
            db,
            macro,
            event_date=base + timedelta(days=1),
            knowledge_date=base + timedelta(days=75),
        )

        await scan_sectors(db.pool, as_of=base + timedelta(days=80))
        dates = await db.pool.fetchrow(
            "SELECT c.event_date, c.knowledge_date, "
            "max(i.event_date) AS max_input_event, "
            "max(i.knowledge_date) AS max_input_knowledge "
            "FROM claim c JOIN claim_input ci ON ci.claim_id = c.id "
            "JOIN claim i ON i.id = ci.input_id "
            "WHERE c.entity_id = $1 AND c.claim_type = 'sector_score' "
            "GROUP BY c.id",
            etf_ids["XLK"],
        )

        assert dates["event_date"] == dates["max_input_event"]
        assert dates["knowledge_date"] == dates["max_input_knowledge"]

    async def test_idempotency_is_partitioned_by_audience(self, db):
        etf_ids = await _seed_etfs(db)
        owner = uuid4()
        other_owner = uuid4()
        base = datetime(2025, 1, 1, tzinfo=UTC)
        for audience in (owner, other_owner):
            await _seed_prices(
                db,
                etf_ids["XLK"],
                _uptrend_closes(),
                base=base,
                redistributable="byo_only",
                audience_user_id=audience,
            )

        first_owner = await scan_sectors(
            db.pool,
            as_of=base + timedelta(days=70),
            operator_user_id=owner,
        )
        first_other = await scan_sectors(
            db.pool,
            as_of=base + timedelta(days=70),
            operator_user_id=other_owner,
        )
        repeat_owner = await scan_sectors(
            db.pool,
            as_of=base + timedelta(days=70),
            operator_user_id=owner,
        )
        repeat_other = await scan_sectors(
            db.pool,
            as_of=base + timedelta(days=70),
            operator_user_id=other_owner,
        )
        audiences = {
            row["audience_user_id"]
            for row in await db.pool.fetch(
                "SELECT audience_user_id FROM claim "
                "WHERE entity_id = $1 AND claim_type = 'sector_score'",
                etf_ids["XLK"],
            )
        }

        assert first_owner.scored == 1
        assert first_other.scored == 1
        assert repeat_owner.skipped_unchanged == 1
        assert repeat_other.skipped_unchanged == 1
        assert audiences == {owner, other_owner}
