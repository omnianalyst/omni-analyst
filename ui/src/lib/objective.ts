import { API_BASE_URL } from "../config";
import { ApiHttpError, ApiUnavailableError, authHeaderIfPresent } from "./api";

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

export interface Capability {
  name: string;
  description: string;
  produces: string[];
  consumes: string[];
  licence_tier: string;
  cost: number;
}

// The claim types the planner can actually produce, derived from the live
// registry rather than typed from memory into a free-text box. A capability
// with an empty `produces` cannot satisfy a need, so it contributes nothing
// here -- which is also why the picker is much shorter than the capability
// count.
export async function fetchClaimTypes(): Promise<string[]> {
  const url = API_BASE_URL + "/capabilities";
  let res: Response;
  try {
    res = await fetch(url, { headers: { accept: "application/json" } });
  } catch (err) {
    throw new ApiUnavailableError(url, err);
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiHttpError(res.status, url, text);
  }
  const body = (await res.json()) as { capabilities: Capability[] };
  const types = new Set<string>();
  for (const c of body.capabilities ?? []) {
    for (const p of c.produces ?? []) types.add(p);
  }
  return [...types].sort();
}

// "price_snapshot" -> "Price snapshot". The registry's names are stable
// identifiers; this is display only and never travels back to the API.
export function humaniseClaimType(claimType: string): string {
  const spaced = claimType.replace(/[._]/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
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
        // /objective/run is auth-gated: without the token the run 401s while
        // the (anonymous) plan beside it succeeded -- exactly the "You are
        // not signed in" contradiction observed live 2026-08-21.
        ...authHeaderIfPresent(),
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
