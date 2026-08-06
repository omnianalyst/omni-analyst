import type { RegimeValue, SectorEntry } from "./autonomous";
import type { RefusalCounts, ScorecardRow } from "./briefing";

export type Tone = "pos" | "neg" | "";

const POSITIVE = new Set([
  "expansion",
  "risk_on",
  "cooling",
  "dovish",
  "favorable",
  "uptrend",
]);
const NEGATIVE = new Set([
  "contraction",
  "risk_off",
  "rising",
  "hawkish",
  "unfavorable",
  "downtrend",
]);

// Regime vocabulary is a fixed set of categorical states; mapping them to a
// pos/neg tone is display shaping only and never changes the underlying claim.
// An unrecognized value renders neutral rather than being guessed at.
export function regimeTone(value: string): Tone {
  const lower = value.toLowerCase();
  if (POSITIVE.has(lower)) return "pos";
  if (NEGATIVE.has(lower)) return "neg";
  return "";
}

export interface RegimeBadge {
  label: string;
  value: string;
  tone: Tone;
}

// The compact strip surfaces the four categorical regime calls; the numeric
// state goes into the metric row below it. Keeping them separate means a glance
// reads "phase / risk / inflation / policy" as words, then the numbers.
export function regimeBadges(v: RegimeValue): RegimeBadge[] {
  return [
    { label: "phase", value: v.cycle_phase, tone: regimeTone(v.cycle_phase) },
    { label: "risk", value: v.risk_regime.replace("_", " "), tone: regimeTone(v.risk_regime) },
    { label: "inflation", value: v.inflation_regime, tone: regimeTone(v.inflation_regime) },
    { label: "policy", value: v.policy_stance, tone: regimeTone(v.policy_stance) },
  ];
}

export interface RegimeMetric {
  label: string;
  value: string;
  sub?: string;
}

export function regimeMetrics(v: RegimeValue): RegimeMetric[] {
  return [
    {
      label: "Recession prob",
      value: `${(v.recession_probability * 100).toFixed(0)}%`,
      sub: v.recession_assessment,
    },
    { label: "CPI YoY", value: `${v.inflation_yoy.toFixed(1)}%` },
    {
      label: "Yield spread",
      value: v.yield_curve_spread != null ? `${v.yield_curve_spread.toFixed(2)}%` : "\u2014",
      sub: v.yield_curve_inverted ? "inverted" : "normal",
    },
    { label: "Output gap", value: `${v.output_gap.toFixed(1)}%` },
  ];
}

export interface ScorecardSummary {
  surfaced: number;
  resolved: number;
  hits: number;
  rate: number | null;
}

// The console's credibility gauge: one hit rate pooled across methods, not a
// per-method table. rate is null when nothing has resolved yet, so the gauge
// reads "not yet calibrated" (via formatHitRate) rather than a fake 0%.
export function aggregateScorecard(rows: ScorecardRow[]): ScorecardSummary {
  let surfaced = 0;
  let resolved = 0;
  let hits = 0;
  for (const r of rows) {
    surfaced += r.surfaced;
    resolved += r.resolved;
    hits += r.hits;
  }
  return { surfaced, resolved, hits, rate: resolved > 0 ? hits / resolved : null };
}

export interface RefusalTop {
  reason: string;
  n: number;
}

// The single most common refusal reason, for the sidebar headline. Returns null
// when nothing has been refused yet so the UI can omit the block honestly
// rather than render a hollow "top: none".
export function topReason(counts: RefusalCounts): RefusalTop | null {
  let best: RefusalTop | null = null;
  for (const [reason, n] of Object.entries(counts)) {
    if (best === null || n > best.n) best = { reason, n };
  }
  return best;
}

// Sector leadership by relative strength, capped to the requested count. A
// missing or NaN percentile reads as 0 (worst rank), consistent with how the
// sector scanner treats absent scores -- `?? 0` alone is not enough because it
// leaves NaN untouched, and NaN in the comparator makes sort order undefined.
function rsPercentile(e: SectorEntry): number {
  const p = e.score.rs_percentile;
  return p == null || Number.isNaN(p) ? 0 : p;
}

export function topSectors(sectors: SectorEntry[], n: number): SectorEntry[] {
  return [...sectors]
    .sort((a, b) => rsPercentile(b) - rsPercentile(a))
    .slice(0, n);
}
