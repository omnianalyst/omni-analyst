"""Convergence producer: independent families agreeing on a DIRECTION.

``convergence/detect.py`` finds N independent claim families co-occurring about
one entity inside a window. Nothing turned one into a falsifiable prediction, so
the class could never accrue resolved history and ``gate.py`` would refuse it
forever. This is that write path.

**Co-occurrence is not direction.** ``detect`` measures that several independent
families all had something to say at once; it does not say up or down. A
directional call needs AGREEMENT IN SIGN on top of the co-occurrence, so this
module reads the constituent claims back and asks each family which way it
pointed over the same window. Unanimity is required: if two families that both
pointed disagree, that is a convergence of attention and not of direction, and
the honest answer is abstention even though ``detect`` found one.

**Which families can point, and why the rest are silent.** A family votes only
where this codebase already fixes the sign of that observation against price.
Nothing here invents a reading:

- ``price`` and ``microstructure`` vote with the sign of the move in the level
  they observe over the window -- the aggregator's close, the venue's mid, the
  venue's traded price. They are separate families because ``detect`` holds them
  to be independent observation streams, not because the quantity differs.
- ``flow`` votes with the sign of net labelled exchange flow, in the convention
  ``conviction/reserve.py`` states: net INflow is supply arriving where it can be
  sold (``down``), net OUTflow is supply leaving circulation (``up``). Whale and
  unlabelled transfers are not exchange flows and do not vote.
- ``derivatives`` is SILENT, deliberately. It is tempting to read positive
  funding as crowded longs, but ``carry.py`` -- the one producer in this
  codebase that reads funding directionally -- says in as many words that "the
  directional bet is NOT that price falls". Its ``down`` is a carry thesis, not
  a price forecast, so borrowing it here would be a price reading nothing in
  this repository supports.
- ``narrative`` is SILENT because no sentiment exists to read: ``news_event``
  carries title/url/feed and ``perception_news`` carries article and headline
  COUNTS. Treating article volume as positive sentiment would fabricate the
  sign outright.
- ``fundamentals`` and ``macro`` are SILENT because the sign of a change in
  either against this entity's price is model-dependent and unrecorded --
  ``protocol_fundamentals.py`` needs a multiple to read fees, and whether a
  rising FRED series is bullish depends on which series it is.

A silent family does not veto and does not vote: it cannot agree, so it cannot
be counted as agreement. Because the caller asked for ``min_families``-way
corroboration, that many families must actually POINT the same way -- otherwise
a three-family convergence with two silent members would ship as a call from one
signal standing alone, which is the exact thing this class exists to improve on.
The presence threshold itself is never reimplemented here; ``detect`` owns it.

**The invalidation barrier, in one sentence: it is the level at which the first
of the agreeing families stops agreeing** -- each level-observing family read a
move away from where it opened the window, so a return to the nearest of those
window-open levels erases that family's move and the agreement the call was
written on. It is derived from the constituent claims, exactly as ``trend.py``'s
stop is the moving average the call was predicated on, and it is not a chosen
percentage. The target is a volatility-scaled move, sized with the same realized
vol ``trend.py`` uses.

**Confidence is the family count and nothing else.** ``detect`` deliberately has
no scoring constants and none are added here: with ``n`` families agreeing on a
binary direction, the chance that ``n`` independent, unbiased family reads would
all have pointed this way is ``2**-n``, and the confidence recorded is its
complement, ``1 - 2**-n`` -- 0.75 at two families, 0.875 at three, 0.9375 at
four. It is monotone in the count by construction, has no free parameter, and is
a statement about the corroboration rather than a calibrated hit rate; turning
it into one is the ledger's resolution pass, which buckets these on 0.1 widths
and so scores each family count separately.

Abstention is honest, not failure: fewer than ``min_families`` families present
(``detect``'s call), families that point in different directions, fewer than
``min_families`` families actually pointing, a non-finite reading anywhere, no
price coverage to anchor entry, zero or non-finite realized vol, or barriers
that fail to straddle entry -- each returns ``None`` rather than a manufactured
call.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from omni.convergence import CLAIM_FAMILIES, detect
from omni.conviction.ledger import record_prediction
from omni.conviction.trend import _price_window, _realized_vol
from omni.coverage.visibility import visible_claims_cte

_CAPABILITY = "convergence.multistream"
METHOD = "convergence.multistream"

# Three is the smallest demand that cannot be met by two views of one move:
# `price` and `microstructure` are both level observers, so a two-family
# agreement can be an aggregator and a venue watching the same tick, while a
# third necessarily brings in `flow`, which observes something else entirely.
DEFAULT_MIN_FAMILIES = 3

# The closed window `detect` counts over. One day is the shortest window in
# which the daily `price_snapshot` cadence can show a move at all (two closes on
# a closed interval); anything shorter makes the price family structurally
# silent rather than merely quiet.
DEFAULT_WINDOW = timedelta(days=1)

DEFAULT_TARGET_K = 2.0
DEFAULT_PRICE_WINDOW = 20

# One idiom (|x| <= rtol * scale, never ==) and one tolerance, because every
# guard in this module is made scale-consistent by dividing through by a scale
# taken from the same observations in the same units: a level move against the
# levels themselves, a net flow against gross flow, realized vol against entry.
# An absolute tolerance could not do that here -- 1e-9 of a dollar and 1e-9 of a
# sub-cent token are not the same test.
_FLAT_RTOL = 1e-9

_LEVEL_FAMILIES = ("price", "microstructure")
_FLOW_FAMILY = "flow"

# Where each level-observing claim type keeps its level, in preference order.
_LEVEL_KEYS: dict[str, tuple[str, ...]] = {
    "price_snapshot": ("close", "price"),
    "orderbook_snapshot": ("mid",),
    "trade_tape": ("price",),
}

# reserve.py's convention: inflow is supply arriving at the venue where it can
# be sold. `whale` (unlabelled) transfers are absent on purpose -- an unlabelled
# flow is not an exchange flow.
_EXCHANGE_ORIENTATION = {"inflow": 1.0, "outflow": -1.0}


@dataclass(frozen=True)
class _FamilyVote:
    """Which way one family pointed, and the level at which it stops pointing.

    `flips_at` is None for a family whose read has no price-space level (net
    flow crosses zero in ETH, not in dollars).
    """

    family: str
    sign: int
    flips_at: float | None


def _value(raw) -> dict | None:
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    return raw if isinstance(raw, dict) else None


def _number(raw) -> float | None:
    """The reading as a float, or None when there is no reading to parse.

    A successfully parsed non-finite value (`NaN`/`inf`) is KEPT so it poisons
    the finiteness check into abstaining, rather than being dropped and leaving
    a confident call computed from the survivors.
    """
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _all_finite(readings: list[float]) -> bool:
    return all(math.isfinite(r) for r in readings)


def _sign(quantity: float, scale: float) -> int:
    if abs(quantity) <= _FLAT_RTOL * scale:
        return 0
    return 1 if quantity > 0.0 else -1


def _levels(rows: list) -> list[float]:
    """The family's observed levels, in event_date order."""
    levels: list[float] = []
    for row in rows:
        keys = _LEVEL_KEYS.get(row["claim_type"])
        if keys is None:
            continue
        value = _value(row["value"])
        if value is None:
            continue
        for key in keys:
            reading = _number(value.get(key))
            if reading is not None:
                levels.append(reading)
                break
    return levels


def _signed_flows(rows: list) -> list[float]:
    """Labelled exchange flows, positive for inflow, in event_date order."""
    flows: list[float] = []
    for row in rows:
        if row["claim_type"] != "onchain_flow":
            continue
        value = _value(row["value"])
        if value is None:
            continue
        orientation = _EXCHANGE_ORIENTATION.get(value.get("direction"))
        if orientation is None:
            continue
        amount = _number(value.get("amount_eth"))
        if amount is None:
            continue
        flows.append(orientation * amount)
    return flows


def _level_vote(family: str, levels: list[float]) -> _FamilyVote | None:
    # One observation is not a move: a family seen once inside the window has
    # nothing to point with.
    if len(levels) < 2:
        return None
    opened, closed = levels[0], levels[-1]
    sign = _sign(closed - opened, max(abs(opened), abs(closed)))
    if sign == 0:
        return None
    return _FamilyVote(family=family, sign=sign, flips_at=opened)


def _flow_vote(flows: list[float]) -> _FamilyVote | None:
    if not flows:
        return None
    # Net against GROSS: flows that cancel are a balanced book, not a direction.
    sign = _sign(math.fsum(flows), math.fsum(abs(f) for f in flows))
    if sign == 0:
        return None
    return _FamilyVote(family=_FLOW_FAMILY, sign=-sign, flips_at=None)


def _votes(by_family: dict[str, list]) -> list[_FamilyVote] | None:
    """One vote per family that points, or None when a reading is non-finite."""
    votes: list[_FamilyVote] = []
    for family in sorted(by_family):
        rows = by_family[family]
        if family in _LEVEL_FAMILIES:
            readings = _levels(rows)
            if not _all_finite(readings):
                return None
            vote = _level_vote(family, readings)
        elif family == _FLOW_FAMILY:
            readings = _signed_flows(rows)
            if not _all_finite(readings):
                return None
            vote = _flow_vote(readings)
        else:
            continue
        if vote is not None:
            votes.append(vote)
    return votes


def _confidence(agreeing: int) -> float:
    return 1.0 - 2.0 ** (-agreeing)


async def _constituent_claims(
    pool, *, claim_ids: tuple[UUID, ...], audience: UUID | None
) -> list:
    """The convergence's own claims, re-read through the visibility rule.

    The ids came from an audience-scoped read, and this one is scoped again
    rather than selecting from `claim` directly: a query path that returns
    claims filters on the licence rule, without exception.
    """
    return await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$1")}
        )
        SELECT c.id, c.claim_type::text AS claim_type, c.event_date, c.value
        FROM visible c
        WHERE c.id = ANY($2)
        ORDER BY c.event_date, c.id
        """,
        audience,
        list(claim_ids),
    )


async def produce_convergence_prediction_from_coverage(
    pool,
    *,
    entity_id: UUID,
    audience_user_id: UUID | None,
    horizon_ends_at: datetime,
    as_of: datetime,
    method: str = METHOD,
    created_at: datetime | None = None,
    window: timedelta = DEFAULT_WINDOW,
    min_families: int = DEFAULT_MIN_FAMILIES,
    target_k: float = DEFAULT_TARGET_K,
    price_window: int = DEFAULT_PRICE_WINDOW,
) -> UUID | None:
    """Produce a directional call from a multi-family convergence, or abstain.

    Delegates the presence threshold to `convergence.detect`, derives a
    direction from the sign each constituent family pointed over the same
    window, and records a call whose invalidation is the level at which the
    first of those families stops agreeing and whose target is vol-scaled.
    Returns the new prediction id, or `None`.
    """
    converged = await detect(
        pool,
        entity_id=entity_id,
        audience_user_id=audience_user_id,
        window=window,
        min_families=min_families,
        as_of=as_of,
    )
    if converged is None:
        return None

    rows = await _constituent_claims(
        pool, claim_ids=converged.claim_ids, audience=audience_user_id
    )
    by_family: dict[str, list] = {}
    for row in rows:
        family = CLAIM_FAMILIES.get(row["claim_type"])
        if family is None:
            continue
        by_family.setdefault(family, []).append(row)

    votes = _votes(by_family)
    if votes is None:
        return None
    if not votes:
        return None
    if len({v.sign for v in votes}) > 1:
        return None
    if len(votes) < min_families:
        return None
    direction = "up" if votes[0].sign > 0 else "down"

    # The nearest window-open level is the first agreement to break: for an up
    # call price reaches the highest of them first on the way back down.
    flips_at = [v.flips_at for v in votes if v.flips_at is not None]
    if not flips_at:
        return None
    invalidation = max(flips_at) if direction == "up" else min(flips_at)

    closes = await _price_window(
        pool,
        entity_id=entity_id,
        audience=audience_user_id,
        as_of=as_of,
        limit=price_window,
    )
    if not closes:
        return None
    if not all(math.isfinite(c) and c > 0.0 for c in closes):
        return None
    entry = closes[-1]

    vol = _realized_vol(closes)
    if not math.isfinite(vol) or vol <= _FLAT_RTOL * entry:
        return None

    if direction == "up":
        upper, lower = entry + target_k * vol, invalidation
    else:
        upper, lower = invalidation, entry - target_k * vol
    if not (upper > entry > lower):
        return None

    return await record_prediction(
        pool,
        entity_id=entity_id,
        capability=_CAPABILITY,
        method=method,
        direction=direction,
        confidence=_confidence(len(votes)),
        entry_price=entry,
        upper_barrier=upper,
        lower_barrier=lower,
        horizon_ends_at=horizon_ends_at,
        audience_user_id=audience_user_id,
        created_at=created_at,
        input_claim_ids=converged.claim_ids,
        assumptions={
            "model": "convergence_multistream",
            "window_seconds": window.total_seconds(),
            "min_families": min_families,
            "target_k": target_k,
            "families_present": list(converged.families),
            "families_agreeing": [v.family for v in votes],
            "agreeing_family_count": len(votes),
            "invalidation": invalidation,
            "realized_vol": vol,
            "entry": entry,
            "confidence_model": "complement_of_chance_agreement",
        },
    )
