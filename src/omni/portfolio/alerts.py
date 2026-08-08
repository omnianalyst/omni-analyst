"""Risk alerts: a closed set of conditions over portfolio state.

This is the capital-side sibling of `omni.alerts.rules`, and it is a separate
module rather than another condition kind there because of what an import would
drag with it. A coverage alert is evaluated on the analysis side, against the
claims its owner may see; if `omni.alerts` imported this package to reach a NAV,
the analysis side would know what is held, which is the one direction
`tests/test_trading_isolation.py` exists to forbid. So the design is shared and
the code is not.

What is shared is the discipline. The condition set is **closed**: four fixed,
pure predicates, validated when the alert is created rather than when it is
evaluated, so a kind that reaches the evaluator is always one the evaluator can
run. There is no expression, no eval, no caller-supplied logic -- a rule engine
that ran arbitrary conditions against a portfolio would be a hole, not a feature.

The inversion `risk.py` describes applies here in full, and harder. That module
refuses a trade when it cannot tell; this one has no trade to refuse, so its only
way to say "I could not tell" is to fire. **A threshold that has never been
evaluated must never read as healthy.** An alert's whole value is that silence
means checked-and-fine; the moment silence can also mean not-measured, silence
stops carrying information and every quiet poll is ambiguous. So an absent NAV
history is `PEAK_NAV_UNKNOWN`, a reconciliation that has never run is
`RECONCILIATION_UNKNOWN`, one that ran too long ago is `STALE_DATA`, and a NAV
that no share can be expressed against is `NO_STATE_AVAILABLE`. Each is a firing
with its own reason, not an empty result.

The reasons are `risk.RiskRefusal` members rather than a parallel enum. The two
modules are describing the same facts about the same book from opposite sides --
one refusing an intent, one raising a flag -- and an operator who has learned
what `max_drawdown_hit` means should not have to learn a second name for it.

**Two bases, deliberately, and each condition stays inside one.** Drawdown is
measured on `nav_snapshot`: both the high-water mark and the current reading come
from that table, which is marked. Measuring it against `PortfolioState.nav`
instead would compare a marked peak to a cost-basis present, and a book whose
positions are all down would report a drawdown of nearly zero -- the reading is
wrong in exactly the situation the alert exists for. Gross exposure and
concentration are measured on `PortfolioState`, at average entry, which is the
same basis `risk.check` scores existing positions on; the alternative needs marks
this module is not given and must not invent. Every detail string names the basis
it used, because the two numbers are not interchangeable.

Peak NAV is derived here, never accepted from a caller. A parameter for it is a
parameter someone eventually passes the current NAV into, and a drawdown measured
against the present is identically zero for every portfolio forever.

Firing is recorded, not merely detected, and a firing is an **episode** rather
than an event: opened when the condition starts holding, closed when it stops.
The `(alert_id, subject)` partial unique index on open rows is the real dedup, so
a condition that stays true produces one notification instead of one per poll,
and a condition that clears and returns produces a second one instead of being
swallowed forever by a lifetime-scoped key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from uuid import UUID

from omni.portfolio.reconcile import ReconciliationResult
from omni.portfolio.risk import RiskRefusal
from omni.portfolio.state import PortfolioState, load

ONE = Decimal(1)
ZERO = Decimal(0)

PORTFOLIO_SUBJECT = "portfolio"


class RiskAlertKind(str, Enum):
    DRAWDOWN_FROM_PEAK = "drawdown_from_peak"
    GROSS_EXPOSURE = "gross_exposure"
    POSITION_CONCENTRATION = "position_concentration"
    RECONCILIATION = "reconciliation"


KNOWN_KINDS = frozenset(kind.value for kind in RiskAlertKind)

# The reasons whose meaning is "a number crossed a line". They must carry both
# numbers; the rest are unknowns, and an unknown that carried a figure would be
# claiming a measurement it did not make.
NUMERIC_REASONS = frozenset(
    {
        RiskRefusal.MAX_DRAWDOWN_HIT,
        RiskRefusal.GROSS_EXPOSURE_EXCEEDED,
        RiskRefusal.POSITION_TOO_LARGE,
    }
)

KNOWN_REASONS = NUMERIC_REASONS | frozenset(
    {
        RiskRefusal.RECONCILIATION_DIVERGENCE,
        RiskRefusal.RECONCILIATION_UNKNOWN,
        RiskRefusal.STALE_DATA,
        RiskRefusal.PEAK_NAV_UNKNOWN,
        RiskRefusal.NO_STATE_AVAILABLE,
    }
)


class InvalidRiskAlert(ValueError):
    """An alert the closed set does not recognise or cannot evaluate.

    Raised at creation and again when a stored row is loaded. A row the
    evaluator would skip is a configured safety check that is not running, and
    it fails silently by construction -- nobody notices an alert that never
    fires.
    """


def _usable(value: object) -> bool:
    """A number this module is willing to compute with.

    `Decimal` carries NaN and Infinity, NUMERIC stores both, and every ordering
    comparison against a `Decimal` NaN raises `InvalidOperation` rather than
    returning False. Screening here turns that into a named firing instead of an
    exception thrown from inside a threshold check.
    """
    return isinstance(value, Decimal) and value.is_finite()


@dataclass(frozen=True)
class RiskAlert:
    """One configured condition, validated into something evaluable.

    `venue` and `stale_after` belong to the reconciliation kind and to no other,
    and the pairing is enforced both ways: a reconciliation alert that does not
    name its venue has nothing to check, and a drawdown alert carrying a venue is
    a row somebody meant to be something else.
    """

    id: UUID
    portfolio_id: UUID
    kind: RiskAlertKind
    threshold: Decimal
    venue: str | None = None
    stale_after: timedelta | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RiskAlertKind):
            raise InvalidRiskAlert(
                f"unknown risk alert kind {self.kind!r}; expected one of: "
                f"{', '.join(sorted(KNOWN_KINDS))}"
            )
        if not _usable(self.threshold):
            raise InvalidRiskAlert(
                f"threshold must be a finite Decimal, got {self.threshold!r}"
            )
        if self.threshold <= ZERO:
            raise InvalidRiskAlert(
                f"threshold must be positive, got {self.threshold}; a zero or "
                f"negative threshold is a disabled alert, not a sensitive one"
            )
        if self.threshold > ONE:
            raise InvalidRiskAlert(
                f"threshold is a share and must not exceed 1, got {self.threshold}; "
                f"a percentage typed here is an alert that can never fire"
            )

        reconciliation = self.kind is RiskAlertKind.RECONCILIATION
        if reconciliation != (self.venue is not None):
            raise InvalidRiskAlert(
                f"venue is required by {RiskAlertKind.RECONCILIATION.value} and "
                f"meaningless to every other kind; kind={self.kind.value} "
                f"venue={self.venue!r}"
            )
        if reconciliation != (self.stale_after is not None):
            raise InvalidRiskAlert(
                f"stale_after is required by {RiskAlertKind.RECONCILIATION.value} "
                f"and meaningless to every other kind; kind={self.kind.value} "
                f"stale_after={self.stale_after!r}"
            )
        if self.venue is not None and not self.venue.strip():
            raise InvalidRiskAlert("venue must name a venue")
        if self.stale_after is not None and self.stale_after <= timedelta(0):
            raise InvalidRiskAlert(
                f"stale_after must be positive, got {self.stale_after}; a "
                f"non-positive tolerance calls every result stale forever"
            )


@dataclass(frozen=True)
class RiskFinding:
    """One condition currently holding, with the numbers behind it.

    `observed` and `threshold` are `None` only for the unknown reasons, where
    there is no measurement to report. That is the point of them: an unknown
    that carried a plausible number would be indistinguishable from a reading.
    """

    alert_id: UUID
    kind: RiskAlertKind
    subject: str
    reason: RiskRefusal
    observed: Decimal | None
    threshold: Decimal | None
    detail: str
    at: datetime

    def __post_init__(self) -> None:
        if self.reason not in KNOWN_REASONS:
            raise ValueError(f"{self.reason} is not a risk alert reason")
        if not self.subject.strip():
            raise ValueError("a finding must name its subject")
        if not self.detail.strip():
            raise ValueError("a finding must say what happened")
        if self.at.tzinfo is None:
            raise ValueError(f"at is naive ({self.at}); firings are stamped in UTC")
        if self.reason in NUMERIC_REASONS and (
            self.observed is None or self.threshold is None
        ):
            raise ValueError(
                f"{self.reason.value} is a breach and must carry the number that "
                f"caused it and the line it crossed"
            )


def alert_from_row(row) -> RiskAlert:
    """Rebuild a stored alert, refusing anything the evaluator could not run.

    The database CHECK already forbids an unknown kind. This refuses it a second
    time, because the constraint protects rows written through this schema and
    this protects the evaluator against every other way a row can arrive.
    """
    raw = row["kind"]
    if raw not in KNOWN_KINDS:
        raise InvalidRiskAlert(
            f"stored alert {row['id']} has kind {raw!r}, which is outside the "
            f"closed set: {', '.join(sorted(KNOWN_KINDS))}"
        )
    return RiskAlert(
        id=row["id"],
        portfolio_id=row["portfolio_id"],
        kind=RiskAlertKind(raw),
        threshold=row["threshold"],
        venue=row["venue"],
        stale_after=row["stale_after"],
    )


_ACTIVE_ALERTS = """
SELECT id, portfolio_id, kind, threshold, venue, stale_after
FROM risk_alert
WHERE portfolio_id = $1 AND active
ORDER BY kind, id
"""

# Peak and present are read from the same table in one statement so they cannot
# come from two different moments. The tie-break takes the LOWER nav among
# snapshots sharing a timestamp: two readings stamped the same instant are
# genuinely ambiguous, and the larger drawdown is the reading that raises a flag
# rather than the one that hides it.
_NAV_HISTORY = """
SELECT (SELECT max(nav) FROM nav_snapshot WHERE portfolio_id = $1) AS peak,
       (SELECT nav FROM nav_snapshot WHERE portfolio_id = $1
        ORDER BY taken_at DESC, nav ASC LIMIT 1) AS latest
"""

_OPEN_EPISODES = """
SELECT subject, reason FROM risk_alert_firing
WHERE alert_id = $1 AND cleared_at IS NULL
"""

_CLOSE_EPISODE = """
UPDATE risk_alert_firing SET cleared_at = $4
WHERE alert_id = $1 AND subject = $2 AND reason = $3 AND cleared_at IS NULL
"""

_OPEN_EPISODE = """
INSERT INTO risk_alert_firing
    (alert_id, subject, reason, opened_at, observed, threshold, detail)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (alert_id, subject, reason, opened_at) DO NOTHING
"""

_TOUCH_LAST_FIRED = "UPDATE risk_alert SET last_fired_at = $2 WHERE id = $1"


def _finding(
    alert: RiskAlert,
    *,
    subject: str,
    reason: RiskRefusal,
    detail: str,
    at: datetime,
    observed: Decimal | None = None,
    threshold: Decimal | None = None,
) -> RiskFinding:
    return RiskFinding(
        alert_id=alert.id,
        kind=alert.kind,
        subject=subject,
        reason=reason,
        observed=observed,
        threshold=threshold,
        detail=detail,
        at=at,
    )


def _unusable_nav(
    alert: RiskAlert, state: PortfolioState | None, at: datetime
) -> RiskFinding | None:
    """The reason a share of NAV cannot be computed from this state, or None."""
    if state is None:
        return _finding(
            alert,
            subject=PORTFOLIO_SUBJECT,
            reason=RiskRefusal.NO_STATE_AVAILABLE,
            detail="no portfolio state was available, so no share of NAV was measured",
            at=at,
        )
    if not _usable(state.nav) or state.nav <= ZERO:
        return _finding(
            alert,
            subject=PORTFOLIO_SUBJECT,
            reason=RiskRefusal.NO_STATE_AVAILABLE,
            detail=(
                f"cost-basis NAV is {state.nav}; a share-of-NAV threshold "
                f"expresses nothing against it, so the alert has not been cleared"
            ),
            at=at,
        )
    bad = [
        p
        for p in state.positions
        if not _usable(p.quantity) or not _usable(p.average_entry)
    ]
    if bad:
        return _finding(
            alert,
            subject=PORTFOLIO_SUBJECT,
            reason=RiskRefusal.NO_STATE_AVAILABLE,
            detail=(
                f"{len(bad)} position(s) carry a non-finite quantity or entry "
                f"price, starting with {bad[0].symbol} at {bad[0].venue}; no share "
                f"of NAV can be shown to be inside the threshold"
            ),
            at=at,
        )
    return None


def _drawdown_findings(
    alert: RiskAlert,
    *,
    peak_nav: Decimal | None,
    latest_nav: Decimal | None,
    at: datetime,
) -> tuple[RiskFinding, ...]:
    if peak_nav is None or latest_nav is None:
        return (
            _finding(
                alert,
                subject=PORTFOLIO_SUBJECT,
                reason=RiskRefusal.PEAK_NAV_UNKNOWN,
                detail=(
                    "no NAV snapshot has been recorded for this portfolio, so "
                    "drawdown has not been measured; an unmeasured drawdown is "
                    "not a cleared one"
                ),
                at=at,
            ),
        )
    if not _usable(peak_nav) or peak_nav <= ZERO:
        return (
            _finding(
                alert,
                subject=PORTFOLIO_SUBJECT,
                reason=RiskRefusal.PEAK_NAV_UNKNOWN,
                detail=f"peak NAV {peak_nav} is not a usable high-water mark",
                at=at,
            ),
        )
    if not _usable(latest_nav):
        return (
            _finding(
                alert,
                subject=PORTFOLIO_SUBJECT,
                reason=RiskRefusal.PEAK_NAV_UNKNOWN,
                detail=(
                    f"the most recent NAV snapshot reads {latest_nav}, which no "
                    f"drawdown can be measured against"
                ),
                at=at,
            ),
        )

    drawdown = (peak_nav - latest_nav) / peak_nav
    if drawdown <= alert.threshold:
        return ()
    return (
        _finding(
            alert,
            subject=PORTFOLIO_SUBJECT,
            reason=RiskRefusal.MAX_DRAWDOWN_HIT,
            observed=drawdown,
            threshold=alert.threshold,
            detail=(
                f"marked NAV {latest_nav} is {drawdown} below the recorded peak "
                f"{peak_nav}, past a {alert.threshold} threshold"
            ),
            at=at,
        ),
    )


def _gross_exposure_findings(
    alert: RiskAlert, *, state: PortfolioState | None, at: datetime
) -> tuple[RiskFinding, ...]:
    refusal = _unusable_nav(alert, state, at)
    if refusal is not None:
        return (refusal,)

    gross = state.gross_exposure
    if not _usable(gross):
        return (
            _finding(
                alert,
                subject=PORTFOLIO_SUBJECT,
                reason=RiskRefusal.NO_STATE_AVAILABLE,
                detail=f"gross exposure is {gross}, which no threshold can bound",
                at=at,
            ),
        )
    share = gross / state.nav
    if share <= alert.threshold:
        return ()
    return (
        _finding(
            alert,
            subject=PORTFOLIO_SUBJECT,
            reason=RiskRefusal.GROSS_EXPOSURE_EXCEEDED,
            observed=share,
            threshold=alert.threshold,
            detail=(
                f"gross exposure {gross} at average entry is {share} of cost-basis "
                f"NAV {state.nav}, past a {alert.threshold} threshold"
            ),
            at=at,
        ),
    )


def _concentration_findings(
    alert: RiskAlert, *, state: PortfolioState | None, at: datetime
) -> tuple[RiskFinding, ...]:
    refusal = _unusable_nav(alert, state, at)
    if refusal is not None:
        return (refusal,)

    found: list[RiskFinding] = []
    for position in state.positions:
        share = position.notional / state.nav
        if share <= alert.threshold:
            continue
        found.append(
            _finding(
                alert,
                subject=f"{position.venue}|{position.symbol}|{position.market_type.value}",
                reason=RiskRefusal.POSITION_TOO_LARGE,
                observed=share,
                threshold=alert.threshold,
                detail=(
                    f"{position.symbol} ({position.market_type.value}) at "
                    f"{position.venue} is {position.notional} at average entry, "
                    f"{share} of cost-basis NAV {state.nav}, past a "
                    f"{alert.threshold} threshold"
                ),
                at=at,
            )
        )
    return tuple(found)


def _reconciliation_findings(
    alert: RiskAlert,
    *,
    result: ReconciliationResult | None,
    at: datetime,
) -> tuple[RiskFinding, ...]:
    """At most one finding, because a venue is one subject and an episode is one row.

    Precedence is unknown, then divergence, then staleness. A result that both
    diverges and is stale reports the divergence -- the stronger and more
    actionable statement -- and names its age in the detail rather than in a
    second firing the open-episode index could not hold.
    """
    venue = alert.venue
    if result is None:
        return (
            _finding(
                alert,
                subject=venue,
                reason=RiskRefusal.RECONCILIATION_UNKNOWN,
                detail=(
                    f"no reconciliation has been recorded for {venue}; an unrun "
                    f"check has not been cleared, and local state has not been "
                    f"shown to match the venue"
                ),
                at=at,
            ),
        )
    if result.venue != venue:
        return (
            _finding(
                alert,
                subject=venue,
                reason=RiskRefusal.RECONCILIATION_UNKNOWN,
                detail=(
                    f"the result offered for {venue} reports venue "
                    f"{result.venue!r}; another venue's reconciliation is not "
                    f"evidence about this one"
                ),
                at=at,
            ),
        )
    if result.checked_at.tzinfo is None:
        return (
            _finding(
                alert,
                subject=venue,
                reason=RiskRefusal.STALE_DATA,
                detail=(
                    f"the reconciliation for {venue} is stamped "
                    f"{result.checked_at}, which is naive and cannot be aged "
                    f"against {at}"
                ),
                at=at,
            ),
        )

    age = at - result.checked_at
    if not result.reconciled:
        kinds = ", ".join(sorted({d.kind.value for d in result.discrepancies}))
        return (
            _finding(
                alert,
                subject=venue,
                reason=RiskRefusal.RECONCILIATION_DIVERGENCE,
                detail=(
                    f"local state diverges from {venue}: {kinds} "
                    f"({len(result.discrepancies)} discrepancy(ies), checked "
                    f"{result.checked_at}, {age} ago)"
                ),
                at=at,
            ),
        )
    if age < timedelta(0):
        return (
            _finding(
                alert,
                subject=venue,
                reason=RiskRefusal.STALE_DATA,
                detail=(
                    f"the reconciliation for {venue} is stamped {-age} in the "
                    f"future; the clocks disagree and neither reading can be "
                    f"trusted"
                ),
                at=at,
            ),
        )
    if age > alert.stale_after:
        return (
            _finding(
                alert,
                subject=venue,
                reason=RiskRefusal.STALE_DATA,
                detail=(
                    f"{venue} last reconciled {result.checked_at}, {age} ago, "
                    f"past a {alert.stale_after} tolerance; a pass this old is "
                    f"not a current one"
                ),
                at=at,
            ),
        )
    return ()


def _assess(
    alert: RiskAlert,
    *,
    state: PortfolioState | None,
    peak_nav: Decimal | None,
    latest_nav: Decimal | None,
    reconciliation: ReconciliationResult | None,
    at: datetime,
) -> tuple[RiskFinding, ...]:
    """Pure: every condition of this alert that currently holds.

    One finding per subject at most, which is what lets each one own an episode
    row. Every input that is absent produces a finding rather than an empty
    result, so an empty result means measured-and-inside, and nothing else.
    """
    if alert.kind is RiskAlertKind.DRAWDOWN_FROM_PEAK:
        return _drawdown_findings(
            alert, peak_nav=peak_nav, latest_nav=latest_nav, at=at
        )
    if alert.kind is RiskAlertKind.GROSS_EXPOSURE:
        return _gross_exposure_findings(alert, state=state, at=at)
    if alert.kind is RiskAlertKind.POSITION_CONCENTRATION:
        return _concentration_findings(alert, state=state, at=at)
    return _reconciliation_findings(alert, result=reconciliation, at=at)


async def _record(pool, alert: RiskAlert, findings, *, now: datetime):
    """Open an episode for each new finding, close the ones that no longer hold.

    Closing runs first: a subject whose reason changed has an open row under the
    old reason, and the partial unique index on open episodes would refuse the
    new one while it is still open. Doing both in one transaction means an
    operator never sees the same subject flagged twice for contradictory
    reasons.
    """
    current = {(f.subject, f.reason.value): f for f in findings}

    async with pool.acquire() as conn, conn.transaction():
        open_episodes = {
            (row["subject"], row["reason"])
            for row in await conn.fetch(_OPEN_EPISODES, alert.id)
        }

        for subject, reason in sorted(open_episodes - set(current)):
            await conn.execute(_CLOSE_EPISODE, alert.id, subject, reason, now)

        opened = [f for key, f in current.items() if key not in open_episodes]
        for finding in opened:
            await conn.execute(
                _OPEN_EPISODE,
                alert.id,
                finding.subject,
                finding.reason.value,
                finding.at,
                finding.observed,
                finding.threshold,
                finding.detail,
            )
        if opened:
            await conn.execute(_TOUCH_LAST_FIRED, alert.id, now)

    return tuple(opened)


async def load_alerts(pool, portfolio_id: UUID) -> tuple[RiskAlert, ...]:
    """Every active alert on a portfolio, each validated into something evaluable."""
    rows = await pool.fetch(_ACTIVE_ALERTS, portfolio_id)
    return tuple(alert_from_row(row) for row in rows)


async def evaluate(
    pool,
    alert: RiskAlert,
    *,
    reconciliations: Mapping[str, ReconciliationResult] | None,
    now: datetime,
) -> tuple[RiskFinding, ...]:
    """Record and return the conditions that newly hold for this alert.

    Only the inputs the alert's kind actually consults are read, so a drawdown
    alert does not load a position book and a reconciliation alert touches no
    portfolio table at all.

    `reconciliations` carries the most recent result per venue and has no
    default. `None`, an empty mapping, and a mapping missing this alert's venue
    all mean the same thing and all produce `RECONCILIATION_UNKNOWN`: nothing has
    verified this venue, and the caller does not get to express that by omission.

    Returns the episodes newly opened. A condition still holding since the last
    call returns nothing -- it has already been recorded, and re-reporting it on
    every poll is how an alerting system teaches people to ignore it.
    """
    if now.tzinfo is None:
        raise ValueError(
            f"now is naive ({now}); firings are stamped in UTC and a naive "
            f"reading silently shifts them"
        )

    state: PortfolioState | None = None
    peak_nav: Decimal | None = None
    latest_nav: Decimal | None = None
    reconciliation: ReconciliationResult | None = None

    if alert.kind in (
        RiskAlertKind.GROSS_EXPOSURE,
        RiskAlertKind.POSITION_CONCENTRATION,
    ):
        state = await load(pool, alert.portfolio_id)
    elif alert.kind is RiskAlertKind.DRAWDOWN_FROM_PEAK:
        row = await pool.fetchrow(_NAV_HISTORY, alert.portfolio_id)
        peak_nav = row["peak"]
        latest_nav = row["latest"]
    else:
        reconciliation = (
            None if reconciliations is None else reconciliations.get(alert.venue)
        )

    findings = _assess(
        alert,
        state=state,
        peak_nav=peak_nav,
        latest_nav=latest_nav,
        reconciliation=reconciliation,
        at=now,
    )
    return await _record(pool, alert, findings, now=now)


__all__ = [
    "KNOWN_KINDS",
    "KNOWN_REASONS",
    "NUMERIC_REASONS",
    "InvalidRiskAlert",
    "RiskAlert",
    "RiskAlertKind",
    "RiskFinding",
    "alert_from_row",
    "evaluate",
    "load_alerts",
]
