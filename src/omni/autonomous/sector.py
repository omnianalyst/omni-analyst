"""Phase C: the sector scanner loop.

The macro regime assessment (Phase B) tells the system WHERE in the cycle it is.
This loop tells it WHICH sectors are moving. For each of the 11 GICS sector ETFs
it reads the trailing price history, computes a relative-strength percentile
(how this sector ranks against its peers), a trend label (MA crossover on
returns), and a macro_alignment verdict (does the current regime historically
favor this sector?). The three compose into a ``sector_score`` claim.

The macro_alignment link is the deduction chain's load-bearing edge -- the
reasoning that connects "expansion + risk_on" to "tech should lead". It starts
with the Stovall sector-rotation mapping (AUTONOMOUS_PLAN.md Gap 7): each cycle
phase has a set of sectors that historically outperform in that phase. A sector
scores "favorable" when it is in the current phase's set, "unfavorable" when it
is not. This is a literature-based prior, not a measured edge; Phase F's meta-
calibration replaces it with the system's own hit rate as soon as enough
history accrues.

A sector with no price coverage abstains (no sector_score written). A system
with no regime assessment scores trend + RS but marks macro_alignment "unknown".
Silence is the honest outcome in both cases.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from omni.autonomous.reading import (
    latest_shared_claim,
    price_closes,
    to_returns,
)
from omni.capabilities.regime import classify_trend
from omni.entities._seed_data import SECTOR_ETFS
from omni.ingest.protocol import ClaimDraft, Unavailable
from omni.perception.divergence import write_derived

logger = logging.getLogger("omni.autonomous.sector")

SOURCE = "omni.autonomous"
CLAIM_TYPE = "sector_score"
_TREND_SHORT = 20
_TREND_LONG = 60
_RS_WINDOW = 60

# Stovall sector-rotation mapping (AUTONOMOUS_PLAN.md Gap 7). Each cycle phase
# has a set of GICS sector ETFs that historically outperform. This is a
# literature-based prior -- the standard framework Murphy's Intermarket Analysis
# and Stovall's guide describe -- not a measured edge. Phase F's meta-calibration
# refines it with the system's own observed hit rate once history accrues.
_FAVORABLE_SECTORS: dict[str, frozenset[str]] = {
    "expansion": frozenset({"XLK", "XLC", "XLY", "XLI", "XLV"}),
    "peak": frozenset({"XLE", "XLB", "XLRE", "XLF"}),
    "contraction": frozenset({"XLP", "XLU", "XLV", "XLE"}),
}


@dataclass(frozen=True)
class SectorScanReport:
    scored: int = 0
    abstained: int = 0
    skipped_unchanged: int = 0
    details: tuple[str, ...] = ()


def macro_alignment(cycle_phase: str | None, etf_symbol: str) -> str:
    """Whether the current cycle phase historically favors this sector.

    ``favorable`` when the ETF is in the phase's Stovall set, ``unfavorable``
    when it is not, ``unknown`` when there is no regime assessment to read.
    """
    if cycle_phase is None:
        return "unknown"
    favorable = _FAVORABLE_SECTORS.get(cycle_phase, frozenset())
    return "favorable" if etf_symbol in favorable else "unfavorable"


def _rs_percentile(returns_by_symbol: dict[str, float], symbol: str) -> float:
    """The percentile of ``symbol``'s return in the cross-section, 0..1.

    A sector at the 85th percentile outperformed 85% of sectors. The
    distribution is the observed returns, not a fitted curve -- with 11
    sectors the resolution is ~9 percentage points, which is honest for the
    sample size. Ties share the higher rank.
    """
    if not returns_by_symbol:
        return 0.0
    target = returns_by_symbol[symbol]
    n_below = sum(1 for r in returns_by_symbol.values() if r < target)
    return n_below / max(len(returns_by_symbol) - 1, 1)


def _trailing_return(closes: list[float]) -> float | None:
    """The total return over the trailing window, or None if too short."""
    if len(closes) < 2:
        return None
    start = closes[max(0, len(closes) - _RS_WINDOW)]
    if start == 0:
        return None
    return closes[-1] / start - 1.0


async def scan_sectors(
    pool, *, as_of: datetime | None = None, operator_user_id=None
) -> SectorScanReport:
    """Score every sector ETF, writing ``sector_score`` claims.

    For each ETF: reads trailing prices, computes trend (MA crossover on
    returns) and relative strength (cross-sectional percentile). Reads the
    latest regime assessment for macro_alignment. Writes a sector_score claim
    if prices are sufficient; abstains per-sector if they are not.

    Sectors are ranked against each other in a single cross-section, so the
    loop makes two passes: the first computes each sector's trailing return,
    the second writes the scores. A sector with fewer than ``_TREND_LONG``
    closes abstains (classify_trend's floor).
    """
    regime = await latest_shared_claim(pool, claim_type="regime_assessment", as_of=as_of)
    cycle_phase = None
    regime_claim_id = None
    if regime is not None:
        regime_value = regime["value"]
        cycle_phase = regime_value.get("cycle_phase")
        regime_claim_id = UUID(str(regime["id"]))

    etf_ids: dict[str, UUID] = {
        row["symbol"]: row["id"]
        for row in await pool.fetch(
            "SELECT id, symbol FROM entity WHERE kind = 'sector_etf'"
        )
    }

    # Pass 1: gather closes + returns for every sector with enough data.
    closes_by_symbol: dict[str, list[float]] = {}
    returns_by_symbol: dict[str, float] = {}
    price_meta: dict[str, tuple[UUID, datetime, datetime]] = {}
    for symbol, _, _ in SECTOR_ETFS:
        entity_id = etf_ids.get(symbol)
        if entity_id is None:
            continue
        closes = await price_closes(pool, entity_id=entity_id, limit=_TREND_LONG + 5)
        if len(closes) < _TREND_LONG:
            continue
        closes_by_symbol[symbol] = closes
        ret = _trailing_return(closes)
        if ret is not None:
            returns_by_symbol[symbol] = ret
        # Track the latest price claim's id + dates as provenance input and as
        # the sector_score's bitemporal dates. Data-driven dates (not clock-
        # driven) make the claim idempotent: same prices -> same event_date ->
        # the existing claim is found and skipped on the next scan.
        latest = await pool.fetchrow(
            "SELECT id, event_date, knowledge_date FROM claim "
            "WHERE entity_id = $1 AND claim_type = 'price_snapshot' "
            "AND superseded_by IS NULL "
            "ORDER BY event_date DESC LIMIT 1",
            entity_id,
        )
        if latest is not None:
            price_meta[symbol] = (
                UUID(str(latest["id"])),
                latest["event_date"],
                latest["knowledge_date"],
            )

    if not returns_by_symbol:
        logger.info("sector scan abstained: no sector has sufficient price data")
        return SectorScanReport()

    # Pass 2: score each sector with enough data.
    scored = 0
    abstained = 0
    skipped_unchanged = 0
    details: list[str] = []
    for symbol, name, _ in SECTOR_ETFS:
        closes = closes_by_symbol.get(symbol)
        if closes is None or len(closes) < _TREND_LONG:
            abstained += 1
            continue
        entity_id = etf_ids[symbol]
        meta = price_meta.get(symbol)
        if meta is None:
            abstained += 1
            continue
        price_cid, event_date, knowledge_date = meta

        returns = to_returns(closes)
        try:
            trend_result = classify_trend(returns, _TREND_SHORT, _TREND_LONG)
        except (Unavailable, ValueError):
            abstained += 1
            continue

        ret = returns_by_symbol.get(symbol, 0.0)
        rs = _rs_percentile(returns_by_symbol, symbol)
        alignment = macro_alignment(cycle_phase, symbol)

        # Idempotency: if a score with the same data-driven dates already
        # exists (prices unchanged since last scan), skip. This prevents the
        # 11-claims-per-run accumulation that would flood meta-calibration.
        existing = await pool.fetchval(
            "SELECT id FROM claim "
            "WHERE entity_id = $1 AND claim_type = 'sector_score' "
            "AND key = $2 AND source = $3 "
            "AND event_date = $4 AND knowledge_date = $5 "
            "AND audience_user_id IS NULL",
            entity_id, symbol.lower(), SOURCE, event_date, knowledge_date,
        )
        if existing is not None:
            skipped_unchanged += 1
            continue

        value = {
            "rs_percentile": round(rs, 4),
            "trend": trend_result["regime"],
            "macro_alignment": alignment,
            "cycle_phase": cycle_phase,
            "return_window": round(ret, 6),
            "etf_symbol": symbol,
        }

        draft = ClaimDraft(
            claim_type=CLAIM_TYPE,
            key=symbol.lower(),
            value=value,
            event_date=event_date,
            knowledge_date=knowledge_date,
            confidence=1.0,
            evidence={
                "ma_short": trend_result["ma_short"],
                "ma_long": trend_result["ma_long"],
                "regime_claim_id": str(regime_claim_id) if regime_claim_id else None,
            },
        )

        input_ids: list[UUID] = [price_cid]
        if regime_claim_id is not None:
            input_ids.append(regime_claim_id)

        # Resolve licence from inputs: Polygon prices are byo_only, so a
        # sector_score derived from them must also be byo_only under the
        # operator's audience (migration 002 licence propagation). A
        # sector_score with no price inputs (only regime) stays allowed.
        price_licence = await pool.fetchval(
            "SELECT redistributable::text FROM claim WHERE id = $1", price_cid)
        if price_licence == "byo_only":
            redistributable = "byo_only"
            audience = operator_user_id
        else:
            redistributable = "allowed"
            audience = None

        await write_derived(
            pool,
            draft,
            entity_id=entity_id,
            input_claim_ids=input_ids,
            audience_user_id=audience,
            redistributable=redistributable,
            source=SOURCE,
        )
        scored += 1
        details.append(f"{symbol}:{trend_result['regime']}:{alignment}")

    logger.info(
        "sector scan: %d scored, %d abstained, %d unchanged (%s)",
        scored, abstained, skipped_unchanged,
        ", ".join(details) if details else "none",
    )
    return SectorScanReport(
        scored=scored, abstained=abstained,
        skipped_unchanged=skipped_unchanged, details=tuple(details),
    )
