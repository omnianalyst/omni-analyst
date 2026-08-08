from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest

from omni.portfolio.alerts import (
    InvalidRiskAlert,
    RiskAlert,
    RiskAlertKind,
    RiskFinding,
    alert_from_row,
    evaluate,
    load_alerts,
)
from omni.portfolio.reconcile import Discrepancy, Divergence, ReconciliationResult
from omni.portfolio.risk import RiskRefusal

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)
LATER_STILL = NOW + timedelta(minutes=2)

VENUE = "binance"


@pytest.fixture
async def portfolio_id(db):
    pid = await db.pool.fetchval(
        "INSERT INTO portfolio (name, base_currency) VALUES ($1, $2) RETURNING id",
        "risk alert book",
        "USD",
    )
    yield pid
    await db.pool.execute("DELETE FROM portfolio WHERE id = $1", pid)


async def _alert(
    db,
    portfolio_id,
    kind: RiskAlertKind,
    threshold: str,
    *,
    venue: str | None = None,
    stale_after: timedelta | None = None,
) -> RiskAlert:
    row = await db.pool.fetchrow(
        """
        INSERT INTO risk_alert (portfolio_id, kind, threshold, venue, stale_after)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id, portfolio_id, kind, threshold, venue, stale_after
        """,
        portfolio_id,
        kind.value,
        Decimal(threshold),
        venue,
        stale_after,
    )
    return alert_from_row(row)


async def _position(
    db, portfolio_id, venue, symbol, quantity, entry, market_type="spot"
) -> None:
    await db.pool.execute(
        "INSERT INTO position (portfolio_id, venue, symbol, market_type, quantity, "
        "average_entry) VALUES ($1, $2, $3, $4, $5, $6)",
        portfolio_id,
        venue,
        symbol,
        market_type,
        Decimal(quantity),
        Decimal(entry),
    )


async def _cash(db, portfolio_id, amount, venue=VENUE, asset="USD") -> None:
    await db.pool.execute(
        "INSERT INTO cash_balance (portfolio_id, venue, asset, free) "
        "VALUES ($1, $2, $3, $4)",
        portfolio_id,
        venue,
        asset,
        Decimal(amount),
    )


async def _nav_snapshot(db, portfolio_id, nav: str, taken_at: datetime) -> None:
    await db.pool.execute(
        "INSERT INTO nav_snapshot (portfolio_id, nav, cash, gross_exposure, "
        "net_exposure, taken_at) VALUES ($1, $2, $3, $4, $5, $6)",
        portfolio_id,
        Decimal(nav),
        Decimal(0),
        Decimal(0),
        Decimal(0),
        taken_at,
    )


async def _episodes(db, alert: RiskAlert):
    return await db.pool.fetch(
        "SELECT subject, reason, observed, threshold, opened_at, cleared_at, detail "
        "FROM risk_alert_firing WHERE alert_id = $1 "
        'ORDER BY opened_at, subject COLLATE "C"',
        alert.id,
    )


def _divergent(venue: str, at: datetime) -> ReconciliationResult:
    return ReconciliationResult(
        reconciled=False,
        discrepancies=(
            Discrepancy(
                kind=Divergence.POSITION_QUANTITY,
                venue=venue,
                symbol="BTC/USD",
                local=Decimal(2),
                remote=Decimal(3),
                detail="we hold 2, the venue holds 3",
            ),
        ),
        checked_at=at,
        venue=venue,
    )


def _clean(venue: str, at: datetime) -> ReconciliationResult:
    return ReconciliationResult(
        reconciled=True, discrepancies=(), checked_at=at, venue=venue
    )


class TestAlertValidation:
    """A configured alert that cannot be evaluated is worse than no alert.

    It fails by staying silent, and silence is what a working alert also looks
    like, so nobody discovers it. Every shape that would produce one is refused
    at creation.
    """

    def test_a_kind_outside_the_closed_set_never_reaches_the_evaluator(self):
        row = {
            "id": uuid4(),
            "portfolio_id": uuid4(),
            "kind": "price_below",
            "threshold": Decimal("0.1"),
            "venue": None,
            "stale_after": None,
        }
        with pytest.raises(InvalidRiskAlert) as raised:
            alert_from_row(row)
        assert "price_below" in str(raised.value)
        for known in RiskAlertKind:
            assert known.value in str(raised.value)

    @pytest.mark.parametrize("threshold", ["0", "-0.1"])
    def test_a_non_positive_threshold_is_a_disabled_alert_not_a_sensitive_one(
        self, threshold
    ):
        with pytest.raises(InvalidRiskAlert):
            RiskAlert(
                id=uuid4(),
                portfolio_id=uuid4(),
                kind=RiskAlertKind.GROSS_EXPOSURE,
                threshold=Decimal(threshold),
            )

    def test_a_percentage_typed_where_a_share_belongs_is_refused(self):
        # 25 meaning 25% would produce an alert that can never fire, and an
        # alert that never fires is indistinguishable from one that is clear.
        with pytest.raises(InvalidRiskAlert):
            RiskAlert(
                id=uuid4(),
                portfolio_id=uuid4(),
                kind=RiskAlertKind.DRAWDOWN_FROM_PEAK,
                threshold=Decimal(25),
            )

    @pytest.mark.parametrize("threshold", ["NaN", "Infinity"])
    def test_a_non_finite_threshold_is_refused(self, threshold):
        with pytest.raises(InvalidRiskAlert):
            RiskAlert(
                id=uuid4(),
                portfolio_id=uuid4(),
                kind=RiskAlertKind.GROSS_EXPOSURE,
                threshold=Decimal(threshold),
            )

    @pytest.mark.parametrize(
        ("venue", "stale_after"),
        [(None, timedelta(hours=1)), (VENUE, None), (None, None)],
    )
    def test_a_reconciliation_alert_must_name_its_venue_and_its_tolerance(
        self, venue, stale_after
    ):
        with pytest.raises(InvalidRiskAlert):
            RiskAlert(
                id=uuid4(),
                portfolio_id=uuid4(),
                kind=RiskAlertKind.RECONCILIATION,
                threshold=Decimal("0.5"),
                venue=venue,
                stale_after=stale_after,
            )

    @pytest.mark.parametrize(
        ("venue", "stale_after"), [(VENUE, None), (None, timedelta(hours=1))]
    )
    def test_only_a_reconciliation_alert_may_carry_a_venue_or_a_tolerance(
        self, venue, stale_after
    ):
        with pytest.raises(InvalidRiskAlert):
            RiskAlert(
                id=uuid4(),
                portfolio_id=uuid4(),
                kind=RiskAlertKind.GROSS_EXPOSURE,
                threshold=Decimal("0.5"),
                venue=venue,
                stale_after=stale_after,
            )

    def test_a_non_positive_staleness_tolerance_is_refused(self):
        with pytest.raises(InvalidRiskAlert):
            RiskAlert(
                id=uuid4(),
                portfolio_id=uuid4(),
                kind=RiskAlertKind.RECONCILIATION,
                threshold=Decimal("0.5"),
                venue=VENUE,
                stale_after=timedelta(0),
            )

    @pytest.mark.parametrize(
        ("kind", "threshold", "venue", "stale_after"),
        [
            ("price_below", "0.1", None, None),
            ("gross_exposure", "0", None, None),
            ("gross_exposure", "25", None, None),
            ("gross_exposure", "NaN", None, None),
            ("reconciliation", "0.5", None, timedelta(hours=1)),
            ("reconciliation", "0.5", VENUE, None),
            ("gross_exposure", "0.5", VENUE, None),
            ("reconciliation", "0.5", VENUE, timedelta(0)),
        ],
    )
    async def test_the_schema_refuses_every_shape_the_dataclass_refuses(
        self, db, portfolio_id, kind, threshold, venue, stale_after
    ):
        # The validation is not only in Python: a row written by anything else
        # would otherwise sit in the table looking configured.
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await db.pool.execute(
                "INSERT INTO risk_alert (portfolio_id, kind, threshold, venue, "
                "stale_after) VALUES ($1, $2, $3, $4, $5)",
                portfolio_id,
                kind,
                Decimal(threshold),
                venue,
                stale_after,
            )

    async def test_load_alerts_returns_every_active_alert_validated(
        self, db, portfolio_id
    ):
        drawdown = await _alert(db, portfolio_id, RiskAlertKind.DRAWDOWN_FROM_PEAK, "0.1")
        reconciliation = await _alert(
            db,
            portfolio_id,
            RiskAlertKind.RECONCILIATION,
            "0.5",
            venue=VENUE,
            stale_after=timedelta(hours=1),
        )
        await db.pool.execute(
            "UPDATE risk_alert SET active = false WHERE id = $1", reconciliation.id
        )

        loaded = await load_alerts(db.pool, portfolio_id)

        assert {a.id for a in loaded} == {drawdown.id}
        assert loaded[0].threshold == Decimal("0.1")


class TestDrawdown:
    async def test_a_portfolio_with_no_recorded_nav_is_unknown_not_healthy(
        self, db, portfolio_id
    ):
        alert = await _alert(db, portfolio_id, RiskAlertKind.DRAWDOWN_FROM_PEAK, "0.1")

        findings = await evaluate(db.pool, alert, reconciliations=None, now=NOW)

        assert [f.reason for f in findings] == [RiskRefusal.PEAK_NAV_UNKNOWN]
        assert findings[0].observed is None
        assert [r["reason"] for r in await _episodes(db, alert)] == [
            RiskRefusal.PEAK_NAV_UNKNOWN.value
        ]

    async def test_drawdown_is_measured_from_the_recorded_peak_not_the_latest_nav(
        self, db, portfolio_id
    ):
        # Peak 120, present 90. Measured against the present the drawdown would
        # be identically zero, which is what a caller-supplied peak degrades to.
        await _nav_snapshot(db, portfolio_id, "100", NOW - timedelta(days=3))
        await _nav_snapshot(db, portfolio_id, "120", NOW - timedelta(days=2))
        await _nav_snapshot(db, portfolio_id, "90", NOW - timedelta(days=1))
        alert = await _alert(db, portfolio_id, RiskAlertKind.DRAWDOWN_FROM_PEAK, "0.1")

        findings = await evaluate(db.pool, alert, reconciliations=None, now=NOW)

        assert [f.reason for f in findings] == [RiskRefusal.MAX_DRAWDOWN_HIT]
        assert findings[0].observed == Decimal("0.25")
        assert findings[0].threshold == Decimal("0.1")

    @pytest.mark.parametrize(
        ("threshold", "fires"), [("0.2", True), ("0.25", False), ("0.3", False)]
    )
    async def test_the_threshold_is_a_bound_that_must_be_exceeded(
        self, db, portfolio_id, threshold, fires
    ):
        await _nav_snapshot(db, portfolio_id, "120", NOW - timedelta(days=2))
        await _nav_snapshot(db, portfolio_id, "90", NOW - timedelta(days=1))
        alert = await _alert(
            db, portfolio_id, RiskAlertKind.DRAWDOWN_FROM_PEAK, threshold
        )

        findings = await evaluate(db.pool, alert, reconciliations=None, now=NOW)

        assert bool(findings) is fires

    async def test_a_condition_still_holding_is_recorded_once_not_once_per_poll(
        self, db, portfolio_id
    ):
        await _nav_snapshot(db, portfolio_id, "120", NOW - timedelta(days=2))
        await _nav_snapshot(db, portfolio_id, "90", NOW - timedelta(days=1))
        alert = await _alert(db, portfolio_id, RiskAlertKind.DRAWDOWN_FROM_PEAK, "0.1")

        first = await evaluate(db.pool, alert, reconciliations=None, now=NOW)
        second = await evaluate(db.pool, alert, reconciliations=None, now=LATER)

        assert len(first) == 1
        assert second == ()
        rows = await _episodes(db, alert)
        assert [r["opened_at"] for r in rows] == [NOW]
        assert [r["cleared_at"] for r in rows] == [None]

    async def test_a_recovery_closes_the_episode_and_a_later_breach_opens_a_new_one(
        self, db, portfolio_id
    ):
        await _nav_snapshot(db, portfolio_id, "120", NOW - timedelta(days=3))
        await _nav_snapshot(db, portfolio_id, "90", NOW - timedelta(days=2))
        alert = await _alert(db, portfolio_id, RiskAlertKind.DRAWDOWN_FROM_PEAK, "0.1")
        assert len(await evaluate(db.pool, alert, reconciliations=None, now=NOW)) == 1

        await _nav_snapshot(db, portfolio_id, "120", NOW - timedelta(days=1))
        recovered = await evaluate(db.pool, alert, reconciliations=None, now=LATER)

        await _nav_snapshot(db, portfolio_id, "60", NOW)
        again = await evaluate(db.pool, alert, reconciliations=None, now=LATER_STILL)

        assert recovered == ()
        assert [f.observed for f in again] == [Decimal("0.5")]
        rows = await _episodes(db, alert)
        assert [(r["opened_at"], r["cleared_at"]) for r in rows] == [
            (NOW, LATER),
            (LATER_STILL, None),
        ]


class TestGrossExposure:
    @pytest.mark.parametrize(
        ("threshold", "fires"), [("0.5", True), ("0.6", False), ("0.7", False)]
    )
    async def test_gross_exposure_is_a_share_of_nav(
        self, db, portfolio_id, threshold, fires
    ):
        await _cash(db, portfolio_id, "400")
        await _position(db, portfolio_id, VENUE, "BTC/USD", "2", "300")
        alert = await _alert(db, portfolio_id, RiskAlertKind.GROSS_EXPOSURE, threshold)

        findings = await evaluate(db.pool, alert, reconciliations=None, now=NOW)

        assert bool(findings) is fires
        if fires:
            assert findings[0].reason is RiskRefusal.GROSS_EXPOSURE_EXCEEDED
            assert findings[0].observed == Decimal("0.6")

    async def test_a_nav_no_share_expresses_against_is_unknown_not_healthy(
        self, db, portfolio_id
    ):
        alert = await _alert(db, portfolio_id, RiskAlertKind.GROSS_EXPOSURE, "0.5")

        findings = await evaluate(db.pool, alert, reconciliations=None, now=NOW)

        assert [f.reason for f in findings] == [RiskRefusal.NO_STATE_AVAILABLE]
        assert findings[0].observed is None

    async def test_a_negative_nav_does_not_divide_into_a_share_inside_the_limit(
        self, db, portfolio_id
    ):
        # 600 of exposure against a NAV of -400 divides to -1.5, which is under
        # every threshold. An overdrawn book is the least safe one there is, and
        # a check that arrives at "inside the limit" from it has been inverted.
        await _cash(db, portfolio_id, "-1000")
        await _position(db, portfolio_id, VENUE, "BTC/USD", "2", "300")
        alert = await _alert(db, portfolio_id, RiskAlertKind.GROSS_EXPOSURE, "0.5")

        findings = await evaluate(db.pool, alert, reconciliations=None, now=NOW)

        assert [f.reason for f in findings] == [RiskRefusal.NO_STATE_AVAILABLE]


class TestConcentration:
    async def test_only_the_position_that_breached_is_named(self, db, portfolio_id):
        await _cash(db, portfolio_id, "200")
        await _position(db, portfolio_id, VENUE, "BTC/USD", "2", "300")
        await _position(db, portfolio_id, VENUE, "ETH/USD", "10", "20")
        alert = await _alert(
            db, portfolio_id, RiskAlertKind.POSITION_CONCENTRATION, "0.5"
        )

        findings = await evaluate(db.pool, alert, reconciliations=None, now=NOW)

        assert {f.subject for f in findings} == {f"{VENUE}|BTC/USD|spot"}
        assert [f.observed for f in findings] == [Decimal("0.6")]
        assert [f.reason for f in findings] == [RiskRefusal.POSITION_TOO_LARGE]

    async def test_a_book_concentrated_in_aggregate_is_not_a_concentrated_position(
        self, db, portfolio_id
    ):
        # Four positions of 200 against a NAV of 1000: gross is 0.8 of NAV and
        # no single position is above 0.2. A concentration check computed from
        # the book rather than per position cannot tell these apart.
        await _cash(db, portfolio_id, "200")
        await _position(db, portfolio_id, VENUE, "BTC/USD", "2", "100")
        await _position(db, portfolio_id, VENUE, "ETH/USD", "4", "50")
        await _position(db, portfolio_id, "kraken", "SOL/USD", "8", "25")
        await _position(db, portfolio_id, "kraken", "ADA/USD", "20", "10")
        concentration = await _alert(
            db, portfolio_id, RiskAlertKind.POSITION_CONCENTRATION, "0.5"
        )
        gross = await _alert(db, portfolio_id, RiskAlertKind.GROSS_EXPOSURE, "0.5")

        by_position = await evaluate(
            db.pool, concentration, reconciliations=None, now=NOW
        )
        by_book = await evaluate(db.pool, gross, reconciliations=None, now=NOW)

        assert by_position == ()
        assert [f.observed for f in by_book] == [Decimal("0.8")]

    async def test_each_breaching_position_owns_its_own_episode(
        self, db, portfolio_id
    ):
        await _cash(db, portfolio_id, "0")
        await _position(db, portfolio_id, VENUE, "BTC/USD", "2", "250")
        await _position(db, portfolio_id, "kraken", "BTC/USD", "1", "500")
        alert = await _alert(
            db, portfolio_id, RiskAlertKind.POSITION_CONCENTRATION, "0.4"
        )

        findings = await evaluate(db.pool, alert, reconciliations=None, now=NOW)

        assert {f.subject for f in findings} == {
            f"{VENUE}|BTC/USD|spot",
            "kraken|BTC/USD|spot",
        }
        assert {r["subject"] for r in await _episodes(db, alert)} == {
            f.subject for f in findings
        }


class TestReconciliation:
    @pytest.fixture
    async def alert(self, db, portfolio_id):
        return await _alert(
            db,
            portfolio_id,
            RiskAlertKind.RECONCILIATION,
            "0.5",
            venue=VENUE,
            stale_after=timedelta(hours=1),
        )

    @pytest.mark.parametrize("supplied", [None, {}, {"kraken": None}])
    async def test_a_check_that_has_never_run_is_unknown_not_reconciled(
        self, db, alert, supplied
    ):
        findings = await evaluate(
            db.pool, alert, reconciliations=supplied, now=NOW
        )

        assert [f.reason for f in findings] == [RiskRefusal.RECONCILIATION_UNKNOWN]
        assert findings[0].subject == VENUE

    async def test_a_divergent_result_fires_divergence(self, db, alert):
        findings = await evaluate(
            db.pool,
            alert,
            reconciliations={VENUE: _divergent(VENUE, NOW - timedelta(minutes=1))},
            now=NOW,
        )

        assert [f.reason for f in findings] == [RiskRefusal.RECONCILIATION_DIVERGENCE]
        assert Divergence.POSITION_QUANTITY.value in findings[0].detail

    async def test_a_pass_older_than_the_tolerance_is_not_a_current_pass(
        self, db, alert
    ):
        findings = await evaluate(
            db.pool,
            alert,
            reconciliations={VENUE: _clean(VENUE, NOW - timedelta(hours=2))},
            now=NOW,
        )

        assert [f.reason for f in findings] == [RiskRefusal.STALE_DATA]

    async def test_a_fresh_pass_is_the_only_thing_that_fires_nothing(self, db, alert):
        findings = await evaluate(
            db.pool,
            alert,
            reconciliations={VENUE: _clean(VENUE, NOW - timedelta(minutes=1))},
            now=NOW,
        )

        assert findings == ()
        assert await _episodes(db, alert) == []

    async def test_another_venues_result_is_not_evidence_about_this_one(
        self, db, alert
    ):
        findings = await evaluate(
            db.pool,
            alert,
            reconciliations={VENUE: _clean("kraken", NOW - timedelta(minutes=1))},
            now=NOW,
        )

        assert [f.reason for f in findings] == [RiskRefusal.RECONCILIATION_UNKNOWN]
        assert "kraken" in findings[0].detail

    async def test_a_result_stamped_in_the_future_is_not_fresh(self, db, alert):
        findings = await evaluate(
            db.pool,
            alert,
            reconciliations={VENUE: _clean(VENUE, NOW + timedelta(hours=1))},
            now=NOW,
        )

        assert [f.reason for f in findings] == [RiskRefusal.STALE_DATA]

    async def test_a_naive_checked_at_cannot_be_aged_and_does_not_pass(
        self, db, alert
    ):
        findings = await evaluate(
            db.pool,
            alert,
            reconciliations={VENUE: _clean(VENUE, NOW.replace(tzinfo=None))},
            now=NOW,
        )

        assert [f.reason for f in findings] == [RiskRefusal.STALE_DATA]

    async def test_a_changed_reason_closes_the_old_episode_and_opens_a_new_one(
        self, db, alert
    ):
        unknown = await evaluate(db.pool, alert, reconciliations={}, now=NOW)
        diverged = await evaluate(
            db.pool,
            alert,
            reconciliations={VENUE: _divergent(VENUE, LATER)},
            now=LATER,
        )

        assert [f.reason for f in unknown] == [RiskRefusal.RECONCILIATION_UNKNOWN]
        assert [f.reason for f in diverged] == [RiskRefusal.RECONCILIATION_DIVERGENCE]
        rows = await _episodes(db, alert)
        assert [(r["reason"], r["cleared_at"]) for r in rows] == [
            (RiskRefusal.RECONCILIATION_UNKNOWN.value, LATER),
            (RiskRefusal.RECONCILIATION_DIVERGENCE.value, None),
        ]


class TestRecording:
    async def test_a_naive_now_is_refused(self, db, portfolio_id):
        alert = await _alert(db, portfolio_id, RiskAlertKind.DRAWDOWN_FROM_PEAK, "0.1")

        # Matched on the message rather than the type: every finding also
        # refuses a naive stamp, so a test that accepted any ValueError could
        # not tell whether evaluate had checked at all.
        with pytest.raises(ValueError, match="now is naive"):
            await evaluate(
                db.pool, alert, reconciliations=None, now=NOW.replace(tzinfo=None)
            )

    def test_a_breach_cannot_be_constructed_without_the_numbers_behind_it(self):
        with pytest.raises(ValueError, match="must carry"):
            RiskFinding(
                alert_id=uuid4(),
                kind=RiskAlertKind.DRAWDOWN_FROM_PEAK,
                subject="portfolio",
                reason=RiskRefusal.MAX_DRAWDOWN_HIT,
                observed=None,
                threshold=None,
                detail="a breach with no measurement behind it",
                at=NOW,
            )

    async def test_a_subject_cannot_hold_two_open_episodes_at_once(
        self, db, portfolio_id
    ):
        alert = await _alert(db, portfolio_id, RiskAlertKind.DRAWDOWN_FROM_PEAK, "0.1")
        await evaluate(db.pool, alert, reconciliations=None, now=NOW)

        # The dedup is the database's, not the evaluator's memory of what it saw.
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await db.pool.execute(
                "INSERT INTO risk_alert_firing (alert_id, subject, reason, "
                "opened_at, detail) VALUES ($1, $2, $3, $4, $5)",
                alert.id,
                "portfolio",
                RiskRefusal.NO_STATE_AVAILABLE.value,
                LATER,
                "a second open episode for the same subject",
            )

    async def test_a_breach_is_stored_with_the_numbers_that_caused_it(
        self, db, portfolio_id
    ):
        # Stored rather than read back from risk_alert: editing the threshold
        # afterwards must not rewrite what the firing was evidence of.
        await _nav_snapshot(db, portfolio_id, "120", NOW - timedelta(days=2))
        await _nav_snapshot(db, portfolio_id, "90", NOW - timedelta(days=1))
        alert = await _alert(db, portfolio_id, RiskAlertKind.DRAWDOWN_FROM_PEAK, "0.1")
        await evaluate(db.pool, alert, reconciliations=None, now=NOW)

        await db.pool.execute(
            "UPDATE risk_alert SET threshold = $2 WHERE id = $1", alert.id, Decimal("0.9")
        )

        row = (await _episodes(db, alert))[0]
        assert row["observed"] == Decimal("0.25")
        assert row["threshold"] == Decimal("0.1")

    async def test_a_firing_stamps_last_fired_at(self, db, portfolio_id):
        alert = await _alert(db, portfolio_id, RiskAlertKind.GROSS_EXPOSURE, "0.5")

        before = await db.pool.fetchval(
            "SELECT last_fired_at FROM risk_alert WHERE id = $1", alert.id
        )
        await evaluate(db.pool, alert, reconciliations=None, now=NOW)
        after = await db.pool.fetchval(
            "SELECT last_fired_at FROM risk_alert WHERE id = $1", alert.id
        )

        assert before is None
        assert after == NOW

    async def test_the_schema_refuses_a_breach_recorded_without_its_numbers(
        self, db, portfolio_id
    ):
        alert = await _alert(db, portfolio_id, RiskAlertKind.DRAWDOWN_FROM_PEAK, "0.1")

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await db.pool.execute(
                "INSERT INTO risk_alert_firing (alert_id, subject, reason, "
                "opened_at, detail) VALUES ($1, $2, $3, $4, $5)",
                alert.id,
                "portfolio",
                RiskRefusal.MAX_DRAWDOWN_HIT.value,
                NOW,
                "a breach with no measurement behind it",
            )

    async def test_the_schema_refuses_a_reason_outside_the_closed_set(
        self, db, portfolio_id
    ):
        alert = await _alert(db, portfolio_id, RiskAlertKind.DRAWDOWN_FROM_PEAK, "0.1")

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await db.pool.execute(
                "INSERT INTO risk_alert_firing (alert_id, subject, reason, "
                "opened_at, detail) VALUES ($1, $2, $3, $4, $5)",
                alert.id,
                "portfolio",
                "looks_bad",
                NOW,
                "a reason nothing evaluates",
            )
