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
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from omni.autonomous.reading import (
    ClaimProvenance,
    latest_shared_claim,
    sector_price_histories,
    to_returns,
)
from omni.capabilities.regime import classify_trend
from omni.entities._seed_data import SECTOR_ETFS
from omni.ingest.protocol import ClaimDraft, Unavailable
from omni.perception.divergence import resolve_derived_licence, write_derived

logger = logging.getLogger("omni.autonomous.sector")

SOURCE = "omni.autonomous"
CLAIM_TYPE = "sector_score"
_TREND_SHORT = 20
_TREND_LONG = 60
_RS_WINDOW = 60
_MIN_CLOSES = _TREND_LONG + 1

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
    end = closes[-1]
    if not math.isfinite(start) or not math.isfinite(end) or start <= 0.0 or end <= 0.0:
        return None
    return end / start - 1.0


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
    the second writes the scores. A sector needs ``_TREND_LONG + 1`` closes
    because the return series is one observation shorter.
    """
    scan_boundary = as_of or datetime.now(UTC)
    regime = await latest_shared_claim(
        pool, claim_type="regime_assessment", as_of=scan_boundary
    )
    cycle_phase = None
    regime_input = None
    if regime is not None:
        regime_value = regime["value"]
        cycle_phase = regime_value.get("cycle_phase")
        regime_input = ClaimProvenance(
            id=UUID(str(regime["id"])),
            entity_id=UUID(str(regime["entity_id"])),
            value=regime_value,
            event_date=regime["event_date"],
            knowledge_date=regime["knowledge_date"],
            redistributable=regime["redistributable"],
            audience_user_id=regime["audience_user_id"],
        )

    etf_ids: dict[str, UUID] = {
        row["symbol"]: row["id"]
        for row in await pool.fetch(
            "SELECT id, symbol FROM entity WHERE kind = 'sector_etf'"
        )
    }

    histories_by_entity = await sector_price_histories(
        pool,
        entity_ids=list(etf_ids.values()),
        audience_user_id=operator_user_id,
        as_of=scan_boundary,
        limit=_TREND_LONG + 5,
    )

    # Pass 1: gather closes + returns for every sector with enough data.
    closes_by_symbol: dict[str, list[float]] = {}
    returns_by_symbol: dict[str, float] = {}
    price_inputs_by_symbol: dict[str, list[ClaimProvenance]] = {}
    for symbol, _, _ in SECTOR_ETFS:
        entity_id = etf_ids.get(symbol)
        if entity_id is None:
            continue
        price_inputs = histories_by_entity.get(entity_id, [])
        if len(price_inputs) < _MIN_CLOSES:
            continue
        closes = [claim.value for claim in price_inputs]
        ret = _trailing_return(closes)
        if ret is None:
            continue
        closes_by_symbol[symbol] = closes
        returns_by_symbol[symbol] = ret
        price_inputs_by_symbol[symbol] = price_inputs

    if not returns_by_symbol:
        logger.info("sector scan abstained: no sector has sufficient price data")
        return SectorScanReport()

    material_inputs = [
        claim
        for symbol in returns_by_symbol
        for claim in price_inputs_by_symbol[symbol]
    ]
    if regime_input is not None:
        material_inputs.append(regime_input)
    input_ids = [claim.id for claim in material_inputs]
    event_date = max(claim.event_date for claim in material_inputs)
    knowledge_date = max(claim.knowledge_date for claim in material_inputs)
    redistributable, audience = resolve_derived_licence(material_inputs)

    # Pass 2: score each sector with enough data.
    scored = 0
    abstained = 0
    skipped_unchanged = 0
    details: list[str] = []
    for symbol, name, _ in SECTOR_ETFS:
        closes = closes_by_symbol.get(symbol)
        if closes is None or len(closes) < _MIN_CLOSES:
            abstained += 1
            continue
        entity_id = etf_ids[symbol]

        returns = to_returns(closes)
        try:
            trend_result = classify_trend(returns, _TREND_SHORT, _TREND_LONG)
        except (Unavailable, ValueError):
            abstained += 1
            continue

        ret = returns_by_symbol[symbol]
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
            "AND audience_user_id IS NOT DISTINCT FROM $6::uuid",
            entity_id, symbol.lower(), SOURCE, event_date, knowledge_date, audience,
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
                "regime_claim_id": str(regime_input.id) if regime_input else None,
            },
        )

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
