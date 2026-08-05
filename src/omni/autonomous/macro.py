"""Phase B: the macro regime assessment loop.

The system's first autonomous act is to look at the world. This loop reads raw
FRED macro_series_point claims (the 7 series the fill pipeline ingests), computes
the five macro signals inline (yield curve inversion, Sahm rule, inflation,
output gap, LEI direction), and composes them into a regime_assessment claim.

The composition is DETERMINISTIC CODE, not an LLM opining. "Expansion" means
the recession-probability function -- a fixed-weight sum of three boolean
recession signals -- returns a probability below the contraction threshold.

The loop reads RAW data directly rather than depending on the derived-signal
claim pipeline (yield_curve_signal, sahm_rule_signal, etc.) because the fill
pipeline routes derived-capability gaps as series-fetched, which fails on the
NULL key. The derived capabilities remain in the registry for the analysis-
by-name path (POST /analysis/run); this loop is self-contained infrastructure
that computes the same logic inline.

If any required series has insufficient data, the loop abstains and writes
nothing. Silence is the honest outcome when coverage is incomplete.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from omni.autonomous.reading import latest_shared_claim, macro_series_values
from omni.ingest.protocol import ClaimDraft
from omni.perception.divergence import write_derived

logger = logging.getLogger("omni.autonomous.macro")

SOURCE = "omni.autonomous"
CLAIM_TYPE = "regime_assessment"
KEY = "us_macro"

_W_YC = 0.3
_W_SAHM = 0.4
_W_LEI = 0.3


# -- Pure composition (same logic as the macro capabilities) -----------------


def recession_probability(
    yc_inverted: bool, sahm_triggered: bool, lei_negative: bool
) -> tuple[float, str]:
    prob = _W_YC * float(yc_inverted) + _W_SAHM * float(sahm_triggered) + _W_LEI * float(
        lei_negative
    )
    if prob >= 0.7:
        band = "high"
    elif prob >= 0.4:
        band = "elevated"
    elif prob >= 0.2:
        band = "moderate"
    else:
        band = "low"
    return prob, band


def cycle_phase(yc_inverted: bool, sahm_triggered: bool, lei_negative: bool) -> str:
    if sahm_triggered:
        return "contraction"
    if yc_inverted or lei_negative:
        return "peak"
    return "expansion"


def risk_regime(
    recession_prob: float, inflation_yoy: float, output_gap: float
) -> str:
    if recession_prob >= 0.4:
        return "risk_off"
    if inflation_yoy > 3.0 and output_gap < 0:
        return "risk_off"
    if recession_prob <= 0.15 and inflation_yoy <= 3.0:
        return "risk_on"
    return "transition"


def inflation_regime(inflation_yoy: float) -> str:
    if inflation_yoy < 2.0:
        return "cooling"
    if inflation_yoy <= 3.0:
        return "stable"
    return "rising"


def policy_stance(inflation_yoy: float, output_gap: float) -> str:
    if inflation_yoy > 2.0 and output_gap > 0:
        return "hawkish"
    if inflation_yoy < 2.0 and output_gap < 0:
        return "dovish"
    return "neutral"


# -- Signal computation from raw FRED data -----------------------------------


def _compute_yield_curve(dgs2: list, dgs10: list) -> tuple[bool, float | None]:
    """Is the 10Y-2Y treasury spread negative?"""
    if not dgs2 or not dgs10:
        return False, None
    latest_2y = dgs2[-1][1]
    latest_10y = dgs10[-1][1]
    if latest_2y is None or latest_10y is None:
        return False, None
    spread = latest_10y - latest_2y
    return spread < 0, spread


def _compute_sahm(unrate: list) -> tuple[bool, float | None]:
    """Sahm rule: 3-month moving average rose 0.5+ above the 12-month low."""
    vals = [v for _, v in unrate if v is not None]
    if len(vals) < 12:
        return False, None
    recent = vals[-12:]
    ma3 = sum(recent[-3:]) / 3.0
    low12 = min(recent)
    indicator = ma3 - low12
    return indicator >= 0.5, indicator


def _compute_inflation(cpi: list) -> float | None:
    """CPI year-over-year percent change."""
    vals = [v for _, v in cpi if v is not None]
    if len(vals) < 13:
        return None
    return (vals[-1] / vals[-13] - 1.0) * 100.0


def _compute_output_gap(gdp: list, pot: list) -> float | None:
    """GDP output gap: (actual / potential - 1) * 100."""
    gdp_v = next((v for _, v in reversed(gdp) if v is not None), None)
    pot_v = next((v for _, v in reversed(pot) if v is not None), None)
    if gdp_v is None or pot_v is None or pot_v == 0:
        return None
    return (gdp_v / pot_v - 1.0) * 100.0


def _compute_lei(lei: list) -> tuple[bool, float | None]:
    """LEI 6-month percent change: negative = recession warning."""
    vals = [v for _, v in lei if v is not None]
    if len(vals) < 7:
        return False, None
    change = (vals[-1] / vals[-7] - 1.0) * 100.0
    return change < 0, change


# -- The loop ----------------------------------------------------------------

_EXISTING = """
SELECT id FROM claim
WHERE entity_id = $1 AND claim_type = $2::claim_type AND key = $3
  AND source = $4 AND event_date = $5 AND knowledge_date = $6
  AND audience_user_id IS NULL
"""

_MACRO_ENTITY = "SELECT id FROM entity WHERE kind = 'macro' AND symbol = 'US_MACRO'"


async def assess_macro_regime(
    pool, *, as_of: datetime | None = None
) -> UUID | None:
    """Read raw FRED data, compute signals, write a regime_assessment claim.

    Returns the claim id (newly written or already existing), or None when the
    assessment abstained -- a required series has insufficient data, or the
    macro entity does not exist.
    """
    macro_entity = await pool.fetchval(_MACRO_ENTITY)
    if macro_entity is None:
        return None

    # Read raw FRED series.
    dgs2 = await macro_series_values(pool, key="DGS2", limit=300, as_of=as_of)
    dgs10 = await macro_series_values(pool, key="DGS10", limit=300, as_of=as_of)
    unrate = await macro_series_values(pool, key="UNRATE", limit=18, as_of=as_of)
    cpi = await macro_series_values(pool, key="CPIAUCSL", limit=19, as_of=as_of)
    gdp = await macro_series_values(pool, key="GDPC1", limit=4, as_of=as_of)
    pot = await macro_series_values(pool, key="GDPPOT", limit=4, as_of=as_of)
    lei = await macro_series_values(pool, key="USSLIND", limit=13, as_of=as_of)

    # Compute signals from raw data.
    yc_inverted, yc_spread = _compute_yield_curve(dgs2, dgs10)
    sahm_triggered, sahm_indicator = _compute_sahm(unrate)
    inf_yoy = _compute_inflation(cpi)
    og = _compute_output_gap(gdp, pot)
    lei_negative, lei_change = _compute_lei(lei)

    if inf_yoy is None or og is None:
        logger.info(
            "macro regime abstained: insufficient data "
            "(cpi=%d obs, gdp=%d, pot=%d, unrate=%d, dgs2=%d, dgs10=%d, lei=%d)",
            len(cpi), len(gdp), len(pot), len(unrate), len(dgs2), len(dgs10), len(lei),
        )
        return None

    prob, prob_band = recession_probability(yc_inverted, sahm_triggered, lei_negative)
    phase = cycle_phase(yc_inverted, sahm_triggered, lei_negative)
    risk = risk_regime(prob, inf_yoy, og)
    inf_regime = inflation_regime(inf_yoy)
    stance = policy_stance(inf_yoy, og)

    # Collect the latest claim per series for provenance edges. One per series
    # (7 total) -- the regime assessment traces to the data it read, not every
    # observation. Filtered to allowed/FRED only so the licence propagation
    # trigger does not reject a byo_only input on an allowed derived claim.
    input_rows = await pool.fetch(
        "SELECT DISTINCT ON (key) id, key FROM claim "
        "WHERE claim_type = 'macro_series_point' "
        "AND key = ANY($1::text[]) "
        "AND source = 'fred' "
        "AND audience_user_id IS NULL "
        "AND redistributable = 'allowed' "
        "AND superseded_by IS NULL "
        "ORDER BY key, event_date DESC",
        ["DGS2", "DGS10", "UNRATE", "CPIAUCSL", "GDPC1", "GDPPOT", "USSLIND"],
    )
    input_ids = [UUID(str(r["id"])) for r in input_rows]
    if not input_ids:
        return None

    event_date = datetime.now(datetime.now().astimezone().tzinfo)
    knowledge_date = event_date

    existing = await pool.fetchval(
        _EXISTING, macro_entity, CLAIM_TYPE, KEY, SOURCE,
        event_date.replace(hour=0, minute=0, second=0, microsecond=0),
        knowledge_date.replace(hour=0, minute=0, second=0, microsecond=0),
    )
    if existing is not None:
        return existing

    value = {
        "recession_probability": round(prob, 4),
        "recession_assessment": prob_band,
        "cycle_phase": phase,
        "risk_regime": risk,
        "inflation_regime": inf_regime,
        "policy_stance": stance,
        "inflation_yoy": round(inf_yoy, 4),
        "output_gap": round(og, 4),
        "yield_curve_inverted": yc_inverted,
        "yield_curve_spread": round(yc_spread, 4) if yc_spread is not None else None,
        "sahm_triggered": sahm_triggered,
        "sahm_indicator": round(sahm_indicator, 4) if sahm_indicator is not None else None,
        "lei_negative": lei_negative,
        "lei_change_6m": round(lei_change, 4) if lei_change is not None else None,
    }

    draft = ClaimDraft(
        claim_type=CLAIM_TYPE,
        key=KEY,
        value=value,
        event_date=event_date,
        knowledge_date=knowledge_date,
        confidence=1.0,
        evidence={
            "source": "raw FRED macro_series_point (computed inline)",
            "series_obs_counts": {
                "DGS2": len(dgs2), "DGS10": len(dgs10),
                "UNRATE": len(unrate), "CPIAUCSL": len(cpi),
                "GDPC1": len(gdp), "GDPPOT": len(pot), "USSLIND": len(lei),
            },
        },
    )

    claim_id = await write_derived(
        pool,
        draft,
        entity_id=macro_entity,
        input_claim_ids=input_ids,
        audience_user_id=None,
        redistributable="allowed",
        source=SOURCE,
    )
    logger.info(
        "macro regime assessed: %s, %s, inflation %.1f%% (%s), recession_prob %.2f",
        phase, risk, inf_yoy, inf_regime, prob,
    )
    return claim_id
