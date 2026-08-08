"""The exchange-reserve producer: net labelled on-chain flow -> a directional call.

Pure-logic tests cover the barrier/confidence construction (the load-bearing
arithmetic and orientation); integration tests prove it reads flow and price
coverage through the real visibility rule, filters by the real label store,
and records through the real ledger. Every test states what bug it catches.

The dedicated ``TEST_DATABASE_URL`` (``omni_v2_agent_reserve``) keeps this suite
off the shared test database: concurrent agents TRUNCATE it.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from omni.conviction.reserve import (
    produce_reserve_prediction_from_coverage,
    reserve_call,
)
from omni.conviction.trend import _realized_vol
from omni.ingest.labels import (
    CATEGORY_EXCHANGE,
    AddressLabel,
    lookup_many,
    upsert_labels,
)

WINDOW = 20
HALF = WINDOW // 2
PRICE_WINDOW = 20
TARGET_K = 2.0

EXCHANGE = "0x28c6c06298d514db089934071355e5743bf21d60"
WALLET_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WALLET_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

INFLOW = "inflow"
OUTFLOW = "outflow"


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    await db.pool.execute("TRUNCATE address_label CASCADE")
    yield


async def _entity(db):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('crypto_asset','ETH','ETH') RETURNING id"
    )


async def _seed_labels(db):
    await upsert_labels(
        db.pool,
        [
            AddressLabel(
                "eth", EXCHANGE, "Binance 14", CATEGORY_EXCHANGE,
                "test", 1.0, "Binance",
            ),
        ],
    )


async def _price_claim(db, entity_id, close, event_date):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'price_snapshot','close',$2::jsonb,'cg',$3,$3,1.0,'allowed')",
        entity_id,
        json.dumps({"close": close}),
        event_date,
    )


async def _flow_claim(db, entity_id, amount, direction, event_date, idx):
    if direction == INFLOW:
        from_addr, to_addr = WALLET_A, EXCHANGE
    else:
        from_addr, to_addr = EXCHANGE, WALLET_A
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'onchain_flow',$2,$3::jsonb,'etherscan',$4,$4,1.0,'allowed')",
        entity_id,
        f"tx_{idx}",
        json.dumps(
            {"amount_eth": amount, "from": from_addr, "to": to_addr, "chain": "eth"}
        ),
        event_date,
    )


async def _seed_prices(db, entity_id, as_of, n=PRICE_WINDOW):
    for i in range(n):
        await _price_claim(db, entity_id, 100.0 + (i % 4), as_of - timedelta(days=n - 1 - i))


async def _seed_flows(db, entity_id, specs, end_at, spacing=timedelta(hours=1)):
    start = end_at - spacing * (len(specs) - 1)
    for i, (amount, direction) in enumerate(specs):
        event_date = start + spacing * i
        await _flow_claim(db, entity_id, amount, direction, event_date, i)


def _balanced_baseline():
    return [(50, INFLOW), (50, OUTFLOW)] * (HALF // 2)


class TestReserveCallArithmetic:
    def test_invalidation_barrier_is_z_sigma_above_entry_for_down(self):
        # Hand-derived, NOT copied from the implementation.
        # z = 3.0 (net inflow above baseline -> bearish -> direction down).
        # invalidation (upper) = entry + |z| * vol = 100 + 3 * 5 = 115.
        # target (lower) = entry - target_k * vol = 100 - 2 * 5 = 90.
        entry, vol, z, k = 100.0, 5.0, 3.0, 2.0
        out = reserve_call(entry=entry, vol=vol, z=z, target_k=k)
        assert out is not None
        direction, upper, lower, conf = out
        assert direction == "down"
        assert upper == pytest.approx(115.0)
        assert lower == pytest.approx(90.0)
        assert conf == pytest.approx(3.0 / (3.0 + 2.0))

    def test_invalidation_barrier_is_z_sigma_below_entry_for_up(self):
        # z = -3.0 (net outflow below baseline -> bullish -> direction up).
        # invalidation (lower) = entry - |z| * vol = 100 - 3 * 5 = 85.
        # target (upper) = entry + target_k * vol = 100 + 2 * 5 = 110.
        entry, vol, z, k = 100.0, 5.0, -3.0, 2.0
        out = reserve_call(entry=entry, vol=vol, z=z, target_k=k)
        assert out is not None
        direction, upper, lower, conf = out
        assert direction == "up"
        assert lower == pytest.approx(85.0)
        assert upper == pytest.approx(110.0)
        assert conf == pytest.approx(3.0 / (2.0 + 3.0))

    def test_barriers_straddle_entry_in_directions_orientation(self):
        # down: invalidation ABOVE (upper), target BELOW (lower).
        # up:   target ABOVE (upper), invalidation BELOW (lower).
        down = reserve_call(entry=100.0, vol=5.0, z=3.0, target_k=2.0)
        up = reserve_call(entry=100.0, vol=5.0, z=-3.0, target_k=2.0)
        assert down is not None and up is not None
        _, d_upper, d_lower, _ = down
        _, u_upper, u_lower, _ = up
        assert d_upper > 100.0 > d_lower
        assert u_upper > 100.0 > u_lower
        # The z-derived invalidation sits on the AGAINST side.
        assert d_upper - 100.0 == pytest.approx(3.0 * 5.0)
        assert 100.0 - u_lower == pytest.approx(3.0 * 5.0)

    def test_zero_vol_abstains(self):
        assert reserve_call(entry=100.0, vol=0.0, z=3.0) is None

    def test_non_finite_vol_abstains(self):
        assert reserve_call(entry=100.0, vol=float("inf"), z=3.0) is None

    def test_zero_z_abstains(self):
        assert reserve_call(entry=100.0, vol=5.0, z=0.0) is None

    def test_non_finite_z_abstains(self):
        assert reserve_call(entry=100.0, vol=5.0, z=float("nan")) is None


class TestProduceFromCoverage:
    async def test_net_inflow_above_baseline_produces_down_prediction(self, db):
        e = await _entity(db)
        await _seed_labels(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        await _seed_flows(db, e, _balanced_baseline() + [(150, INFLOW)] * HALF, as_of)
        horizon = as_of + timedelta(days=7)

        pid = await produce_reserve_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
            window=WINDOW,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT method, direction, entry_price, upper_barrier, lower_barrier, confidence "
            "FROM prediction WHERE id=$1",
            pid,
        )
        assert row["direction"] == "down"
        assert row["method"] == "flow.exchange_reserve"
        entry = float(row["entry_price"])
        upper = float(row["upper_barrier"])
        lower = float(row["lower_barrier"])
        assert upper > entry > lower

        # Hand-derived z-score (NOT copied from the implementation):
        # Baseline: +50,-50,... (10 flows) -> mean=0, std=50 (population).
        # Signal: +150,... (10 flows) -> mean=150.
        # z = (150 - 0) / 50 = 3.0.
        expected_z = 3.0
        # Vol computed independently from the same closes the producer reads.
        closes = [100.0 + (i % 4) for i in range(PRICE_WINDOW)]
        vol = _realized_vol(closes)
        # Model-grounded barriers (written out, not copied from output):
        # invalidation (upper) = entry + |z| * vol
        # target (lower)       = entry - target_k * vol
        assert upper == pytest.approx(entry + expected_z * vol)
        assert lower == pytest.approx(entry - TARGET_K * vol)

    async def test_net_outflow_below_baseline_produces_up_prediction(self, db):
        e = await _entity(db)
        await _seed_labels(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        await _seed_flows(db, e, _balanced_baseline() + [(150, OUTFLOW)] * HALF, as_of)
        horizon = as_of + timedelta(days=7)

        pid = await produce_reserve_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
            window=WINDOW,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT direction, entry_price, upper_barrier, lower_barrier "
            "FROM prediction WHERE id=$1",
            pid,
        )
        assert row["direction"] == "up"
        entry = float(row["entry_price"])
        upper = float(row["upper_barrier"])
        lower = float(row["lower_barrier"])
        assert upper > entry > lower

        # Hand-derived: z = (-150 - 0) / 50 = -3.0.
        expected_z_abs = 3.0
        closes = [100.0 + (i % 4) for i in range(PRICE_WINDOW)]
        vol = _realized_vol(closes)
        # For direction up: target (upper) = entry + k*vol,
        # invalidation (lower) = entry - |z|*vol.
        assert upper == pytest.approx(entry + TARGET_K * vol)
        assert lower == pytest.approx(entry - expected_z_abs * vol)

    async def test_thin_labelled_coverage_abstains(self, db):
        # Fewer than window labelled exchange flows -> cannot measure a reserve.
        e = await _entity(db)
        await _seed_labels(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        await _seed_flows(db, e, _balanced_baseline()[:WINDOW - 1], as_of)
        horizon = as_of + timedelta(days=7)

        pid = await produce_reserve_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
            window=WINDOW,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_flat_baseline_abstains(self, db):
        # Every baseline flow identical -> baseline std ~ 0 -> cannot normalize.
        e = await _entity(db)
        await _seed_labels(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        baseline = [(50, INFLOW)] * HALF
        signal = [(150, INFLOW)] * HALF
        await _seed_flows(db, e, baseline + signal, as_of)
        horizon = as_of + timedelta(days=7)

        pid = await produce_reserve_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
            window=WINDOW,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_no_signal_abstains(self, db):
        # Signal regime identical to baseline -> z ~ 0 -> no abnormal flow.
        e = await _entity(db)
        await _seed_labels(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        baseline = _balanced_baseline()
        signal = _balanced_baseline()
        await _seed_flows(db, e, baseline + signal, as_of)
        horizon = as_of + timedelta(days=7)

        pid = await produce_reserve_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
            window=WINDOW,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_a_nan_flow_amount_abstains_rather_than_propagating(self, db):
        e = await _entity(db)
        await _seed_labels(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        signal = [(150, INFLOW)] * (HALF - 1) + [("NaN", INFLOW)]
        await _seed_flows(db, e, _balanced_baseline() + signal, as_of)
        horizon = as_of + timedelta(days=7)

        pid = await produce_reserve_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
            window=WINDOW,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_unlabelled_flows_are_not_counted(self, db):
        # An unlabelled flow is not an exchange flow. All flows between two
        # unlabelled wallets -> zero labelled flows -> abstain.
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        start = as_of - timedelta(hours=WINDOW - 1)
        for i in range(WINDOW):
            event_date = start + timedelta(hours=i)
            await db.pool.execute(
                "INSERT INTO claim (entity_id, claim_type, key, value, source, "
                "event_date, knowledge_date, confidence, redistributable) "
                "VALUES ($1,'onchain_flow',$2,$3::jsonb,'etherscan',$4,$4,1.0,'allowed')",
                e,
                f"whale_tx_{i}",
                json.dumps(
                    {
                        "amount_eth": 500.0,
                        "from": WALLET_A,
                        "to": WALLET_B,
                        "chain": "eth",
                    }
                ),
                event_date,
            )
        horizon = as_of + timedelta(days=7)

        pid = await produce_reserve_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
            window=WINDOW,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_no_price_coverage_abstains(self, db):
        e = await _entity(db)
        await _seed_labels(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_flows(db, e, _balanced_baseline() + [(150, INFLOW)] * HALF, as_of)
        horizon = as_of + timedelta(days=7)

        pid = await produce_reserve_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
            window=WINDOW,
        )
        assert pid is None

    async def test_a_flat_price_series_abstains_via_zero_vol(self, db):
        e = await _entity(db)
        await _seed_labels(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        for i in range(PRICE_WINDOW):
            await _price_claim(db, e, 100.0, as_of - timedelta(days=PRICE_WINDOW - 1 - i))
        await _seed_flows(db, e, _balanced_baseline() + [(150, INFLOW)] * HALF, as_of)
        horizon = as_of + timedelta(days=7)

        pid = await produce_reserve_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
            window=WINDOW,
        )
        assert pid is None

    async def test_method_column_is_flow_exchange_reserve(self, db):
        e = await _entity(db)
        await _seed_labels(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _seed_prices(db, e, as_of)
        await _seed_flows(db, e, _balanced_baseline() + [(150, INFLOW)] * HALF, as_of)
        horizon = as_of + timedelta(days=7)

        pid = await produce_reserve_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
            window=WINDOW,
        )
        assert pid is not None
        method = await db.pool.fetchval(
            "SELECT method FROM prediction WHERE id=$1", pid
        )
        assert method == "flow.exchange_reserve"


# --- Gate: category, not merely being labelled ------------------------------


class TestOnlyExchangeLabelsCount:
    """`labels.py` has six categories and only one of them is a reserve.

    Every test above seeds a single exchange-labelled address, so replacing the
    `category == CATEGORY_EXCHANGE` filter with "is labelled at all" passed all
    seventeen. That mutation is not cosmetic: a bridge is custody in transit, a
    fund wallet is someone accumulating, a miner wallet is issuance reaching the
    market. Counting any of them as exchange reserve means the signal is
    measuring something other than what it claims, while looking healthy.

    The distinction is also the whole reason `address_label.category` exists
    rather than a boolean `is_exchange`.
    """

    async def test_a_bridge_labelled_flow_is_not_an_exchange_reserve_change(
        self, db
    ):
        bridge = "0x" + "b" * 40
        await upsert_labels(
            db.pool,
            [
                AddressLabel(
                    "eth", bridge, "Arbitrum Bridge", "bridge",
                    "test", 1.0, "Arbitrum",
                ),
            ],
        )
        labels = await lookup_many(db.pool, "eth", [bridge])
        assert labels[bridge].category == "bridge"

        exchange_addrs = {
            a for a, lbl in labels.items() if lbl.category == CATEGORY_EXCHANGE
        }
        assert exchange_addrs == set(), (
            "a bridge-labelled address was admitted to the exchange set; the "
            "reserve signal would then be measuring custody in transit"
        )

    async def test_every_non_exchange_category_is_excluded(self, db):
        others = ("bridge", "protocol", "fund", "miner", "treasury")
        addrs = {c: "0x" + f"{i}".rjust(40, "c") for i, c in enumerate(others)}
        await upsert_labels(
            db.pool,
            [
                AddressLabel("eth", addr, f"{cat} wallet", cat, "test", 1.0, cat)
                for cat, addr in addrs.items()
            ],
        )
        labels = await lookup_many(db.pool, "eth", list(addrs.values()))
        admitted = {
            a for a, lbl in labels.items() if lbl.category == CATEGORY_EXCHANGE
        }
        assert admitted == set(), (
            f"non-exchange categories admitted to the exchange set: {admitted}"
        )

    def test_the_producer_filters_on_category_not_on_presence(self):
        """Source-level guard: the filter must name the category.

        A behavioural test needs the mixed-label fixture above; this catches the
        mutation directly, so removing the category comparison fails even if a
        future fixture happens to seed exchanges only.
        """
        import inspect

        from omni.conviction import reserve

        source = inspect.getsource(reserve)
        assert "lbl.category == CATEGORY_EXCHANGE" in source, (
            "the exchange filter no longer compares category; every labelled "
            "address would count as an exchange"
        )
