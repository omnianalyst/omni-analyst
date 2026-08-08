"""The smart-money producer: labelled fund/treasury accumulation -> a directional call.

Pure-logic tests cover the barrier/confidence construction (the load-bearing
arithmetic and orientation); integration tests prove it reads flow and price
coverage through the real visibility rule, attributes wallets through the real
``address_label`` store, and records through the real ledger. Every test states
what bug it catches.

The dedicated ``TEST_DATABASE_URL`` (``omni_v2_agent_smart_money``) keeps this
suite off the shared test database: concurrent agents TRUNCATE it.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from omni.conviction.smart_money import (
    produce_smart_money_prediction_from_coverage,
    smart_money_call,
)

MIN_WALLETS = 3

# Valid 40-hex (lowercase) EVM addresses. Labels are stored lowercased by
# labels.normalise_address; the flow claim values use the same casing so the
# two agree without any magic.
FUND_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FUND_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
FUND_C = "0xcccccccccccccccccccccccccccccccccccccccc"
FUND_D = "0xdddddddddddddddddddddddddddddddddddddddd"
FUND_E = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
TREAS = "0x1111111111111111111111111111111111111111"
EXCH = "0x28c6c06298d514db089934071355e5743bf21d60"  # Binance 14 (exchange category)
UNLAB = "0x9999999999999999999999999999999999999999"


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    await db.pool.execute("TRUNCATE address_label CASCADE")
    yield


async def _entity(db):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('crypto_asset','ETH','ETH') RETURNING id"
    )


async def _label(db, address, category, *, source="test"):
    await db.pool.execute(
        "INSERT INTO address_label (chain, address, label, category, source, confidence) "
        "VALUES ('eth',$1,$2,$3,$4,1.0)",
        address,
        f"{category} wallet",
        category,
        source,
    )


async def _price_claim(db, entity_id, close, event_date):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'price_snapshot',$2,$3::jsonb,'cg',$4,$4,1.0,'allowed')",
        entity_id,
        f"close-{event_date.date()}-{close}",
        json.dumps({"close": close}),
        event_date,
    )


async def _flow(db, entity_id, frm, to, amount, event_date, *, chain="eth"):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'onchain_flow',$2,$3::jsonb,'etherscan',$4,$4,1.0,'allowed')",
        entity_id,
        f"{frm}->{to}@{event_date.isoformat()}",
        json.dumps(
            {
                "kind": "whale",
                "exchange": None,
                "direction": "inflow",
                "amount_eth": amount,
                "from": frm,
                "to": to,
                "chain": chain,
            }
        ),
        event_date,
    )


async def _seed_prices(db, entity_id, closes, as_of):
    # `closes` oldest-first; entry is the last. Varied so realized vol is nonzero.
    n = len(closes)
    for i, c in enumerate(closes):
        await _price_claim(db, entity_id, c, as_of - timedelta(days=n - 1 - i))


class TestSmartMoneyCallArithmetic:
    def test_invalidation_is_the_window_low_for_an_up_call(self):
        # Hand-derived, NOT copied from the implementation.
        # Up call (accumulation): invalidation is the window LOW. The cohort
        # built its position down to that level; price retaking it rejects the
        # accumulation. target (upper) = entry + target_k * vol = 100 + 2*5 = 110.
        # lower (invalidation) = 88.0 (the window low). confidence = 4/(5+1).
        entry, vol, window_low = 100.0, 5.0, 88.0
        out = smart_money_call(
            entry=entry,
            vol=vol,
            invalidation_level=window_low,
            direction="up",
            n_agree=4,
            n_active=5,
            target_k=2.0,
        )
        assert out is not None
        direction, upper, lower, conf = out
        assert direction == "up"
        assert upper == pytest.approx(110.0)  # entry + k*vol, hand-derived
        assert lower == pytest.approx(88.0)  # the window low, hand-derived
        assert conf == pytest.approx(4 / 6)

    def test_invalidation_is_the_window_high_for_a_down_call(self):
        # Down call (distribution): invalidation is the window HIGH.
        # upper (invalidation) = 115.0. lower (target) = entry - k*vol = 100-10 = 90.
        entry, vol, window_high = 100.0, 5.0, 115.0
        out = smart_money_call(
            entry=entry,
            vol=vol,
            invalidation_level=window_high,
            direction="down",
            n_agree=3,
            n_active=4,
            target_k=2.0,
        )
        assert out is not None
        direction, upper, lower, conf = out
        assert direction == "down"
        assert upper == pytest.approx(115.0)  # the window high, hand-derived
        assert lower == pytest.approx(90.0)  # entry - k*vol, hand-derived
        assert conf == pytest.approx(3 / 5)

    def test_barriers_straddle_entry_in_the_directions_orientation(self):
        # up: invalidation BELOW (lower = window low), target ABOVE (upper).
        # down: invalidation ABOVE (upper = window high), target BELOW (lower).
        up = smart_money_call(
            entry=100.0, vol=5.0, invalidation_level=88.0,
            direction="up", n_agree=3, n_active=3,
        )
        down = smart_money_call(
            entry=100.0, vol=5.0, invalidation_level=115.0,
            direction="down", n_agree=3, n_active=3,
        )
        assert up is not None and down is not None
        _, u_up, u_low, _ = up
        _, d_up, d_low, _ = down
        assert u_up > 100.0 > u_low
        assert d_up > 100.0 > d_low
        # Orientation: the model level sits on the against side for each call.
        assert u_low == pytest.approx(88.0)  # below entry
        assert d_up == pytest.approx(115.0)  # above entry

    def test_confidence_is_agreement_fraction_not_first_passage(self):
        # confidence = n_agree / (n_active + 1): rises with agreements, falls
        # with dissent, never reaches 1.0. Distinct from trend/carry's geometry.
        unanimous = smart_money_call(
            entry=100.0, vol=5.0, invalidation_level=90.0,
            direction="up", n_agree=5, n_active=5,
        )
        narrow = smart_money_call(
            entry=100.0, vol=5.0, invalidation_level=90.0,
            direction="up", n_agree=3, n_active=5,
        )
        assert unanimous is not None and narrow is not None
        _, _, _, c_unan = unanimous
        _, _, _, c_narrow = narrow
        assert c_unan == pytest.approx(5 / 6)
        assert c_narrow == pytest.approx(3 / 6)
        assert c_unan > c_narrow  # more agreement -> more confidence
        assert 0.0 < c_unan < 1.0 and 0.0 < c_narrow < 1.0

    def test_entry_is_the_window_extreme_abstains_no_structural_stop(self):
        # Up call but entry IS the window low: no held level to invalidate
        # against -> the barriers cannot straddle -> refuse.
        assert (
            smart_money_call(
                entry=100.0, vol=5.0, invalidation_level=100.0,
                direction="up", n_agree=3, n_active=3,
            )
            is None
        )

    def test_zero_realized_vol_abstains(self):
        assert (
            smart_money_call(
                entry=100.0, vol=0.0, invalidation_level=88.0,
                direction="up", n_agree=3, n_active=3,
            )
            is None
        )

    def test_non_finite_vol_abstains(self):
        assert (
            smart_money_call(
                entry=100.0, vol=float("inf"), invalidation_level=88.0,
                direction="up", n_agree=3, n_active=3,
            )
            is None
        )

    def test_non_finite_invalidation_level_abstains(self):
        assert (
            smart_money_call(
                entry=100.0, vol=5.0, invalidation_level=float("nan"),
                direction="up", n_agree=3, n_active=3,
            )
            is None
        )


class TestProduceFromCoverage:
    async def test_accumulation_by_three_funds_produces_an_up_prediction(self, db):
        e = await _entity(db)
        for addr in (FUND_A, FUND_B, FUND_C):
            await _label(db, addr, "fund")
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        # Each fund receives ETH from an unlabelled address -> net accumulation.
        for i, addr in enumerate((FUND_A, FUND_B, FUND_C), start=1):
            await _flow(db, e, UNLAB, addr, 100.0, as_of - timedelta(hours=4 - i))
        closes = [85.0, 92.0, 88.0, 96.0, 100.0]  # min 85.0, entry 100.0
        await _seed_prices(db, e, closes, as_of)
        horizon = as_of + timedelta(days=7)

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT method, direction, entry_price, upper_barrier, lower_barrier, confidence "
            "FROM prediction WHERE id=$1",
            pid,
        )
        assert row["method"] == "onchain.smart_money"
        assert row["direction"] == "up"
        entry = float(row["entry_price"])
        upper = float(row["upper_barrier"])
        lower = float(row["lower_barrier"])
        assert upper > entry > lower
        # The invalidation is the accumulation window low, hand-derived = 85.0.
        assert lower == pytest.approx(85.0)
        # confidence = n_agree/(n_active+1) = 3/4 (three funds, unanimous).
        assert float(row["confidence"]) == pytest.approx(3 / 4)

    async def test_distribution_by_three_funds_produces_a_down_prediction(self, db):
        e = await _entity(db)
        for addr in (FUND_A, FUND_B, FUND_C):
            await _label(db, addr, "fund")
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        # Each fund sends ETH to an unlabelled address -> net distribution.
        for i, addr in enumerate((FUND_A, FUND_B, FUND_C), start=1):
            await _flow(db, e, addr, UNLAB, 100.0, as_of - timedelta(hours=4 - i))
        closes = [108.0, 104.0, 115.0, 110.0, 100.0]  # max 115.0, entry 100.0
        await _seed_prices(db, e, closes, as_of)
        horizon = as_of + timedelta(days=7)

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=horizon,
            as_of=as_of,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT direction, entry_price, upper_barrier, lower_barrier "
            "FROM prediction WHERE id=$1",
            pid,
        )
        assert row["direction"] == "down"
        entry = float(row["entry_price"])
        upper = float(row["upper_barrier"])
        lower = float(row["lower_barrier"])
        assert upper > entry > lower
        # The invalidation is the distribution window high, hand-derived = 115.0.
        assert upper == pytest.approx(115.0)

    async def test_fewer_than_min_wallets_agreeing_abstains_anecdote(self, db):
        # One wallet is an anecdote; two agreeing is still below the floor.
        e = await _entity(db)
        await _label(db, FUND_A, "fund")
        await _label(db, FUND_B, "fund")
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _flow(db, e, UNLAB, FUND_A, 100.0, as_of - timedelta(hours=2))
        await _flow(db, e, UNLAB, FUND_B, 100.0, as_of - timedelta(hours=1))
        await _seed_prices(db, e, [85.0, 92.0, 88.0, 96.0, 100.0], as_of)
        horizon = as_of + timedelta(days=7)

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            horizon_ends_at=horizon, as_of=as_of,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_a_single_dominant_wallet_abstains_even_with_large_flow(self, db):
        # One huge accumulator alone is still an anecdote: n_agree=1 < floor.
        e = await _entity(db)
        await _label(db, FUND_A, "fund")
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _flow(db, e, UNLAB, FUND_A, 50_000.0, as_of - timedelta(hours=1))
        await _seed_prices(db, e, [85.0, 92.0, 88.0, 96.0, 100.0], as_of)

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7), as_of=as_of,
        )
        assert pid is None

    async def test_contradictory_flows_that_cancel_abstain(self, db):
        # Fund A accumulates +100, Fund B distributes -100 -> net cancels to ~0.
        e = await _entity(db)
        await _label(db, FUND_A, "fund")
        await _label(db, FUND_B, "fund")
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _flow(db, e, UNLAB, FUND_A, 100.0, as_of - timedelta(hours=2))
        await _flow(db, e, FUND_B, UNLAB, 100.0, as_of - timedelta(hours=1))
        await _seed_prices(db, e, [85.0, 92.0, 88.0, 96.0, 100.0], as_of)

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7), as_of=as_of,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_dissenting_wallets_lower_confidence_but_do_not_abstain(self, db):
        # 3 accumulators (+100 each = +300) vs 2 distributors (-100 each = -200):
        # net +100 -> up. n_active=5, n_agree=3 -> confidence 3/6 = 0.5.
        e = await _entity(db)
        for addr in (FUND_A, FUND_B, FUND_C, FUND_D, FUND_E):
            await _label(db, addr, "fund")
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        for i, addr in enumerate((FUND_A, FUND_B, FUND_C), start=1):
            await _flow(db, e, UNLAB, addr, 100.0, as_of - timedelta(hours=6 - i))
        for i, addr in enumerate((FUND_D, FUND_E), start=1):
            await _flow(db, e, addr, UNLAB, 100.0, as_of - timedelta(hours=3 - i))
        await _seed_prices(db, e, [85.0, 92.0, 88.0, 96.0, 100.0], as_of)

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7), as_of=as_of,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT direction, confidence FROM prediction WHERE id=$1", pid
        )
        assert row["direction"] == "up"
        assert float(row["confidence"]) == pytest.approx(3 / 6)

    async def test_unlabelled_addresses_are_never_treated_as_smart_money(self, db):
        # Large flows between two unlabelled addresses -> no labels -> abstain.
        # A label is never inferred from transaction shape or volume.
        e = await _entity(db)
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        other = "0x7777777777777777777777777777777777777777"
        await _flow(db, e, UNLAB, other, 5_000.0, as_of - timedelta(hours=1))
        await _seed_prices(db, e, [85.0, 92.0, 88.0, 96.0, 100.0], as_of)

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7), as_of=as_of,
        )
        assert pid is None

    async def test_an_exchange_label_is_not_a_smart_money_label(self, db):
        # An exchange-category address has huge flow but is excluded: only
        # fund/treasury count. No fund/treasury wallet present -> abstain.
        e = await _entity(db)
        await _label(db, EXCH, "exchange")
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _flow(db, e, UNLAB, EXCH, 5_000.0, as_of - timedelta(hours=1))
        await _flow(db, e, EXCH, UNLAB, 5_000.0, as_of - timedelta(minutes=30))
        await _seed_prices(db, e, [85.0, 92.0, 88.0, 96.0, 100.0], as_of)

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7), as_of=as_of,
        )
        assert pid is None

    async def test_a_nan_flow_amount_abstains_rather_than_propagating(self, db):
        # A non-finite amount must poison the net check into abstaining.
        e = await _entity(db)
        for addr in (FUND_A, FUND_B, FUND_C):
            await _label(db, addr, "fund")
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _flow(db, e, UNLAB, FUND_A, "NaN", as_of - timedelta(hours=3))
        await _flow(db, e, UNLAB, FUND_B, 100.0, as_of - timedelta(hours=2))
        await _flow(db, e, UNLAB, FUND_C, 100.0, as_of - timedelta(hours=1))
        await _seed_prices(db, e, [85.0, 92.0, 88.0, 96.0, 100.0], as_of)

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7), as_of=as_of,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_a_flat_price_series_abstains_via_zero_vol(self, db):
        # Every close identical -> realized vol ~ 0 -> no honest target barrier.
        e = await _entity(db)
        for addr in (FUND_A, FUND_B, FUND_C):
            await _label(db, addr, "fund")
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _flow(db, e, UNLAB, FUND_A, 100.0, as_of - timedelta(hours=3))
        await _flow(db, e, UNLAB, FUND_B, 100.0, as_of - timedelta(hours=2))
        await _flow(db, e, UNLAB, FUND_C, 100.0, as_of - timedelta(hours=1))
        await _seed_prices(db, e, [100.0, 100.0, 100.0, 100.0, 100.0], as_of)

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7), as_of=as_of,
        )
        assert pid is None

    async def test_no_price_coverage_abstains(self, db):
        e = await _entity(db)
        for addr in (FUND_A, FUND_B, FUND_C):
            await _label(db, addr, "fund")
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _flow(db, e, UNLAB, FUND_A, 100.0, as_of - timedelta(hours=3))
        await _flow(db, e, UNLAB, FUND_B, 100.0, as_of - timedelta(hours=2))
        await _flow(db, e, UNLAB, FUND_C, 100.0, as_of - timedelta(hours=1))

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7), as_of=as_of,
        )
        assert pid is None

    async def test_a_treasury_label_counts_the_same_as_fund(self, db):
        # treasury is the other smart-money category; three treasuries ship.
        e = await _entity(db)
        for addr in (TREAS, FUND_A, FUND_B):
            await _label(db, addr, "treasury")
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _flow(db, e, UNLAB, TREAS, 100.0, as_of - timedelta(hours=3))
        await _flow(db, e, UNLAB, FUND_A, 100.0, as_of - timedelta(hours=2))
        await _flow(db, e, UNLAB, FUND_B, 100.0, as_of - timedelta(hours=1))
        await _seed_prices(db, e, [85.0, 92.0, 88.0, 96.0, 100.0], as_of)

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7), as_of=as_of,
        )
        assert pid is not None
        direction = await db.pool.fetchval(
            "SELECT direction FROM prediction WHERE id=$1", pid
        )
        assert direction == "up"

    async def test_method_column_is_onchain_smart_money(self, db):
        e = await _entity(db)
        for addr in (FUND_A, FUND_B, FUND_C):
            await _label(db, addr, "fund")
        as_of = datetime(2025, 1, 31, tzinfo=UTC)
        await _flow(db, e, UNLAB, FUND_A, 100.0, as_of - timedelta(hours=3))
        await _flow(db, e, UNLAB, FUND_B, 100.0, as_of - timedelta(hours=2))
        await _flow(db, e, UNLAB, FUND_C, 100.0, as_of - timedelta(hours=1))
        await _seed_prices(db, e, [85.0, 92.0, 88.0, 96.0, 100.0], as_of)

        pid = await produce_smart_money_prediction_from_coverage(
            db.pool, entity_id=e, audience_user_id=None,
            horizon_ends_at=as_of + timedelta(days=7), as_of=as_of,
        )
        assert pid is not None
        method = await db.pool.fetchval(
            "SELECT method FROM prediction WHERE id=$1", pid
        )
        assert method == "onchain.smart_money"


# --- Gate: which categories are "smart", and which are emphatically not -----


class TestOnlyFundAndTreasuryAreSmartMoney:
    """The second producer this session whose category filter was untested.

    `reserve.py` had the identical gap: every fixture seeded only the category
    the producer cared about, so replacing the filter with "is labelled at all"
    passed the whole suite. Here that mutation would admit exchange, bridge,
    miner and protocol wallets as smart money -- and each is the OPPOSITE of the
    signal:

      exchange  a deposit is supply arriving where it can be sold, which is
                what flow.exchange_reserve reads as bearish. Counting it as
                accumulation inverts the call.
      miner     outflow is issuance reaching the market, structural selling
                that happens regardless of conviction
      bridge    custody in transit; the same coins, a different chain
      protocol  contract-held balances moving for mechanical reasons

    Only `fund` and `treasury` represent a party taking a deliberate position.
    """

    def test_the_smart_set_is_exactly_fund_and_treasury(self):
        from omni.conviction.smart_money import _SMART_CATEGORIES
        from omni.ingest.labels import CATEGORY_FUND, CATEGORY_TREASURY

        assert _SMART_CATEGORIES == {CATEGORY_FUND, CATEGORY_TREASURY}

    def test_an_exchange_wallet_is_not_smart_money(self):
        from omni.conviction.smart_money import _SMART_CATEGORIES
        from omni.ingest.labels import CATEGORY_EXCHANGE

        # The most dangerous inclusion: an exchange deposit is bearish under
        # flow.exchange_reserve, so admitting it here would make the two
        # producers read the same flow in opposite directions.
        assert CATEGORY_EXCHANGE not in _SMART_CATEGORIES

    def test_no_mechanical_category_is_smart_money(self):
        from omni.conviction.smart_money import _SMART_CATEGORIES
        from omni.ingest.labels import (
            CATEGORY_BRIDGE,
            CATEGORY_EXCHANGE,
            CATEGORY_MINER,
            CATEGORY_PROTOCOL,
        )

        mechanical = {
            CATEGORY_EXCHANGE,
            CATEGORY_BRIDGE,
            CATEGORY_MINER,
            CATEGORY_PROTOCOL,
        }
        admitted = mechanical & _SMART_CATEGORIES
        assert not admitted, (
            f"mechanical categories admitted as smart money: {admitted}. Each "
            f"moves for reasons unrelated to conviction."
        )

    def test_the_producer_filters_on_category_membership(self):
        """Source guard, matching the one added to reserve.py for the same gap.

        A behavioural test needs a mixed-label fixture; this fails the moment
        the membership test disappears, regardless of what the fixtures happen
        to seed.
        """
        import inspect

        from omni.conviction import smart_money

        assert "lbl.category in _SMART_CATEGORIES" in inspect.getsource(
            smart_money
        ), (
            "the smart-money filter no longer checks category membership; every "
            "labelled wallet would count, including exchanges"
        )
