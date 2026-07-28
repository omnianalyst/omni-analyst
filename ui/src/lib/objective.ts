import { API_BASE_URL } from "../config";
import { ApiHttpError, ApiUnavailableError } from "./api";

export interface ObjectiveStep {
  capability: string;
  claim_type: string;
  target: string;
  cost: number;
  licence_tier: string;
}

export interface ObjectiveShortfall {
  claim_type: string;
  reason: string;
  detail: string;
}

export interface ObjectiveRunStepResult {
  capability: string;
  claim_type: string;
  ok: boolean;
  output: unknown;
  error: string | null;
}

export interface ObjectivePlan {
  objective: string;
  steps: ObjectiveStep[];
  shortfalls: ObjectiveShortfall[];
  cost: number;
  satisfiable: boolean;
  partial: boolean;
  summary: string;
}

export interface ObjectiveRunResult extends ObjectivePlan {
  results: ObjectiveRunStepResult[];
  evidence: unknown;
  answered: boolean;
  demand_raised: string[];
}

export interface ObjectiveRequest {
  text: string;
  target: string;
  needs: string[];
  entity_kind?: string | null;
  shareable?: boolean;
  budget?: number;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const url = API_BASE_URL + path;
  let res: Response;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify(body),
    });
  } catch (err) {
    throw new ApiUnavailableError(url, err);
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiHttpError(res.status, url, text);
  }
  return res.json() as Promise<T>;
}

export const planObjective = (req: ObjectiveRequest): Promise<ObjectivePlan> =>
  postJson<ObjectivePlan>("/objective/plan", req);

export const runObjective = (req: ObjectiveRequest): Promise<ObjectiveRunResult> =>
  postJson<ObjectiveRunResult>("/objective/run", req);

export function parseNeeds(raw: string): string[] {
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

export function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "\u2014";
  return Number(value).toFixed(2);
}

export function explainLicenceTier(tier: string): string {
  switch (tier) {
    case "private":
      return "private \u2014 BYO-licensed, visible only to you";
    case "shared":
      return "shared \u2014 redistributable, counts toward the shared network";
    default:
      return tier;
  }
}

export function explainShortfall(reason: string): string {
  switch (reason) {
    case "no_capability_produces_this":
      return "No capability produces this claim type yet. It would need a new adapter before it can be answered.";
    case "only_licensed_sources_can_produce_this":
      return "Every producer is licensed per operator, so it cannot enter a shareable answer. Scope the objective to yourself, or obtain a redistribution licence.";
    case "cheapest_viable_plan_exceeds_budget":
      return "The cheapest source for this exceeds the remaining budget. Raise the budget or drop one of the needs.";
    default:
      return reason;
  }
}
