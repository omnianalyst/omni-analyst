// Plain-English definitions for the vocabulary this product cannot avoid using.
//
// The terms below are load-bearing -- "RS percentile" and "calibrated threshold"
// mean something precise and no shorter word carries it -- but a reader meeting
// them for the first time has no way in. Rather than dumb the labels down (which
// would cost precision) or leave them bare (which costs comprehension), each one
// carries its definition on hover and focus.
//
// One definition per term, defined once here, so the same word cannot come to
// mean two things on two pages. Keep them to a sentence or two: a tooltip that
// needs scrolling is documentation in the wrong place.

export const GLOSSARY: Record<string, string> = {
  // -- What the system produces ----------------------------------------------
  call:
    "A directional statement the system chose to make: this name is trending up or down, with a price that would prove it wrong. Analysis, not advice.",
  conviction_gate:
    "The check every call must pass before it is shown. It compares the call's confidence against how often calls of that class have actually resolved correctly. Nothing that has not been measured can be surfaced at all.",
  calibrated_threshold:
    "The confidence level at which this class of call has historically been right often enough to be worth showing. Derived from resolved outcomes, never chosen by hand.",
  confidence:
    "The model's own estimate that this call hits its target before it hits its invalidation level. It is a self-report, which is why it is always shown next to the measured hit rate.",
  hit_rate:
    "How often calls of this class have actually resolved correctly. Measured, not estimated. Blank until at least ten predictions have resolved, because a rate from fewer is noise.",
  invalidation:
    "The price that proves this call wrong. Fixed when the call is made, so the outcome cannot be reinterpreted after the fact.",
  refusal:
    "A call the system considered and decided not to make, with the reason recorded. Refusals are the denominator behind the hit rate: without them, any accuracy figure can be made to look good.",
  supporting:
    "Facts from coverage that argue for this call.",
  disconfirming:
    "Facts from coverage that argue against this call. The system refuses to surface anything it has not looked for a counter-case on, so an empty list here means the checks ran and found nothing, not that nothing was checked.",
  prediction:
    "The falsifiable record behind a call: a direction, an entry price, a target and an invalidation level, all fixed at the time it was written so it can be scored honestly later.",

  // -- Macro -----------------------------------------------------------------
  cycle_phase:
    "Where the economy sits in its expansion-peak-contraction-trough cycle, read from macro indicators rather than declared.",
  risk_regime:
    "Whether conditions have been rewarding risk-taking (risk on) or punishing it (risk off).",
  recession_probability:
    "The system's estimate that a recession is underway or imminent, composed from the yield curve, the Sahm rule, leading indicators and the output gap.",
  yield_curve:
    "The gap between long-dated and short-dated government bond yields. When it inverts, long rates sit below short rates, which has preceded most recessions.",
  sahm_rule:
    "A recession indicator based on how far unemployment has risen from its recent low. It triggers at half a percentage point.",
  lei:
    "The Leading Economic Index, a composite of forward-looking indicators. A sustained six-month decline has historically preceded downturns.",
  output_gap:
    "The difference between what the economy is producing and what it could produce at full capacity. Negative means slack.",
  inflation_regime:
    "Whether price growth is accelerating, cooling, or stable, relative to recent trend.",
  policy_stance:
    "Whether the central bank is tightening (hawkish) or loosening (dovish) relative to current conditions.",

  // -- Sectors ---------------------------------------------------------------
  rs_percentile:
    "Relative strength: where this sector's recent return ranks against the other sectors, from 0 (weakest) to 100 (strongest).",
  macro_alignment:
    "Whether this sector has historically suited the current phase of the economic cycle. Favourable does not mean it will rise.",
  sector_trend:
    "The direction of this sector's price relative to its own moving average.",

  // -- Coverage --------------------------------------------------------------
  claim:
    "A single fact about an entity, carrying where it came from, when it happened, when it became knowable, and how confident the source is.",
  coverage:
    "What the system knows about an entity right now: which claim types it holds, how fresh they are, and which sources they came from.",
  gap:
    "A difference between what has been demanded about an entity and what is actually held. Gaps are the work queue.",
  byo_scoped:
    "Fetched with your own provider key. Provider terms forbid passing this data on, so it fills your gaps only and is never visible to another account or counted toward shared coverage.",
  demand:
    "A standing request for coverage of an entity. Demand is what drives the system to go and fetch things; nothing is collected speculatively.",
  freshness:
    "How long ago a claim became knowable. A stale network that looks covered is worse than an empty one, because emptiness is honest.",
  contradiction:
    "Two sources reporting different values for the same fact, same key, same date. Surfaced rather than silently resolved.",

  // -- The engine ------------------------------------------------------------
  loop_scheduled:
    "Runs on a fixed cadence. If it stops writing rows, something is wrong and the status turns stale.",
  loop_on_demand:
    "Runs only when there is work. Long silence is normal and is not a fault.",
  fill_outcome:
    "What happened on the last attempt to close a gap: filled, unfillable (the source genuinely has no answer), or an error.",
  engine_status:
    "Read from the data itself rather than from a heartbeat: a loop that is alive writes rows, one that is dead stops.",

  // -- Portfolio vocabulary (Discover) -----------------------------------------
  median_year:
    "The middle calendar-year return over everything measured: half the years were better, half worse. Robust to one freak year, unlike an average. Each asset's own history, so lengths differ.",
  volatility:
    "How widely returns swing, annualised. Under 10% is steady, 10-30% balanced, 30%+ aggressive. High volatility means bigger gains and bigger falls, not bigger expected returns.",
  max_drawdown:
    "The worst peak-to-trough fall in the asset's measured history. What holding it through the bad stretch would have cost. Past worst is not a floor on the future worst.",
  positive_year_rate:
    "The share of measured calendar years that ended up. A 78% rate means roughly four good years in five.",
  rebalance:
    "Selling what grew and buying what shrank until each holding is back at its target weight. Without it, winners quietly take over the portfolio and the balance erodes. About once a year is enough.",
  sharpe:
    "Return earned per unit of risk taken. Above roughly 1 is good over long periods; a negative number means the risk was taken for nothing. Withheld when volatility is too low for the ratio to mean anything.",
  correlation:
    "How often two assets move together, from -1 (always opposite) through 0 (unrelated) to 1 (always together). Low or negative correlation is what makes diversification work.",
  risk_share:
    "This holding's share of the portfolio's total swinging. Equal money in each does not mean equal risk: the volatile holdings drive the ride.",
  payoff_asymmetry:
    "What the call's own barriers pay: risking the smaller move (to the price that proves it wrong) to make the larger one (the target). A 4:1 call can be wrong most of the time and still compound -- probability is on the hit-rate line, payoff is on this one.",
};

export function define(term: string): string | undefined {
  return GLOSSARY[term];
}
