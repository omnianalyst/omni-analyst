"""Smart-money producer: labelled fund/treasury accumulation -> a directional call.

``onchain_flow`` claims (from ``ingest/onchain.py``) and the ``address_label``
store (from ``ingest/labels.py``) are now both in place, but nothing reads them
together. This producer does: it reads the trailing on-chain flows visible to
the audience, attributes them through the label store, and asserts "labelled
smart money is accumulating (or distributing), so price follows" -- a genuine,
falsifiable directional assertion the triple-barrier schema can score.

The unit of evidence is a **LABELLED wallet's** net position change. Labels
come only from ``ingest/labels.py``: an unlabelled address stays unlabelled,
and a label is never inferred from transaction shape, volume or clustering (a
heuristic label presented as a known one is fabricated provenance, and every
signal built on it would inherit the fabrication). Only wallets labelled
``fund`` or ``treasury`` count -- exchanges (the ``exchange_*`` flows the
adapter emits) are the opposite of smart money here, and other categories are
out of scope. One wallet is an anecdote: the call ships only when at least
``min_wallets`` distinct labelled wallets agree with the net direction, and
``confidence`` scales with how many agree.

The barriers are model-grounded, not invented (mirroring ``trend.py`` /
``carry.py``):

- **Direction** is ``up`` when labelled wallets are net accumulating (receiving
  more than they send), ``down`` when net distributing. The sign of the
  aggregate net position change across all agreeing labelled wallets.
- **The invalidation barrier** is the price extreme of the window the model
  examines: the window's lowest close for an accumulation (``up``) call, the
  highest close for a distribution (``down``) call. Smart money built its
  position over that window; the extreme is the level at which that positioning
  was established. Price returning to it means the market rejected the level
  the cohort transacted at -- the accumulation that "preceded a move" preceded
  no sustained move. It is the structural stop of the positioning regime, read
  from the same price window the model reads, not a fixed percentage or round
  number. It is the smart-money analogue of ``trend.py``'s "the MA is the
  invalidation": a level the model's window identifies, crossing which
  falsifies the premise.
- **The target barrier** is a volatility-scaled move in the trade's direction,
  exactly as ``trend.py`` / ``carry.py`` size their target with realized vol.

Confidence is NOT the driftless first-passage geometry used by trend/carry. The
work order mandates that conviction here scale with breadth of agreement:
``confidence = n_agree / (n_active + 1)``, where ``n_agree`` is the number of
distinct labelled wallets whose net stance matches the direction and
``n_active`` is the number taking any stance. It rises as more wallets agree
and falls when dissenting labelled wallets are present; the ``+1`` keeps a
single on-chain signal from ever claiming certainty. Calibration -- not the
writer -- decides whether that number is worth interrupting anyone for.

Abstention is honest, not failure: no labelled fund/treasury wallet active in
the window, a net position change that cancels to ~zero (contradictory flows),
fewer than ``min_wallets`` agreeing wallets (an anecdote), a non-finite flow
amount, zero/non-finite realized vol, no price to anchor entry, or entry itself
being the window extreme (no structural stop exists yet -- calling a reversal
with no held level is not grounded) -- each returns ``None`` rather than a
manufactured call.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from uuid import UUID

import numpy as np

from omni.conviction.ledger import record_prediction
from omni.conviction.trend import _realized_vol
from omni.coverage.visibility import visible_claims_cte
from omni.ingest.labels import (
    CATEGORY_FUND,
    CATEGORY_TREASURY,
    lookup_many,
    normalise_address,
)

_CAPABILITY = "onchain.smart_money"
METHOD = "onchain.smart_money"

DEFAULT_WINDOW = 50
DEFAULT_PRICE_WINDOW = 20
DEFAULT_MIN_WALLETS = 3
DEFAULT_TARGET_K = 2.0

# One idiom (abs(x) <= atol, never ==), two scale-consistent tolerances. Vol is
# a price-space quantity (stdev x close, matching trend.py / carry.py). The net
# signal is checked as a FRACTION of gross labelled flow: total_net/gross is a
# pure ratio in [-1, 1] regardless of how large the flows are, so a single
# dimensionless tolerance is scale-consistent across entities and periods. It
# fires only on true cancellation or float dust -- real whale flows (the adapter
# floors non-exchange flows at >= whale_min_eth) are orders of magnitude above.
_ZERO_VOL_ATOL = 1e-9
_NET_FRAC_ATOL = 1e-9

_SMART_CATEGORIES = frozenset({CATEGORY_FUND, CATEGORY_TREASURY})


async def _smart_labels(
    pool, addresses_by_chain: dict[str, set[str]]
) -> dict[tuple[str, str], str]:
    """The category of each supplied address that carries a smart-money label.

    Goes through ``ingest/labels.py::lookup_many`` -- labels are sourced there
    and only there. Returns ``{(chain, normalised_address): category}`` for
    addresses whose winning (highest-confidence) label is ``fund`` or
    ``treasury``. An address with any other winning label (exchange, bridge,
    miner, protocol) or no label at all is absent: it is NOT smart money here,
    and its absence is the point -- a label is never inferred from transaction
    shape. A winner that is not ``fund``/``treasury`` excludes the address even
    if a lower-confidence ``fund`` row exists, so an address the operator
    reclassified cannot sneak in on a stale label.
    """
    out: dict[tuple[str, str], str] = {}
    for chain, addrs in addresses_by_chain.items():
        if not addrs:
            continue
        found = await lookup_many(pool, chain, sorted(addrs))
        for addr, lbl in found.items():
            if lbl.category in _SMART_CATEGORIES:
                out[(chain, addr)] = lbl.category
    return out


async def _flow_window(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime, limit: int
) -> list[dict]:
    """The trailing ``limit`` on-chain flows visible to the audience as-of
    ``as_of``, oldest-first.

    Point-in-time: a flow filed after ``as_of`` is invisible. A non-finite
    ``amount_eth`` (``NaN``/``inf``) is KEPT -- it must poison the net check
    into abstaining rather than be silently dropped, which is the exact failure
    mode AGENTS.md's float section names.
    """
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        )
        SELECT c.value
        FROM visible c
        WHERE c.entity_id = $1
          AND c.claim_type = 'onchain_flow'
          AND c.knowledge_date <= $2
        ORDER BY c.event_date DESC
        LIMIT $4
        """,
        entity_id,
        as_of,
        audience,
        limit,
    )
    flows: list[dict] = []
    for r in rows:
        raw = r["value"]
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            continue
        amt = raw.get("amount_eth")
        if amt is None:
            continue
        try:
            amount = float(amt)
        except (TypeError, ValueError):
            continue
        flows.append(
            {
                "amount": amount,
                "from": raw.get("from") or "",
                "to": raw.get("to") or "",
                "chain": raw.get("chain") or "eth",
            }
        )
    flows.reverse()  # oldest-first
    return flows


async def _price_window(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime, limit: int
) -> list[float]:
    """The trailing daily closes knowable as-of ``as_of``, oldest-first.

    Mirrors ``trend._price_window`` / ``carry._price_window``; CoinGecko
    snapshots carry ``price``, Polygon bars carry ``close``.
    """
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        )
        SELECT c.value
        FROM visible c
        WHERE c.entity_id = $1
          AND c.claim_type = 'price_snapshot'
          AND c.knowledge_date <= $2
        ORDER BY c.event_date DESC
        LIMIT $4
        """,
        entity_id,
        as_of,
        audience,
        limit,
    )
    closes: list[float] = []
    for r in rows:
        raw = r["value"]
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            continue
        scalar = raw.get("close")
        if scalar is None:
            scalar = raw.get("price")
        if scalar is None:
            continue
        try:
            closes.append(float(scalar))
        except (TypeError, ValueError):
            continue
    closes.reverse()  # oldest-first
    return closes


def smart_money_call(
    *,
    entry: float,
    vol: float,
    invalidation_level: float,
    direction: str,
    n_agree: int,
    n_active: int,
    target_k: float = DEFAULT_TARGET_K,
) -> tuple[str, float, float, float] | None:
    """A falsifiable smart-money call, or ``None`` when none is honest.

    Returns ``(direction, upper, lower, confidence)`` with barriers that
    genuinely straddle entry. ``direction`` is ``up`` when labelled wallets net
    accumulated, ``down`` when they net distributed.

    The **invalidation** barrier is the caller-supplied model level -- the
    accumulation window's price extreme (lowest close for an up call, highest
    for a down call): the level at which the cohort's positioning was
    established, crossing which means the move it preceded was rejected. The
    **target** is a vol-scaled move in the trade's direction. Confidence is the
    breadth-of-agreement fraction ``n_agree / (n_active + 1)`` -- it rises with
    the number of agreeing wallets and falls with dissent, never reaching 1.0.

    ``None`` when: realized vol is non-positive/non-finite (no honest target),
    the invalidation level is non-finite, the constructed barriers fail the
    straddle ``upper > entry > lower`` (entry is the window extreme -- no
    structural stop exists yet), or no agreeing wallet is on record.
    """
    if not math.isfinite(vol) or vol <= _ZERO_VOL_ATOL:
        return None
    if not math.isfinite(invalidation_level):
        return None
    if direction == "up":
        upper = entry + target_k * vol
        lower = invalidation_level
    else:
        upper = invalidation_level
        lower = entry - target_k * vol

    if not (upper > entry > lower):
        return None
    if n_active <= 0 or n_agree <= 0:
        return None
    confidence = n_agree / (n_active + 1)
    if not (0.0 < confidence < 1.0):
        return None
    return direction, upper, lower, confidence


def _net_stance(flows: list[dict], labels: dict[tuple[str, str], str]):
    """Reduce attributed flows to an aggregate net stance, or ``None`` to abstain.

    Each flow moves the asset between two addresses; a ``fund``/``treasury``
    wallet receiving (``to``) accumulates, one sending (``from``) distributes.
    Returns ``(direction, n_agree, n_active, total_net, gross)`` or ``None``
    when there is no labelled smart-money activity or the net cancels to ~zero.
    The ``min_wallets`` floor is the caller's -- this returns the raw counts and
    trusts the caller to abstain on too few agreeing wallets.

    Scale-consistent float handling: the net is judged as ``total_net/gross``
    (a ratio in [-1, 1] independent of flow magnitude), never by comparing the
    raw ETH total to zero. A per-wallet stance is counted only when its net
    exceeds the same fraction of gross, so a wallet that round-trips to dust is
    neither agreeing nor dissenting. Non-finite amounts poison the aggregate.
    """
    nets: dict[tuple[str, str], float] = {}
    for f in flows:
        chain = f["chain"]
        amount = f["amount"]
        for raw_addr, sign in ((f["from"], -1.0), (f["to"], 1.0)):
            if not raw_addr:
                continue
            key = (chain, normalise_address(chain, raw_addr))
            if key not in labels:
                continue
            nets[key] = nets.get(key, 0.0) + sign * amount

    if not nets:
        return None
    amounts = np.array(list(nets.values()), dtype=float)
    if not np.all(np.isfinite(amounts)):
        return None
    gross = float(np.sum(np.abs(amounts)))
    total_net = float(np.sum(amounts))
    # The net is judged as a FRACTION of gross (a ratio in [-1, 1], independent
    # of flow magnitude) -- never by comparing the raw ETH total to zero. This
    # is the scale-consistent idiom: a single tolerance serves both the
    # cancellation check below and the per-wallet stance threshold. gross is
    # only ever multiplied in (never divided), so an all-zero window (gross
    # exactly 0.0) yields threshold 0.0 and abstains here; a dust flow is left
    # for the min_wallets floor to reject.
    threshold = _NET_FRAC_ATOL * gross
    if abs(total_net) <= threshold:
        return None

    direction = "up" if total_net > 0.0 else "down"
    n_active = 0
    n_agree = 0
    for a in amounts:
        if abs(float(a)) <= threshold:
            continue
        n_active += 1
        if (float(a) > 0.0) == (total_net > 0.0):
            n_agree += 1
    return direction, n_agree, n_active, total_net, gross


async def produce_smart_money_prediction_from_coverage(
    pool,
    *,
    entity_id: UUID,
    audience_user_id: UUID | None,
    horizon_ends_at: datetime,
    method: str = METHOD,
    as_of: datetime | None = None,
    created_at: datetime | None = None,
    window: int = DEFAULT_WINDOW,
    price_window: int = DEFAULT_PRICE_WINDOW,
    target_k: float = DEFAULT_TARGET_K,
    min_wallets: int = DEFAULT_MIN_WALLETS,
) -> UUID | None:
    """Produce a smart-money directional prediction from coverage.

    Reads the trailing ``window`` on-chain flows and ``price_window`` daily
    closes visible to the audience as-of ``as_of`` (default now), attributes the
    flows through the ``address_label`` store (``fund``/``treasury`` only --
    labels are sourced, never inferred), and records a directional call whose
    invalidation is the accumulation window's price extreme and whose target is
    vol-scaled. Returns the new prediction id, or ``None`` when coverage is
    insufficient or there is no honest smart-money signal: no labelled wallet
    active, net flows cancelling to ~zero, fewer than ``min_wallets`` wallets
    agreeing (an anecdote), a non-finite amount, zero/non-finite vol, no price
    to anchor entry, or entry being the window extreme. Abstention is the
    honest outcome, never a manufactured call.

    Reads only through ``coverage/visibility.py`` scoped to the audience --
    never the ``claim`` table directly -- which is the licence boundary.
    """
    if as_of is None:
        as_of = datetime.now(UTC)

    flows = await _flow_window(
        pool,
        entity_id=entity_id,
        audience=audience_user_id,
        as_of=as_of,
        limit=window,
    )
    if not flows:
        return None

    addresses_by_chain: dict[str, set[str]] = {}
    for f in flows:
        chain = f["chain"]
        for raw_addr in (f["from"], f["to"]):
            if raw_addr:
                addresses_by_chain.setdefault(chain, set()).add(
                    normalise_address(chain, raw_addr)
                )
    labels = await _smart_labels(pool, addresses_by_chain)
    if not labels:
        return None

    stance = _net_stance(flows, labels)
    if stance is None:
        return None
    direction, n_agree, n_active, total_net, gross = stance
    if n_agree < min_wallets:
        return None

    closes = await _price_window(
        pool,
        entity_id=entity_id,
        audience=audience_user_id,
        as_of=as_of,
        limit=price_window,
    )
    if not closes:
        return None
    entry = closes[-1]
    vol = _realized_vol(closes)
    window_low = min(closes)
    window_high = max(closes)
    invalidation_level = window_low if direction == "up" else window_high

    call = smart_money_call(
        entry=entry,
        vol=vol,
        invalidation_level=invalidation_level,
        direction=direction,
        n_agree=n_agree,
        n_active=n_active,
        target_k=target_k,
    )
    if call is None:
        return None
    _, upper, lower, confidence = call

    return await record_prediction(
        pool,
        entity_id=entity_id,
        capability=_CAPABILITY,
        method=method,
        direction=direction,
        confidence=confidence,
        entry_price=entry,
        upper_barrier=upper,
        lower_barrier=lower,
        horizon_ends_at=horizon_ends_at,
        audience_user_id=audience_user_id,
        created_at=created_at,
        assumptions={
            "model": "smart_money_accumulation",
            "window": window,
            "price_window": price_window,
            "target_k": target_k,
            "min_wallets": min_wallets,
            "total_net": total_net,
            "gross_flow": gross,
            "n_active_wallets": n_active,
            "n_agree_wallets": n_agree,
            "window_low": window_low,
            "window_high": window_high,
            "realized_vol": vol,
            "entry": entry,
            "confidence_model": "wallet_agreement_fraction",
            "labelled_categories": sorted(_SMART_CATEGORIES),
        },
    )
