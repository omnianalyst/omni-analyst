"""The prediction ledger: writing directional calls, and resolving them.

This is the mechanism the conviction apparatus has never had. `gate.py` derives
its threshold from `calibration_bucket`; `publish.py` reads the same view. Both
are correct and unchanged. But nothing in `src/` ever wrote a `prediction` row,
and nothing resolved one -- so `calibration_bucket` was empty forever and
`assess()` returned `UNCALIBRATED` for every candidate. Running the scheduler
for a year accrued zero predictions. This module is the write path and the
resolve path.

**Two properties, both load-bearing:**

1. `record_prediction` refuses a non-directional result rather than manufacturing
   a barrier. The schema constraint `prediction_barriers_straddle_entry`
   (`upper > entry > lower`) is satisfied by *any* straddling triple, including
   one invented for an analysis that asserted no price. A barrier manufactured
   for a non-price analysis passes the constraint and measures nothing -- which
   is the precise failure mode this project exists not to repeat. The guard is
   here, in Python, before the row is written: a result with no genuine
   entry/upper/lower is rejected, never coerced.

2. `resolve_due_predictions` decides each outcome from observed prices and never
   fabricates a price path. If no price is visible for the window, the
   prediction stays `pending` -- an honest "cannot score" rather than a guess.

Resolution is **self-scoped**. Since 019 a prediction carries its own
`audience_user_id`; `_resolve_one` reads it back and scopes the price path to
it. A shared prediction (NULL) resolves on the shared network and feeds the
shared calibration; an audience-owned prediction resolves on that audience's
visible prices (shared + their byo) and feeds their calibration alone. The
licence leak that motivated this -- an outcome decided by a `byo_only` price
series moving a shared finding's threshold -- is closed structurally: a
prediction's outcome and the calibration bucket it lands in share one audience,
so private outcomes can never reach the shared aggregate. There is no global
resolution audience to get right, which is the point.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from omni.conviction.gate import MIN_RESOLVED_FOR_CALIBRATION  # noqa: F401  (public re-export)
from omni.coverage.visibility import visible_claims_cte


class NonDirectionalResult(Exception):
    """An analysis result carries no genuine directional price assertion.

    Raised when `record_prediction` is handed no entry/upper/lower (the analysis
    asserted no price), so no falsifiable barrier triple exists. Manufacturing
    one would satisfy `prediction_barriers_straddle_entry` and score nothing --
    the worst outcome named in the work order. Refused, never coerced.
    """


_INSERT_PREDICTION = """
INSERT INTO prediction (
    entity_id, claim_id, method, direction, confidence,
    entry_price, upper_barrier, lower_barrier, horizon_ends_at, provenance,
    audience_user_id, created_at
) VALUES ($1,$2,$3,$4::prediction_direction,$5,$6,$7,$8,$9,$10::jsonb,$11,$12)
RETURNING id
"""

# Pending predictions whose horizon has elapsed. Written to use the partial
# index `prediction_due` on (horizon_ends_at) WHERE outcome = 'pending' rather
# than scanning the table. Resolution happens at the horizon: the whole window
# is examined to find the first barrier crossed (or expiry), so a prediction is
# never resolved before its horizon and never swept to `expiry` early.
_DUE_PREDICTIONS = """
SELECT id FROM prediction
WHERE outcome = 'pending' AND horizon_ends_at <= $1
ORDER BY horizon_ends_at
"""

_LOCK_PREDICTION = """
SELECT id, entity_id, direction, entry_price, upper_barrier, lower_barrier,
       horizon_ends_at, created_at, audience_user_id
FROM prediction
WHERE id = $1 AND outcome = 'pending'
FOR UPDATE SKIP LOCKED
"""

# The price path over the prediction's window, scoped by the visibility rule
# from coverage/visibility.py: an audience sees the shared network plus their
# own private claims. The audience is the prediction's OWN audience_user_id
# (read back in _resolve_one): a shared prediction resolves on the shared
# network, an audience-owned prediction on that audience's visible set. Either
# way the outcome lands in the calibration bucket of the same audience, so a
# private price series can never move a shared threshold.
#
# The rule is COMPOSED from visibility.visible_claims_cte(), not restated here.
# That module's docstring is explicit that it "exists once rather than at each
# call site", and it is deliberately a fragment precisely so callers can add
# their own filters on top. A hand-copied predicate would be a second copy of
# the one rule that must never be got wrong, free to drift from the original
# without any test noticing.
_PRICE_PATH = f"""
WITH visible AS (
{visible_claims_cte("$4")}
)
SELECT c.event_date, c.value
FROM visible c
WHERE c.entity_id = $1
  AND c.claim_type = 'price_snapshot'
  AND c.event_date >= $2
  AND c.event_date <= $3
  AND c.knowledge_date <= $5
ORDER BY c.event_date
"""


def _check_straddle(entry: float, upper: float, lower: float) -> None:
    # Mirror prediction_barriers_straddle_entry in Python so a non-straddling
    # triple is rejected with a clear error before reaching the DB, and so the
    # refusal is testable independently of the constraint name.
    if not (upper > entry > lower):
        raise ValueError(
            f"barriers must straddle entry (upper > entry > lower); "
            f"got upper={upper}, entry={entry}, lower={lower}"
        )


async def record_prediction(
    pool,
    *,
    entity_id: UUID,
    capability: str,
    direction: str,
    confidence: float,
    entry_price: float | None,
    upper_barrier: float | None,
    lower_barrier: float | None,
    horizon_ends_at: datetime,
    input_claim_ids: Sequence[str] = (),
    assumptions: dict | None = None,
    method: str | None = None,
    claim_id: UUID | None = None,
    audience_user_id: UUID | None = None,
    created_at: datetime | None = None,
) -> UUID:
    """Write a falsifiable prediction from a directional analysis result.

    `entry_price` / `upper_barrier` / `lower_barrier` are the genuine barriers
    the call was made under, supplied by the caller. They are NOT derived or
    defaulted here: a result that asserts no price has none to give, and the
    caller passes `None`, which this function refuses with
    `NonDirectionalResult` rather than inventing a straddling triple.

    `audience_user_id` is the access-control key, mirroring claim.audience_user
    id: `None` is a shared prediction (resolves on the shared network, feeds the
    shared calibration every audience reads); a user id is a private prediction
    (resolves on that audience's visible prices, feeds their calibration alone).
    The caller is the analysis, which knows which audience it ran for. The
    licence class is implied by NULL-ness, exactly as 001's CHECK makes it
    isomorphic with redistributable on claims -- there is no separate column to
    drift against.

    `created_at` defaults to now (a live call). A backtest replay passes the
    historical decision time so the entry price -- which is point-in-time --
    lines up with the window the resolver scores against. The entry/barriers are
    still the caller's genuine values; only the timestamp is settable, and it is
    a point-in-time attribute, not an outcome field.

    `method` is the calibration grouping key. It defaults to `capability`: the
    unit at which a hit rate is meaningful is the analysis itself, because two
    different analyses have unrelated error distributions and pooling them
    produces a calibrated number that describes neither. Coarser ("equity") or
    finer ("capability + entity") would respectively pool unlike things or never
    reach the resolved-sample floor. The capability name is the natural grain.

    `provenance` records which capability made the call, which input claims fed
    it, and the assumptions it was contingent on. A DCF fair-value is a function
    of its terminal-growth and discount-rate assumptions; a prediction that does
    not carry them cannot be honestly scored later, because the outcome is only
    meaningful relative to the call as it was actually made.
    """
    if entry_price is None or upper_barrier is None or lower_barrier is None:
        raise NonDirectionalResult(
            f"{capability} produced no directional price assertion "
            f"(entry/upper/lower required); refusing to manufacture a barrier"
        )
    entry_f = float(entry_price)
    upper_f = float(upper_barrier)
    lower_f = float(lower_barrier)
    _check_straddle(entry_f, upper_f, lower_f)

    if method is None:
        method = capability

    provenance = {
        "capability": capability,
        "input_claims": [str(c) for c in input_claim_ids],
        "assumptions": assumptions or {},
    }
    written_at = created_at if created_at is not None else datetime.now(UTC)
    return await pool.fetchval(
        _INSERT_PREDICTION,
        entity_id,
        claim_id,
        method,
        direction,
        confidence,
        # Decimal(str(f)), not the raw float. asyncpg encodes a Python float
        # into NUMERIC as its full binary expansion, so a barrier of 0.1 is
        # stored as 0.1000000000000000055511151231257827 -- digits the
        # observation never had, persisted as though measured. Every barrier
        # distance, and therefore every expectancy figure the gate reads, would
        # inherit that noise. str() gives the shortest repr that round-trips the
        # float, which is the honest width of what was actually computed.
        Decimal(str(entry_f)),
        Decimal(str(upper_f)),
        Decimal(str(lower_f)),
        horizon_ends_at,
        json.dumps(provenance),
        audience_user_id,
        written_at,
    )


def _price_point(value: dict) -> tuple[float | None, float | None, float | None]:
    # CoinGecko snapshots carry a single `price`; Polygon bars carry OHLC. Use
    # high/low to detect a crossing when they are present (a bar can touch a
    # barrier intraday without closing beyond it); fall back to the scalar
    # close/price. Returns (scalar, high, low), each None when absent.
    scalar = value.get("price")
    if scalar is None:
        scalar = value.get("close")
    return (
        float(scalar) if scalar is not None else None,
        float(value["high"]) if value.get("high") is not None else None,
        float(value["low"]) if value.get("low") is not None else None,
    )


def _barrier_exit(
    outcome: str, upper: float, lower: float, at: datetime
) -> tuple[str, datetime, float, datetime]:
    # A barrier outcome closes AT the barrier: that level is the falsifiable
    # threshold fixed at write time, not wherever the discrete sample that
    # revealed the crossing happened to print. Mapping outcome -> barrier lives
    # here alone so no branch below can disagree with another about it.
    return outcome, at, (upper if outcome == "upper" else lower), at


def _decide_outcome(
    *,
    direction: str,
    entry: float,
    upper: float,
    lower: float,
    horizon_ends_at: datetime,
    samples: list[tuple[datetime, float | None, float | None, float | None]],
) -> tuple[str, datetime | None, float | None, datetime | None]:
    """Decide a prediction's outcome, and the price it closed at, from a path.

    `samples` are `(event_date, scalar, high, low)` in event_date order, already
    restricted to the window. Returns `(outcome, resolved_at, exit_price,
    exit_at)`; outcome is `pending` only when no barrier was touched and the
    caller must not have called with a passed horizon -- exposed for reuse, not
    expected from the resolver.

    Both-barriers-crossed: the barrier whose crossing is *observed first* in
    event_date order wins. Price snapshots are discrete; this is the finest
    ordering the available granularity supports, and it is a time order, not
    "whichever the code checks first". Where a single observation spans both
    barriers (one Polygon bar whose high >= upper and low <= lower) the
    intra-bar sequence is genuinely unknowable, and the conservative resolution
    is applied: the outcome that counts as a miss for the prediction's
    direction. The system's danger is manufacturing credibility, so the
    indeterminate case is scored against the predictor, never for it.

    **The exit price** is what GATE A found missing: a barrier outcome's
    realised move was recoverable from the row, an `expiry`'s was not, so a
    third of the sample was scored at zero for want of an observed number. An
    expiry closes at the LAST OBSERVED price in the window, carrying that
    observation's OWN timestamp -- which is why `exit_at` is returned separately
    from `resolved_at` (the horizon) rather than reusing it. Stamping the last
    price with the horizon would assert a price was observed at a moment it was
    not, and interpolating one to the horizon would invent it outright. When the
    path holds no scalar price at all (a range-only bar with no close), the exit
    is `None`: the outcome is still decided -- the range shows neither barrier
    was touched -- but the mid of a range is an invention, and 044's CHECK keeps
    the missing price and its missing time together.
    """
    upper_crossed_at: datetime | None = None
    lower_crossed_at: datetime | None = None
    last_price: float | None = None
    last_price_at: datetime | None = None

    for event_date, scalar, high, low in samples:
        top = high if high is not None else scalar
        bottom = low if low is not None else scalar
        touched_upper = top is not None and top >= upper
        touched_lower = bottom is not None and bottom <= lower

        if touched_upper and touched_lower and upper_crossed_at is None and lower_crossed_at is None:
            # Both barriers spanned within one observation: the granularity
            # cannot order them. Conservative miss for this direction.
            return _barrier_exit(_miss_outcome(direction), upper, lower, event_date)

        if touched_upper and upper_crossed_at is None:
            upper_crossed_at = event_date
        if touched_lower and lower_crossed_at is None:
            lower_crossed_at = event_date
        if scalar is not None:
            last_price, last_price_at = scalar, event_date

    if upper_crossed_at is not None and lower_crossed_at is not None:
        if upper_crossed_at < lower_crossed_at:
            return _barrier_exit("upper", upper, lower, upper_crossed_at)
        if lower_crossed_at < upper_crossed_at:
            return _barrier_exit("lower", upper, lower, lower_crossed_at)
        # Observed crossed on the same event_date: order unknowable.
        return _barrier_exit(_miss_outcome(direction), upper, lower, upper_crossed_at)

    if upper_crossed_at is not None:
        return _barrier_exit("upper", upper, lower, upper_crossed_at)
    if lower_crossed_at is not None:
        return _barrier_exit("lower", upper, lower, lower_crossed_at)

    # No barrier was touched. If the window was observed, this is expiry --
    # price stayed within the barriers until the horizon elapsed, and
    # resolved_at is when the horizon passed. If nothing was observed at all,
    # the outcome is indeterminate: asserting "neither crossed" with no price
    # path would fabricate the very thing the outcome is decided on, so it
    # stays pending rather than being scored against an invented path.
    if not samples:
        return "pending", None, None, None
    return "expiry", horizon_ends_at, last_price, last_price_at


def _miss_outcome(direction: str) -> str:
    # The outcome that does NOT count as a hit for `direction`. calibration_bucket
    # scores a hit as (up->upper) | (down->lower) | (neutral->expiry); the miss
    # is the complementary barrier. For `neutral` either barrier is a miss
    # (expiry is the only hit and both were crossed); `upper` is chosen
    # deterministically.
    if direction == "up":
        return "lower"
    if direction == "down":
        return "upper"
    return "upper"


async def _resolve_one(pool, prediction_id: UUID, knowledge_cutoff: datetime) -> bool:
    """Resolve a single prediction under a row lock. Returns True if resolved.

    The SELECT ... FOR UPDATE SKIP LOCKED and the UPDATE share one transaction
    (the `pool.acquire() + conn.transaction()` convention used by writer.py and
    gaps.py), so a second worker that reaches the same row while the first holds
    the lock gets nothing back and skips -- the same `SKIP LOCKED` convention the
    fill path uses to lease gaps. No lease columns are needed because resolution
    is a fast, non-external computation done entirely inside the transaction.

    The price path is scoped to the prediction's OWN audience_user_id (read back
    from the locked row), not a global parameter: a shared prediction resolves
    on the shared network, a private one on its owner's visible prices. The
    outcome therefore always lands in the calibration bucket of the same
    audience that decided it.
    """
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(_LOCK_PREDICTION, prediction_id)
        if row is None:
            return False

        prices = await conn.fetch(
            _PRICE_PATH,
            row["entity_id"],
            row["created_at"],
            row["horizon_ends_at"],
            row["audience_user_id"],
            knowledge_cutoff,
        )
        samples = []
        for rec in prices:
            raw = rec["value"]
            value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            scalar, high, low = _price_point(value)
            if scalar is None and high is None and low is None:
                continue
            samples.append((rec["event_date"], scalar, high, low))

        outcome, resolved_at, exit_price, exit_at = _decide_outcome(
            direction=row["direction"],
            entry=float(row["entry_price"]),
            upper=float(row["upper_barrier"]),
            lower=float(row["lower_barrier"]),
            horizon_ends_at=row["horizon_ends_at"],
            samples=samples,
        )
        if outcome == "pending" or resolved_at is None:
            return False

        await conn.execute(
            "UPDATE prediction "
            "SET outcome = $1::prediction_outcome, resolved_at = $2, "
            "    exit_price = $3, exit_at = $4 "
            "WHERE id = $5",
            outcome,
            resolved_at,
            # Via str(), not Decimal(float): asyncpg encodes a float into NUMERIC
            # as its full binary expansion (0.1 lands as 0.1000000000000000055...),
            # which stores noise the observation never had.
            None if exit_price is None else Decimal(str(exit_price)),
            exit_at,
            prediction_id,
        )
        return True


async def resolve_due_predictions(
    pool, *, now: datetime | None = None
) -> int:
    """Resolve every pending prediction whose horizon has elapsed.

    Returns the number resolved. Each prediction resolves against its own
    audience's visible prices (read back from the row in `_resolve_one`): a
    shared prediction on the shared network, a private one on its owner's. A
    prediction whose horizon has not elapsed is never returned by the candidate
    query and so cannot be swept to `expiry` early; a prediction whose horizon
    has elapsed but whose barriers were never touched resolves to `expiry`.
    """
    if now is None:
        now = datetime.now(UTC)

    due = await pool.fetch(_DUE_PREDICTIONS, now)
    resolved = 0
    for rec in due:
        if await _resolve_one(pool, rec["id"], now):
            resolved += 1
    if resolved:
        # The materialized statistics views only change when outcomes do, and
        # this pass is the only writer of outcomes. Refreshing here bounds
        # their staleness at one resolve interval for every reader.
        from omni.conviction.stats_refresh import refresh_statistics

        await refresh_statistics(pool)
    return resolved
