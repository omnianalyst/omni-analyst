"""Per-kind prediction routing: which producer runs for which entity.

The headline defect this guards: ``scheduler/worker.py`` once filtered the
predict loop with ``e.kind = 'company'``, so a seeded crypto asset with full
price coverage received no prediction at all -- no call, no resolved outcome,
no calibration bucket, permanently UNCALIBRATED. The fix is a registry
(``conviction.producers``) mapping kind -> applicable producers, so the loop
dispatches by kind instead of hardcoding equities. DCF reads EDGAR company
facts and stays company-only; trend reads a price window and is now enabled for
crypto_asset too.

These tests prove the routing through the real loop (``predict_once``) against
the real DB: crypto gets trend and only trend, a company still gets both, an
unregistered kind gets nothing without raising, and the per-(entity, method,
audience) dedupe holds when two producers apply to one kind.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from omni.capabilities.fundamentals import dcf_valuation
from omni.conviction.producers import PRODUCERS, producers_for
from omni.coverage.fundamentals import assemble_fundamentals
from omni.demand.ledger import direct_attention
from omni.scheduler.worker import predict_once

NOW = datetime.now(UTC)

DCF = "fundamentals.dcf_valuation"
TREND = "trend.sma"


class TestRegistryContract:
    def test_company_routes_to_every_producer_that_serves_equities(self):
        """Inclusion, not a fixed roster -- the sixth test this shape has bitten.

        This asserted `== {DCF, TREND}` and broke the moment
        convergence.multistream registered for company as well. What must hold
        is that the two producers equities depend on are reachable, and that a
        producer requiring a claim type no company can have never routes here.
        """
        methods = {p.method for p in producers_for("company")}
        assert {DCF, TREND} <= methods

    def test_no_company_producer_requires_a_crypto_only_claim(self):
        crypto_only = {
            "funding_rate",
            "open_interest",
            "liquidation_event",
            "basis",
            "onchain_flow",
            "protocol_fees",
            "protocol_revenue",
        }
        for producer in producers_for("company"):
            overlap = crypto_only & set(producer.requires_claim_types)
            assert not overlap, (
                f"{producer.method} routes to company but requires {overlap}, "
                f"which no equity will ever have"
            )

    def test_crypto_asset_never_routes_to_a_company_only_producer(self):
        """The invariant is exclusion, not a fixed roster.

        This asserted `== {TREND}` when trend was the only crypto producer, so
        it broke the moment carry.funding registered -- a legitimate addition
        failing a test that was really about something else. What must hold is
        that a producer requiring company-shaped inputs never routes to a
        token: fundamentals.dcf_valuation reads EDGAR company facts and would
        refuse forever on a crypto asset while burning the fill budget on every
        sweep doing it. That is the reason the kind filter exists at all.
        """
        methods = {p.method for p in producers_for("crypto_asset")}
        assert DCF not in methods
        assert TREND in methods
        assert methods, "crypto_asset must route to at least one producer"

    def test_every_crypto_producer_declares_crypto_claim_inputs(self):
        # A producer routed to crypto that requires a company-only claim type
        # would abstain forever. Cheaper to catch here than in the fill budget.
        company_only = {"fundamental_metric", "filing_event"}
        for producer in producers_for("crypto_asset"):
            overlap = company_only & set(producer.requires_claim_types)
            assert not overlap, (
                f"{producer.method} routes to crypto_asset but requires "
                f"{overlap}, which no crypto entity will ever have"
            )

    def test_an_unregistered_kind_routes_to_nothing(self):
        # A kind with no applicable producer is not an error -- it has nothing
        # to say, and the loop must skip it. "chain"/"sector" are real seeded
        # crypto-universe kinds that no prediction producer covers today.
        assert producers_for("chain") == ()
        assert producers_for("sector") == ()
        assert producers_for("nonsense") == ()

    def test_every_producer_carries_its_contract(self):
        for p in PRODUCERS:
            assert p.method
            assert p.entity_kinds
            assert callable(p.produce)
            assert p.requires_claim_types


async def _entity(db, *, kind, symbol, name=None):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ($1,$2,$3) RETURNING id",
        kind, symbol, name or symbol,
    )


async def _price_claim(db, entity_id, price, event_date, *, owner=None):
    shared = owner is None
    await db.pool.execute(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1,'price_snapshot','seed',$2::jsonb,$3,$4,$4,1.0,$5,$6)
        """,
        entity_id,
        json.dumps({"price": price}),
        "seed" if shared else "polygon",
        event_date,
        "allowed" if shared else "byo_only",
        owner,
    )


async def _seed_price_window(db, entity_id, *, n=50, end_price, owner=None):
    """n ascending daily closes ending at end_price, oldest-first by event_date.

    Enough for the trend producer's default 50-bar window and a non-flat
    uptrend (entry ends above its own SMA, vol > 0). The DCF reads only the
    latest price_snapshot, which is end_price here.
    """
    base = datetime(2025, 1, 1, tzinfo=UTC)
    for i in range(n):
        await _price_claim(
            db, entity_id, end_price - (n - 1 - i), base + timedelta(days=i),
            owner=owner,
        )


async def _seed_fundamentals(db, e) -> None:
    end = datetime(2024, 12, 31, tzinfo=UTC)
    filed = end + timedelta(days=46)
    prior_end = datetime(2023, 12, 31, tzinfo=UTC)
    prior_filed = prior_end + timedelta(days=46)
    year_start = (end - timedelta(days=365)).date().isoformat()
    prior_year_start = (prior_end - timedelta(days=365)).date().isoformat()
    pairs = [
        ("NetCashProvidedByUsedInOperatingActivities", 1_200_000, end, filed, year_start),
        ("PaymentsToAcquirePropertyPlantAndEquipment", 200_000, end, filed, year_start),
        ("CommonStockSharesOutstanding", 100_000, end, filed, None),
        ("CashAndCashEquivalentsAtCarryingValue", 500_000, end, filed, None),
        ("StockholdersEquity", 4_000_000, end, filed, None),
        ("LongTermDebt", 2_000_000, end, filed, None),
        ("LongTermDebtCurrent", 100_000, end, filed, None),
        ("Revenues", 5_000_000, end, filed, year_start),
        ("Revenues", 4_000_000, prior_end, prior_filed, prior_year_start),
    ]
    for concept, val, ev, kf, start in pairs:
        await db.pool.execute(
            """
            INSERT INTO claim (entity_id, claim_type, key, value, source,
                               event_date, knowledge_date, confidence,
                               redistributable, audience_user_id, evidence)
            VALUES ($1,'fundamental_metric',$2,$3::jsonb,'sec_edgar',
                    $4,$5,1.0,'allowed',NULL,$6::jsonb)
            """,
            e, concept, json.dumps({"value": val}), ev, kf,
            json.dumps({"cik": "0000320193", "form": "10-K", "fp": "FY", "start": start}),
        )


async def _methods_for(db, entity_id) -> set[str]:
    rows = await db.pool.fetch(
        "SELECT method FROM prediction WHERE entity_id=$1", entity_id
    )
    return {r["method"] for r in rows}


class TestProducerRouting:
    @pytest.fixture(autouse=True)
    async def _clean(self, db):
        await db.pool.execute("TRUNCATE entity CASCADE")
        yield

    async def test_a_demanded_crypto_asset_receives_a_trend_prediction(self, db):
        # The headline: impossible before P18. A seeded crypto asset with full
        # price coverage gets a trend.sma call once the loop routes by kind.
        e = await _entity(db, kind="crypto_asset", symbol="BTC", name="Bitcoin")
        await direct_attention(
            db.pool, entity_id=e, claim_type="price_snapshot", key="BTC"
        )
        await _seed_price_window(db, e, end_price=100.0)

        produced, _abstained = await predict_once(db.pool, horizon_days=90)

        assert produced == 1
        row = await db.pool.fetchrow(
            "SELECT method, direction, entry_price, upper_barrier, lower_barrier "
            "FROM prediction WHERE entity_id=$1",
            e,
        )
        assert row is not None
        assert row["method"] == TREND
        assert row["direction"] == "up"  # ascending series -> entry above SMA
        assert (
            float(row["lower_barrier"])
            < float(row["entry_price"])
            < float(row["upper_barrier"])
        )

    async def test_a_demanded_crypto_asset_does_not_receive_a_dcf_prediction(self, db):
        # DCF reads EDGAR company facts and is meaningless for a token; the
        # registry must keep it company-only even now that crypto is routed.
        e = await _entity(db, kind="crypto_asset", symbol="BTC", name="Bitcoin")
        await direct_attention(
            db.pool, entity_id=e, claim_type="price_snapshot", key="BTC"
        )
        await _seed_price_window(db, e, end_price=100.0)

        await predict_once(db.pool, horizon_days=90)

        methods = await _methods_for(db, e)
        assert DCF not in methods
        assert TREND in methods

    async def test_a_demanded_company_still_receives_both_producers(self, db):
        # Regression: enabling crypto must not change the company path. A
        # company with full coverage gets both calls, exactly as before.
        e = await _entity(db, kind="company", symbol="AAPL", name="Apple")
        await direct_attention(
            db.pool, entity_id=e, claim_type="fundamental_metric", key="AAPL"
        )
        await _seed_fundamentals(db, e)
        # Place the latest price between bear and base fair value so the DCF
        # straddles (direction up); the 50-bar ascending window lets trend fire
        # on the same coverage. Computed from the assembled fundamentals, not
        # hard-coded, so a fixture change moves the entry with it.
        fundamentals = await assemble_fundamentals(
            db.pool, entity_id=e, as_of=NOW, current_price=100.0
        )
        base_out = await dcf_valuation(fundamentals, 100.0)
        base_fv = float(base_out["fair_value_per_share"])
        b_disc = base_out["assumptions"]["discount_rate"]
        bear_fv = float(
            (
                await dcf_valuation(
                    fundamentals, 100.0,
                    growth_rate=0.20 * 0.5, discount_rate=b_disc + 0.02,
                    terminal_growth_rate=0.03,
                )
            )["fair_value_per_share"]
        )
        entry = (bear_fv + base_fv) / 2
        await _seed_price_window(db, e, end_price=entry)

        produced, _abstained = await predict_once(db.pool, horizon_days=90)

        methods = await _methods_for(db, e)
        assert methods == {DCF, TREND}
        assert produced == 2

    async def test_an_unregistered_kind_receives_nothing_and_does_not_raise(self, db):
        # A chain entity is demanded and priced, but no producer is registered
        # for kind 'chain'. The loop skips it -- not an error -- and writes
        # nothing. A kind with no applicable producer has no prediction to make.
        e = await _entity(db, kind="chain", symbol="bitcoin", name="Bitcoin Chain")
        await direct_attention(
            db.pool, entity_id=e, claim_type="price_snapshot", key="bitcoin"
        )
        await _seed_price_window(db, e, end_price=100.0)

        produced, abstained = await predict_once(db.pool, horizon_days=90)

        assert produced == 0
        assert abstained == 0
        assert await _methods_for(db, e) == set()

    async def test_running_the_loop_twice_writes_no_duplicate_pending_per_method(
        self, db,
    ):
        # The dedupe invariant: one pending prediction per (entity, method,
        # audience). A company has two producers; running the loop twice must
        # not double-write either method. Enabling a second producer for a kind
        # cannot be allowed to flood the ledger by re-firing each cycle.
        e = await _entity(db, kind="company", symbol="AAPL", name="Apple")
        await direct_attention(
            db.pool, entity_id=e, claim_type="fundamental_metric", key="AAPL"
        )
        await _seed_fundamentals(db, e)
        fundamentals = await assemble_fundamentals(
            db.pool, entity_id=e, as_of=NOW, current_price=100.0
        )
        base_out = await dcf_valuation(fundamentals, 100.0)
        base_fv = float(base_out["fair_value_per_share"])
        b_disc = base_out["assumptions"]["discount_rate"]
        bear_fv = float(
            (
                await dcf_valuation(
                    fundamentals, 100.0,
                    growth_rate=0.20 * 0.5, discount_rate=b_disc + 0.02,
                    terminal_growth_rate=0.03,
                )
            )["fair_value_per_share"]
        )
        entry = (bear_fv + base_fv) / 2
        await _seed_price_window(db, e, end_price=entry)

        await predict_once(db.pool, horizon_days=90)
        await predict_once(db.pool, horizon_days=90)

        for method in (DCF, TREND):
            n = await db.pool.fetchval(
                "SELECT count(*) FROM prediction "
                "WHERE entity_id=$1 AND method=$2 AND outcome='pending'",
                e, method,
            )
            assert n == 1, f"{method}: expected 1 pending, got {n}"
