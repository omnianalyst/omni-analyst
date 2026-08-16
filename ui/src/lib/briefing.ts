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
  // False on findings written before the disconfirming search existed. Without
  // it an empty `disconfirming` is ambiguous between "looked, found nothing"
  // and "never looked", and the card would assert the first for both.
  evidence_searched?: boolean;
  created_at: string | null;
}

export interface ScorecardRow {
  method: string;
  surfaced: number;
  resolved: number;
  hits: number;
  hit_rate: number | null;
  payoff_ratio?: number | null;
  avg_risk_pct?: number | null;
  avg_payoff_pct?: number | null;
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
      return "Too few calls of this kind have played out to know what its confidence is worth.";
    case "confidence_below_the_calibrated_threshold":
      return "Confidence fell short of the bar this kind of call has to clear.";
    case "no_disconfirming_evidence_was_gathered":
      return "The counter-case could not be checked \u2014 not enough price history.";
    case "no_disconfirming_search_exists_for_this_method":
      return "No counter-case checks exist for this method yet, so it cannot be surfaced.";
    case "no_falsifiable_prediction_could_be_written":
      return "No price could be named that would prove the call wrong, so it cannot be scored.";
    default:
      return reason;
  }
}

export function refusalTotal(counts: RefusalCounts): number {
  // _unproven rides in the same payload but is not a refusal; summing it
  // would inflate the denominator it exists to complement.
  return Object.entries(counts)
    .filter(([key]) => key !== "_unproven")
    .reduce((sum, [, n]) => sum + n, 0);
}

export function unprovenCount(counts: RefusalCounts): number | null {
  const value = counts["_unproven"];
  return typeof value === "number" ? value : null;
}

export function briefingHeading(findings: BriefingFinding[]): string {
  if (findings.length === 0) return "Nothing met the bar";
  const n = findings.length;
  return `${n} call${n === 1 ? "" : "s"} surfaced`;
}
