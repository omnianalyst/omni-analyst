import { API_BASE_URL } from "../config";
import { ApiHttpError, ApiUnavailableError } from "./api";

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

async function getJson<T>(path: string): Promise<T> {
  const url = API_BASE_URL + path;
  let res: Response;
  try {
    res = await fetch(url, { headers: { accept: "application/json" } });
  } catch (err) {
    throw new ApiUnavailableError(url, err);
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new ApiHttpError(res.status, url, body);
  }
  return res.json() as Promise<T>;
}

export const getBriefing = (): Promise<BriefingFinding[]> =>
  getJson<BriefingFinding[]>("/briefing");

export const getScorecard = (): Promise<ScorecardRow[]> =>
  getJson<ScorecardRow[]>("/briefing/scorecard");

export const getRefusals = (): Promise<RefusalCounts> =>
  getJson<RefusalCounts>("/briefing/refusals");

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
