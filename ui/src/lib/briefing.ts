import { authHeaderIfPresent, request } from "./api";

export interface BriefingEntity {
  id: string;
  symbol: string | null;
  name: string | null;
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
  supporting: string[];
  disconfirming: string[];
  prediction_id: string | null;
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
// its owner. Attach the token when present so a logged-in viewer sees their
// private findings; anonymous falls through to the shared feed only. The
// scorecard and refusals endpoints are not audience-scoped, so they stay
// anonymous.
export const getBriefing = (): Promise<BriefingFinding[]> =>
  request<BriefingFinding[]>("/briefing", authHeaderIfPresent());

export const getScorecard = (): Promise<ScorecardRow[]> =>
  request<ScorecardRow[]>("/briefing/scorecard");

export const getRefusals = (): Promise<RefusalCounts> =>
  request<RefusalCounts>("/briefing/refusals");

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
  return `${n} finding${n === 1 ? "" : "s"} surfaced`;
}
