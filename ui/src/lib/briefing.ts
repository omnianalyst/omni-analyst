import { authHeaderIfPresent, request } from "./api";

export interface BriefingEntity {
  id: string;
  symbol: string | null;
  name: string | null;
}

export interface DeductionLayer {
  layer: string;
  claim_id?: string;
  etf_symbol?: string;
  cycle_phase?: string;
  risk_regime?: string;
  inflation_regime?: string;
  policy_stance?: string;
  recession_probability?: number;
  rs_percentile?: number;
  trend?: string;
  macro_alignment?: string;
  entity_id?: string;
  prediction_id?: string;
  direction?: string;
  confidence?: number;
  method?: string;
  sector_etf?: string;
}

export interface BriefingFinding {
  id: string;
  claim_id: string;
  entity_id: string;
  entity: BriefingEntity;
  method: string;
  confidence: number;
  threshold: number | null;
  calibrated_hit_rate: number | null;
  direction: "up" | "down" | null;
  entry_price: number | null;
  upper_barrier: number | null;
  lower_barrier: number | null;
  supporting: string[];
  disconfirming: string[];
  prediction_id: string | null;
  deduction_chain?: DeductionLayer[];
  created_at: string | null;
}

export interface ScorecardRow {
  method: string;
  surfaced: number;
  resolved: number;
  hits: number;
  hit_rate: number | null;
}

export type RefusalCounts = Record<string, number>;

// /briefing is audience-scoped server-side: a byo-derived finding belongs to
// its owner. The scorecard and refusals are operator-only AND audience-scoped
// (an operator sees the shared network's record plus their own, never another
// operator's byo-derived rate), so all three attach the token when present and
// the server refuses an anonymous caller on scorecard/refusals.
export const getBriefing = (): Promise<BriefingFinding[]> =>
  request<BriefingFinding[]>("/briefing", authHeaderIfPresent());

export const getScorecard = (): Promise<ScorecardRow[]> =>
  request<ScorecardRow[]>("/briefing/scorecard", authHeaderIfPresent());

export const getRefusals = (): Promise<RefusalCounts> =>
  request<RefusalCounts>("/briefing/refusals", authHeaderIfPresent());

export function formatHitRate(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "not yet calibrated";
  }
  return `${Math.round(value * 100)}%`;
}

export function formatConfidence(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "\u2014";
  return Number(value).toFixed(2);
}

export function explainRefusal(reason: string): string {
  switch (reason) {
    case "class_has_too_few_resolved_predictions":
      return "Class has too few resolved predictions to calibrate a threshold.";
    case "confidence_below_the_calibrated_threshold":
      return "Confidence was below the calibrated threshold.";
    case "no_disconfirming_evidence_was_gathered":
      return "No disconfirming evidence was gathered.";
    case "no_falsifiable_prediction_could_be_written":
      return "No falsifiable prediction could be written.";
    default:
      return reason;
  }
}

export function refusalTotal(counts: RefusalCounts): number {
  return Object.values(counts).reduce((sum, n) => sum + n, 0);
}

export function briefingHeading(findings: BriefingFinding[]): string {
  if (findings.length === 0) return "Nothing met the bar";
  const n = findings.length;
  return `${n} call${n === 1 ? "" : "s"} surfaced`;
}
