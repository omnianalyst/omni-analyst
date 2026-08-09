"""Cross-sectional funding carry: which names the delta-neutral book holds.

`carry.funding` asks one question about one asset -- "is this funding stream
persistent enough to be worth the price risk?" -- and answers it as a
directional call because that is the only shape the ledger has. This module asks
the question that pays: **of the names available, which ones are paying the most
right now.**

The position it selects for is a pair -- long spot, short perp -- held delta
neutral, whose entire return is the funding collected. A **positive funding rate
means longs pay shorts** (`portfolio/state.py::_funding_amount` derives the whole
sign convention from that sentence), so a short perp *receives* on a positive
rate and the book wants the names ranked HIGHEST on trailing funding. There is
no `direction` here, no barrier and no horizon: nothing is asserted about price.

**Hysteresis is the economics, not a refinement.** Measured over 2,831
rebalances (GATE_A_FINDINGS Finding 9), selecting the top funding quartile earns
+8.74% annualised gross -- and rebalancing every settlement to keep chasing it
costs 29.19%, so the strategy that looks like +8.7% is actually -20.5%. Slowing
the cadence barely dents the signal, because funding regimes persist; a separate
exit rank removes the rest of the churn, the assets oscillating across the
selection boundary:

    6w, enter top5 / exit top15   gross 7.79%   cost 0.44%   NET +7.35%   t +30.6

That is why `enter_rank` and `exit_rank` are two parameters with no defaults and
no defaulting to one another. Collapsing them is not a tuning choice; it is the
difference between the two numbers above, and it is invisible in the gross one.

**Abstention.** Both entry points return the incoming basket unchanged, naming
the reason, when:

- `no_funding_coverage_visible` -- the window held no funding settlement at all.
  Funding claims are `byo_only` and therefore audience-scoped, so the usual
  cause is an `audience_user_id` that owns none of them; passing `None` sees the
  shared network alone and the shared network holds no venue data.
- `universe_too_small` -- fewer ranked names than `minimum_universe`. A
  cross-sectional rank over three assets is not a ranking.
- `non_finite_score` -- a `NaN` or infinite rate reached a score. The name is
  not dropped: every comparison against `NaN` is false, so a silently discarded
  one would shrink the universe and shift every rank below it without a trace.

Not rebalancing is the honest null action for a carry book. Liquidating because
the data thinned would be a trade the signal never asked for, and turnover is
the one thing measured to destroy this strategy.
"""

from __future__ import annotations

import json
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Generic, TypeVar
from uuid import UUID

from omni.coverage.visibility import visible_claims_cte

_CAPABILITY = "carry.crosssectional"
METHOD = "carry.crosssectional"

# The trailing window Finding 9 scored on. Wall-clock rather than a settlement
# count so every name is ranked over the same period.
DEFAULT_LOOKBACK_DAYS = 7

# A hard floor, not just a default: the score is a mean, and a mean of one
# observation is the current level rather than a trailing average. Callers may
# raise it; `select_carry_basket` refuses to go below it.
MIN_SETTLEMENTS = 2

# The entered names must be at most this fraction of the universe -- see
# `minimum_universe`.
UNIVERSE_MULTIPLE_OF_ENTER = 2

ABSTAIN_NO_COVERAGE = "no_funding_coverage_visible"
ABSTAIN_UNIVERSE_TOO_SMALL = "universe_too_small"
ABSTAIN_NON_FINITE_SCORE = "non_finite_score"

K = TypeVar("K", bound=Hashable)


@dataclass(frozen=True)
class BasketSelection(Generic[K]):
    """What the book should hold after this rebalance.

    `abstention` is `None` on a real decision and names the reason otherwise, in
    which case `held` is the incoming basket unchanged.
    """

    held: frozenset[K]
    abstention: str | None


@dataclass(frozen=True)
class BasketDecision:
    """One rebalance decision, with the turnover it implies.

    `entered` and `exited` are the sets the cost model prices. They are carried
    here rather than left to the caller because turnover is what the strategy is
    graded net of, and a decision that does not state its own turnover cannot be
    graded at all.
    """

    as_of: datetime
    held: frozenset[UUID]
    entered: frozenset[UUID]
    exited: frozenset[UUID]
    scores: dict[UUID, Decimal]
    abstention: str | None


def minimum_universe(*, enter_rank: int, exit_rank: int) -> int:
    """How many ranked names the cross-section needs before it means anything.

    Two independent degeneracies, so the floor is the larger of two:

    - **Nothing can ever exit.** With no more names than `exit_rank`, every name
      sits inside the exit band permanently, hysteresis never releases anything
      and the selector is buy-and-hold-the-universe wearing a rank's clothes.
      Hence `exit_rank + 1` -- at least one name must be capable of being out.
    - **Selection that selects nothing.** "Top five of six" is inclusion, not a
      statement about relative funding. Hence `enter_rank * 2`: the entered
      names must be at most half the universe.
    """
    return max(exit_rank + 1, enter_rank * UNIVERSE_MULTIPLE_OF_ENTER)


def _rank(scores: Mapping[K, Decimal]) -> list[K]:
    """The names best-first: highest trailing funding, ties by key.

    Descending is the direction that pays -- a short perp receives on a positive
    rate. The same measurement that put the top quartile at +8.74% put the
    bottom at -2.15%, so an inverted sort is not a weaker strategy, it is the
    losing side of the same one.

    Ties break on the string form of the key, the deterministically-first-by-key
    resolution `basis.py::_pick_anchor` already uses. Two names on identical
    trailing funding are indistinguishable to the signal; with no explicit total
    order Python's stable sort would fall back on mapping insertion order, and
    the same inputs would give different baskets on different runs.
    """
    return sorted(scores, key=lambda k: (-scores[k], str(k)))


def select_basket(
    scores: Mapping[K, Decimal],
    *,
    held: Iterable[K],
    enter_rank: int,
    exit_rank: int,
) -> BasketSelection[K]:
    """The new basket, given trailing scores and what is held now.

    A name enters when it ranks inside `enter_rank` and is released only once it
    falls outside `exit_rank`; between the two it is held but not bought.

    **The basket holds at most `enter_rank` names.** Retained names are kept
    first, and any remaining room is filled from the top of the ranking. An
    earlier version unioned all of `top(enter_rank)` with everything retained
    inside `top(exit_rank)`, which let the basket float up to `exit_rank` -- in
    the measured configuration, drifting to 13.6 names against an enter rank of
    7. That dilutes: half the capital lands in names ranked 8 through 21, which
    by construction pay less than the top 7, and Finding 11 measured the cost of
    it at 0.64pp of net return (+7.16% against +7.80%), more than the churn the
    floating basket saved.

    The cap also makes per-name capital knowable. A book whose size floats
    between 7 and 21 names cannot say what fraction of NAV one position is until
    after the selection runs, and a position that cannot be sized in advance
    cannot be risk-limited in advance.

    The trade-off is deliberate and is the hysteresis working: with the basket
    full of retained names, a higher-ranked entrant waits. Buying it would mean
    selling a name that has not yet left the exit band, which is the churn the
    exit band exists to prevent.

    A held name absent from `scores` -- delisted, or with no funding coverage in
    the window -- cannot be ranked and therefore exits. Holding it would be a
    position no current evidence supports.

    Raises rather than abstains on a configuration that cannot be right:
    `enter_rank` below 1, an `exit_rank` tighter than `enter_rank` (which ejects
    a name the same rank would immediately re-enter -- worse churn than no
    hysteresis at all), or a score that is not a `Decimal`. Rates are exact
    decimal strings in the store and a float score is a lossy one.
    """
    if enter_rank < 1:
        raise ValueError(f"enter_rank must be at least 1, got {enter_rank}")
    if exit_rank < enter_rank:
        raise ValueError(
            f"exit_rank {exit_rank} is tighter than enter_rank {enter_rank}; the exit "
            f"band must contain the entry band or a name is ejected at the rank that "
            f"would buy it straight back"
        )
    for key, score in scores.items():
        if not isinstance(score, Decimal):
            raise TypeError(
                f"score for {key!r} must be a Decimal, got {type(score).__name__}; "
                f"funding rates are exact decimal strings and a float is not one"
            )

    held_now = frozenset(held)
    if not all(score.is_finite() for score in scores.values()):
        return BasketSelection(held=held_now, abstention=ABSTAIN_NON_FINITE_SCORE)
    if len(scores) < minimum_universe(enter_rank=enter_rank, exit_rank=exit_rank):
        return BasketSelection(held=held_now, abstention=ABSTAIN_UNIVERSE_TOO_SMALL)

    ranked = _rank(scores)
    # Retained first: a name already held and still inside the exit band keeps
    # its slot, which is what stops the basket churning. Whatever room is left
    # goes to the highest-ranked names not already in it, in rank order, and the
    # basket stops at `enter_rank` names.
    keep = list(dict.fromkeys(k for k in ranked[:exit_rank] if k in held_now))
    del keep[enter_rank:]
    for key in ranked[:enter_rank]:
        if len(keep) >= enter_rank:
            break
        if key not in keep:
            keep.append(key)
    return BasketSelection(held=frozenset(keep), abstention=None)


async def _funding_window(
    pool,
    *,
    entity_ids: Sequence[UUID],
    audience: UUID | None,
    as_of: datetime,
    lookback_days: int,
    funding_venue: str,
) -> dict[UUID, list[tuple[datetime, Decimal]]]:
    """Trailing funding settlements per entity, oldest-first, as-of `as_of`.

    Point-in-time, on the `trend._price_window` / `reserve._flow_window` idiom: a
    settlement filed after `as_of` is invisible, and visibility runs through
    `visible_claims_cte` so a `byo_only` rate reaches only the audience that
    fetched it.

    Ranked in one query over the whole universe rather than once per entity,
    because the statement being made is comparative. There is no `LIMIT`: the
    window is bounded by wall-clock time, and a flat row cap across entities
    would let a frequently-settling name starve a quiet one -- the trap
    `basis.py::_venue_price_series` partitions around.

    `DISTINCT ON (entity_id, key, event_date)` keeps the most recently knowable
    version of each settlement, so a restated rate is corrected rather than
    counted twice. It groups on the settlement's **exact** stamp: Binance's
    stamps carry a millisecond of jitter (consecutive deltas run 28,799,999 /
    28,800,000 / 28,800,001), so rounding to an eight-hour bucket would merge
    two settlements or split one. `key` carries the venue (`binance:BTCUSDT`),
    which stops one venue's settlement being mistaken for a restatement of
    another's.

    **One venue per ranking**, matched to the accrual path's `funding_venue`.
    Keeping the streams distinct is not the same as keeping them apart: without
    this filter every venue's settlements pool into one window, and
    `_trailing_score` averages them. That average has no unit. Hyperliquid
    settles hourly and Binance every eight hours, so the same annual carry
    arrives as a per-settlement mean eight times smaller, and a blended mean
    ranks a name by which venues happen to cover it. Worse, the score would then
    disagree with the book: `carry_loop._settlements` filters on this same venue,
    so the strategy would select against one number and accrue another.

    Rates are parsed to `Decimal` from the store's exact decimal string. A
    non-finite one (`NaN`, `Infinity`) is KEPT so it can poison the score into
    an abstention; a value that will not parse at all is not a rate and is
    skipped.
    """
    cutoff = as_of - timedelta(days=lookback_days)
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        )
        SELECT DISTINCT ON (c.entity_id, c.key, c.event_date)
               c.entity_id, c.value, c.event_date
        FROM visible c
        WHERE c.entity_id = ANY($1::uuid[])
          AND c.claim_type = 'funding_rate'
          AND split_part(c.key, ':', 1) = $5
          AND c.knowledge_date <= $2
          AND c.event_date >= $4
        ORDER BY c.entity_id, c.key, c.event_date, c.knowledge_date DESC
        """,
        list(entity_ids),
        as_of,
        audience,
        cutoff,
        funding_venue,
    )

    windows: dict[UUID, list[tuple[datetime, Decimal]]] = {}
    for r in rows:
        raw = r["value"]
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            continue
        rate = raw.get("rate")
        if rate is None:
            continue
        try:
            parsed = Decimal(str(rate))
        except InvalidOperation:
            continue
        windows.setdefault(r["entity_id"], []).append((r["event_date"], parsed))

    for settlements in windows.values():
        settlements.sort(key=lambda s: s[0])
    return windows


def _trailing_score(
    settlements: Sequence[tuple[datetime, Decimal]], *, min_settlements: int
) -> Decimal | None:
    """The mean rate per settlement over the window, or `None` when too thin.

    A mean rather than a sum. Names differ in how many settlements landed in the
    same wall-clock window -- a gap in coverage, a venue on a different cadence,
    a second venue filing the same asset -- and a sum would rank a name up for
    printing more often rather than for paying more.

    A non-finite rate anywhere in the window returns `NaN` explicitly instead of
    being summed. Summing is not safe: `Infinity + -Infinity` signals
    `InvalidOperation`, which the default decimal context raises, so the caller
    would get an exception where it needs a refusal.
    """
    if len(settlements) < min_settlements:
        return None
    if any(not rate.is_finite() for _, rate in settlements):
        return Decimal("NaN")
    total = sum((rate for _, rate in settlements), Decimal(0))
    return total / Decimal(len(settlements))


async def select_carry_basket(
    pool,
    *,
    entity_ids: Sequence[UUID],
    audience_user_id: UUID | None,
    as_of: datetime,
    held: Iterable[UUID],
    enter_rank: int,
    exit_rank: int,
    funding_venue: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_settlements: int = MIN_SETTLEMENTS,
) -> BasketDecision:
    """The basket the carry book should hold as-of `as_of`, and its turnover.

    Reads each name's trailing `lookback_days` of funding visible to the
    audience as-of `as_of`, scores it as the mean rate per settlement, and ranks
    the cross-section under the enter/exit hysteresis. `as_of` is required and
    has no clock default, exactly as `trend.produce_trend_prediction_from_coverage`
    requires one: a replay that silently reads *now* is a replay with lookahead.

    `funding_venue` is required and has no default, for the same reason `as_of`
    does not default to the clock: the wrong one is silent. It must be the venue
    the book accrues against, because a score taken from one venue and a
    settlement taken from another are two different strategies sharing a
    portfolio.

    A name with fewer than `min_settlements` in the window is not scored and so
    is not part of the universe -- it has no trailing funding to be ranked on.
    Enough of those and the decision abstains on `universe_too_small`, which is
    the correct outcome rather than a rank over whatever happened to be covered.

    Returns a `BasketDecision` whose `abstention` is `None` on a real decision;
    on an abstention `held` is the incoming basket, `entered` and `exited` are
    empty, and the reason is one of the three module constants.
    """
    if not entity_ids:
        raise ValueError(
            "entity_ids is empty; a cross-sectional rank needs a stated universe, and "
            "an empty one would abstain as though the coverage were missing"
        )
    if lookback_days < 1:
        raise ValueError(f"lookback_days must be at least 1, got {lookback_days}")
    if min_settlements < MIN_SETTLEMENTS:
        raise ValueError(
            f"min_settlements must be at least {MIN_SETTLEMENTS}, got {min_settlements}; "
            f"a mean over fewer observations is the current level, not a trailing average"
        )

    held_now = frozenset(held)
    windows = await _funding_window(
        pool,
        entity_ids=entity_ids,
        audience=audience_user_id,
        as_of=as_of,
        lookback_days=lookback_days,
        funding_venue=funding_venue,
    )
    if not windows:
        return BasketDecision(
            as_of=as_of,
            held=held_now,
            entered=frozenset(),
            exited=frozenset(),
            scores={},
            abstention=ABSTAIN_NO_COVERAGE,
        )

    scores: dict[UUID, Decimal] = {}
    for entity_id, settlements in windows.items():
        score = _trailing_score(settlements, min_settlements=min_settlements)
        if score is not None:
            scores[entity_id] = score

    selection = select_basket(
        scores, held=held_now, enter_rank=enter_rank, exit_rank=exit_rank
    )
    if selection.abstention is not None:
        return BasketDecision(
            as_of=as_of,
            held=held_now,
            entered=frozenset(),
            exited=frozenset(),
            scores=scores,
            abstention=selection.abstention,
        )

    return BasketDecision(
        as_of=as_of,
        held=selection.held,
        entered=selection.held - held_now,
        exited=held_now - selection.held,
        scores=scores,
        abstention=None,
    )
