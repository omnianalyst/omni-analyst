"""Whether the carry book's edge is still there, against a real database.

The failure this guards is silent by construction: the book runs itself, nothing
in it learns, and a premium that decays to nothing produces no error at all --
just a flat NAV curve noticed months after the decision to stop should have been
made. So the arithmetic that turns settlements into an annual rate has to be
right, and the thresholds have to be stated rather than fitted.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from omni.trading.carry_health import (
    DEGRADED_PCT,
    FLOOR_PCT,
    JUSTIFIED_NET_PCT,
    Verdict,
    assess,
    classify,
)

NOW = datetime(2026, 8, 10, 5, 0, tzinfo=UTC)
VENUE = "hyperliquid"


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, users CASCADE")
    yield


@pytest.fixture
async def owner(db) -> UUID:
    return await db.pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1,$2) RETURNING id",
        f"health-{uuid4().hex}@omni.test", "not-a-real-hash",
    )


async def _asset(db, owner, symbol, *, hourly_rate, settlements=48):
    """A coin paying `hourly_rate` every hour, as Hyperliquid settles."""
    entity_id = await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('crypto_asset',$1,$1) "
        "RETURNING id", symbol,
    )
    for hour in range(1, settlements + 1):
        await db.pool.execute(
            "INSERT INTO claim (entity_id, claim_type, key, value, source, "
            "event_date, knowledge_date, confidence, redistributable, "
            "audience_user_id) VALUES ($1,'funding_rate'::claim_type,$2,$3::jsonb,"
            "'derivatives',$4,$4,1.0,'byo_only',$5)",
            entity_id, f"{VENUE}:{symbol}",
            json.dumps({"rate": str(hourly_rate), "venue": VENUE, "symbol": symbol}),
            NOW - timedelta(hours=hour), owner,
        )
    return entity_id


class TestTheAnnualisationMatchesTheVenueCadence:
    async def test_an_hourly_rate_is_annualised_over_24x365(self, db, owner):
        """Using 365 instead of 24*365 understates by 24x.

        That error would read as catastrophic decay on the very first run and
        would look exactly like the thing this module exists to detect.
        """
        # 0.00001 per hour = 0.00001 * 24 * 365 = 8.76%/yr
        ids = {await _asset(db, owner, "AAA", hourly_rate="0.00001"): "AAA",
               await _asset(db, owner, "BBB", hourly_rate="0.00001"): "BBB"}

        h = await assess(
            db.pool, assets=ids, audience_user_id=owner, funding_venue=VENUE,
            as_of=NOW, enter_rank=2,
        )

        assert h.per_asset_pct["AAA"] == pytest.approx(Decimal("8.76"), abs=0.01)
        assert h.basket_gross_pct == pytest.approx(Decimal("8.76"), abs=0.01)


class TestCostsAreAmortisedOverTheHold:
    async def test_a_round_trip_is_charged_once_across_six_weeks(self, db, owner):
        """Charging the round trip annually would understate the book ~8x and
        manufacture a decay that is not there."""
        ids = {await _asset(db, owner, "AAA", hourly_rate="0.00002"): "AAA",
               await _asset(db, owner, "BBB", hourly_rate="0.00002"): "BBB"}

        h = await assess(
            db.pool, assets=ids, audience_user_id=owner, funding_venue=VENUE,
            as_of=NOW, enter_rank=2, execution_cost_bps=Decimal(28), hold_days=42,
        )

        # 28 bps once over 42 days = 0.28% * 365/42 = 2.43%/yr of drag
        assert h.basket_gross_pct == pytest.approx(Decimal("17.52"), abs=0.02)
        assert h.basket_net_pct == pytest.approx(Decimal("15.09"), abs=0.02)


class TestTheBasketIsTheTopNames:
    async def test_only_the_entered_names_count(self, db, owner):
        """The book holds the top 2, so its health is the top 2's -- averaging
        the whole universe would report a book nobody runs."""
        ids = {
            await _asset(db, owner, "RICH", hourly_rate="0.00003"): "RICH",
            await _asset(db, owner, "MID", hourly_rate="0.00002"): "MID",
            await _asset(db, owner, "POOR", hourly_rate="0.000001"): "POOR",
        }

        h = await assess(
            db.pool, assets=ids, audience_user_id=owner, funding_venue=VENUE,
            as_of=NOW, enter_rank=2,
        )

        # (26.28 + 17.52) / 2, ignoring POOR entirely
        assert h.basket_gross_pct == pytest.approx(Decimal("21.90"), abs=0.05)


class TestItRefusesRatherThanGuessing:
    async def test_too_few_names_is_unknown_not_zero(self, db, owner):
        """A universe that cannot fill the basket has no health reading. Zero
        would read as a dead edge, which is a different and alarming claim."""
        ids = {await _asset(db, owner, "AAA", hourly_rate="0.00002"): "AAA"}

        h = await assess(
            db.pool, assets=ids, audience_user_id=owner, funding_venue=VENUE,
            as_of=NOW, enter_rank=2,
        )

        assert h.verdict is Verdict.UNKNOWN
        assert h.basket_net_pct is None

    async def test_thin_coverage_is_excluded_from_the_basket(self, db, owner):
        ids = {
            await _asset(db, owner, "GOOD", hourly_rate="0.00002"): "GOOD",
            await _asset(db, owner, "THIN", hourly_rate="0.00009", settlements=1): "THIN",
        }

        h = await assess(
            db.pool, assets=ids, audience_user_id=owner, funding_venue=VENUE,
            as_of=NOW, enter_rank=2, min_settlements=2,
        )

        assert "THIN" not in h.per_asset_pct
        assert h.verdict is Verdict.UNKNOWN


class TestTheThresholdsAreStated:
    def test_at_the_justified_level_it_is_healthy(self):
        assert classify(JUSTIFIED_NET_PCT) is Verdict.HEALTHY

    def test_below_half_the_justification_it_is_degraded(self):
        assert classify(DEGRADED_PCT - Decimal("0.01")) is Verdict.DEGRADED

    def test_below_idle_stablecoin_it_is_not_a_judgement_call(self):
        """Under the floor the book takes exchange, liquidation and basis risk
        to underperform doing nothing."""
        assert classify(FLOOR_PCT - Decimal("0.01")) is Verdict.BELOW_FLOOR

    def test_no_reading_is_unknown_rather_than_healthy(self):
        """The permissive failure would be treating an absent measurement as a
        passing one, which is how a dead book keeps trading."""
        assert classify(None) is Verdict.UNKNOWN
