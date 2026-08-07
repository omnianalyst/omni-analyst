"""Phase C: the sector scanner loop."""

import json
from datetime import UTC, datetime, timedelta

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


async def _seed_prices(db, entity_id, closes, *, start_days_ago=60):
    base = datetime.now(UTC) - timedelta(days=start_days_ago)
    for i, close in enumerate(closes):
        event = base + timedelta(days=i)
        knowledge = event + timedelta(days=1)
        await db.pool.execute(
            """
            INSERT INTO claim (
                entity_id, claim_type, key, value, source,
                event_date, knowledge_date, confidence,
                redistributable, audience_user_id, derivation
            )
            VALUES ($1, 'price_snapshot', $2, $3::jsonb, 'test',
                    $4, $5, 1.0,
                    'allowed', NULL, 'ingested')
            ON CONFLICT DO NOTHING
            """,
            entity_id, "TEST",
            json.dumps({"close": close, "open": close, "high": close, "low": close, "volume": 1000}),
            event, knowledge,
        )


async def _seed_regime(db, entity_id, cycle_phase="expansion"):
    now = datetime.now(UTC)
    await db.pool.execute(
        """
        INSERT INTO claim (
            entity_id, claim_type, key, value, source,
            event_date, knowledge_date, confidence,
            redistributable, audience_user_id, derivation
        )
        VALUES ($1, 'regime_assessment', 'us_macro', $2::jsonb, 'test',
                $3, $3, 1.0, 'allowed', NULL, 'ingested')
        """,
        entity_id,
        json.dumps({"cycle_phase": cycle_phase, "risk_regime": "risk_on"}),
        now,
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
