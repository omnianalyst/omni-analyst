"""Phase B: the macro regime assessment loop.

The system's first autonomous act is to look at the world. This loop reads the
five macro signal claims the existing derived capabilities produce (yield curve,
Sahm, inflation, output gap, LEI), composes them into a single regime
assessment, and writes it as a ``regime_assessment`` claim. That claim is the
root of every deduction chain the sector scanner and synthesis finding build on.

The composition is DETERMINISTIC CODE, not an LLM opining. "Expansion" means
the recession-probability function -- the same fixed-weight sum the existing
``macro.recession_probability`` capability uses (YC 0.3, Sahm 0.4, LEI 0.3) --
returns a probability below the contraction threshold. "Risk_off" means that
probability is elevated or inflation is in stagflation territory. Each field is
a mechanical read of the claim values, not a judgment.

If any of the five required signals is absent from shared coverage, the loop
abstains and writes nothing. This is invariant #3 from the plan: a system that
always has something to say is lying. A fresh deployment with no FRED data has
no regime -- silence is the honest outcome.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from omni.autonomous.reading import latest_shared_claim
from omni.ingest.protocol import ClaimDraft
from omni.perception.divergence import write_derived

logger = logging.getLogger("omni.autonomous.macro")

SOURCE = "omni.autonomous"
CLAIM_TYPE = "regime_assessment"
KEY = "us_macro"

# The five signals the assessment composes from. Each is an earned macro claim
# type produced by a derived capability (see capability/derived.py). All must be
# present in shared coverage; a missing one is an abstention, not a substitute.
_SIGNAL_TYPES = {
    "yield_curve": "yield_curve_signal",
    "sahm": "sahm_rule_signal",
    "inflation": "inflation_signal",
    "output_gap": "output_gap_signal",
    "lei": "lei_signal",
}

# Fixed weights matching macro.recession_probability (macro.py:303-326). Kept as
# literals here rather than imported because the capability takes already-
# computed sibling outputs and we are reading the sibling CLAIMS -- the weights
# are the same, the call shape is not. If the capability's weights change, the
# test test_recession_weights_match_capability will catch the drift.
_W_YC = 0.3
_W_SAHM = 0.4
_W_LEI = 0.3


# -- Pure composition (testable without a database) --------------------------


def recession_probability(
    yc_inverted: bool, sahm_triggered: bool, lei_negative: bool
) -> tuple[float, str]:
    """Fixed-weight recession probability and its plain-language assessment.

    Mirrors ``macro.recession_probability`` exactly: the probability is the
    weighted sum of three boolean recession signals, and the assessment bands
    follow the same thresholds (< 0.2 low, < 0.4 moderate, < 0.7 elevated,
    >= 0.7 high).
    """
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
    """The business-cycle phase implied by the three recession signals.

    Sahm triggered = recession is here (``contraction``). Curve inverted or LEI
    declining but Sahm quiet = late-cycle warning (``peak``). All clear =
    ``expansion``. ``trough`` (post-recession recovery) needs historical context
    the current-state signals do not carry, so it is not emitted by this
    single-snapshot read.
    """
    if sahm_triggered:
        return "contraction"
    if yc_inverted or lei_negative:
        return "peak"
    return "expansion"


def risk_regime(
    recession_prob: float, inflation_yoy: float, output_gap: float
) -> str:
    """risk_on / risk_off / transition from the probability and the inflation gap.

    ``risk_off`` when recession probability is elevated (>= 0.4) or the economy
    is in stagflation (inflation above 3 with a negative output gap).
    ``risk_on`` when probability is low and inflation is contained. Anything
    between is ``transition`` -- the honest label for a mixed picture.
    """
    if recession_prob >= 0.4:
        return "risk_off"
    if inflation_yoy > 3.0 and output_gap < 0:
        return "risk_off"
    if recession_prob <= 0.15 and inflation_yoy <= 3.0:
        return "risk_on"
    return "transition"


def inflation_regime(inflation_yoy: float) -> str:
    """cooling / stable / rising from the CPI YoY reading."""
    if inflation_yoy < 2.0:
        return "cooling"
    if inflation_yoy <= 3.0:
        return "stable"
    return "rising"


def policy_stance(inflation_yoy: float, output_gap: float) -> str:
    """hawkish / dovish / neutral from a directional Taylor-rule read.

    Hawkish when the economy is overheating (inflation above target AND a
    positive output gap). Dovish when there is slack (inflation below target
    AND a negative gap). Neutral when the two disagree or both are at target.
    This is the *direction* of the Taylor rule, not the level -- the existing
    ``macro.taylor_rule`` computes the exact rate; this loop labels the stance.
    """
    if inflation_yoy > 2.0 and output_gap > 0:
        return "hawkish"
    if inflation_yoy < 2.0 and output_gap < 0:
        return "dovish"
    return "neutral"


# -- The loop ----------------------------------------------------------------

_EXISTING = """
SELECT id FROM claim
WHERE entity_id = $1 AND claim_type = $2::claim_type AND key = $3
  AND source = $4 AND event_date = $5 AND knowledge_date = $6
  AND audience_user_id IS NULL
"""


async def assess_macro_regime(
    pool, *, as_of: datetime | None = None
) -> UUID | None:
    """Read macro signals, compose a regime assessment, write a ``regime_assessment`` claim.

    Returns the claim id (newly written or already existing from a prior
    identical run), or ``None`` when the assessment abstained -- a required
    signal is absent from shared coverage, or the macro entity does not exist.

    Idempotent: if the inputs have not changed since the last run (same
    event_date and knowledge_date on every signal), the composed claim has the
    same identity and the existing row is returned without a second write.
    """
    macro_entity = await pool.fetchval(
        "SELECT id FROM entity WHERE kind = 'macro' AND symbol = 'US_MACRO'"
    )
    if macro_entity is None:
        return None

    signals: dict[str, dict] = {}
    for name, claim_type in _SIGNAL_TYPES.items():
        claim = await latest_shared_claim(
            pool, claim_type=claim_type, as_of=as_of
        )
        if claim is None:
            logger.info("macro regime abstained: %s missing", claim_type)
            return None
        signals[name] = claim

    yc_v = signals["yield_curve"]["value"]
    sahm_v = signals["sahm"]["value"]
    inf_v = signals["inflation"]["value"]
    og_v = signals["output_gap"]["value"]
    lei_v = signals["lei"]["value"]

    yc_inverted = bool(yc_v["is_inverted"])
    sahm_triggered = bool(sahm_v["triggered"])
    lei_negative = bool(lei_v["is_negative"])
    inf_yoy = float(inf_v["yoy"])
    og = float(og_v["output_gap"])

    prob, prob_band = recession_probability(yc_inverted, sahm_triggered, lei_negative)
    phase = cycle_phase(yc_inverted, sahm_triggered, lei_negative)
    risk = risk_regime(prob, inf_yoy, og)
    inf_regime = inflation_regime(inf_yoy)
    stance = policy_stance(inf_yoy, og)

    event_date = max(s["event_date"] for s in signals.values())
    knowledge_date = max(s["knowledge_date"] for s in signals.values())

    existing = await pool.fetchval(
        _EXISTING, macro_entity, CLAIM_TYPE, KEY, SOURCE, event_date, knowledge_date
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
        "sahm_triggered": sahm_triggered,
        "lei_negative": lei_negative,
    }

    draft = ClaimDraft(
        claim_type=CLAIM_TYPE,
        key=KEY,
        value=value,
        event_date=event_date,
        knowledge_date=knowledge_date,
        confidence=1.0,
        evidence={
            "source_signals": {
                name: {
                    "claim_type": _SIGNAL_TYPES[name],
                    "event_date": s["event_date"].isoformat() if hasattr(s["event_date"], "isoformat") else str(s["event_date"]),
                    "value": s["value"],
                }
                for name, s in signals.items()
            },
        },
    )

    input_ids = [UUID(str(s["id"])) for s in signals.values()]
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
        "macro regime assessed: %s, %s, inflation %s (%.1f%%)",
        phase, risk, inf_regime, inf_yoy,
    )
    return claim_id
