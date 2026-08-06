import type { StalenessTier } from "./age";
import { formatAge } from "./age";
import { authedGetJson } from "./auth";

export interface LoopStatus {
  loop: string;
  last_activity: string | null;
  age_seconds: number | null;
  never_run: boolean;
}

export interface SystemStatus {
  now: string;
  loops: LoopStatus[];
  demand: { active: number; total: number };
  fill_last_hour: Record<string, number>;
  production_24h: { predictions: number; findings: number };
}

// /system/status is auth-gated (resolve_audience_from_request -> 401 with no
// token), so this goes through authedGetJson: an absent token throws
// AuthRequiredError rather than a silent anonymous fetch that would always 401.
export const getSystemStatus = (): Promise<SystemStatus> =>
  authedGetJson<SystemStatus>("/system/status");

export type LoopCadence = "scheduled" | "on_demand";

// A loop's cadence decides whether staleness is a *health* signal or just a
// quiet period. Scheduled loops write on a tick, so silence means the engine
// stopped; on_demand loops write only when there is something to say, so
// silence is a legitimate state (a quiet week of findings is the system
// working, per the conviction gate). Grading an on_demand loop on the same
// staleness scale as a scheduled one would manufacture a false alarm exactly
// where the product is designed to stay quiet.
export const LOOP_CADENCE: Record<string, LoopCadence> = {
  prediction: "scheduled",
  fill: "scheduled",
  finding: "on_demand",
  demand: "on_demand",
  claim_ingest: "on_demand",
};

export function loopCadence(name: string): LoopCadence {
  return LOOP_CADENCE[name] ?? "on_demand";
}

const MINUTE = 60;
const HOUR = 60 * MINUTE;

// Operational thresholds for scheduled loops. These are glance-grade, not SLAs:
// a prediction loop that last wrote over an hour ago has missed several ticks
// and warrants attention; one idle for six hours is almost certainly stuck.
// never_run is its own state (a fresh deployment), distinct from a dead loop,
// and must never be graded as "dead" -- that would conflate "hasn't started"
// with "broke".
export function scheduledLoopTier(
  ageSeconds: number | null,
  neverRun: boolean,
): StalenessTier {
  if (neverRun) return "unknown";
  if (ageSeconds === null || Number.isNaN(ageSeconds)) return "unknown";
  if (ageSeconds < 0) return "fresh";
  if (ageSeconds < 2 * MINUTE) return "fresh";
  if (ageSeconds < 15 * MINUTE) return "recent";
  if (ageSeconds < HOUR) return "aging";
  if (ageSeconds < 6 * HOUR) return "stale";
  return "dead";
}

// The worst health among scheduled loops only -- the single "is the engine
// alive" reading for the compact rail. On_demand loops are excluded so a
// healthy silence in findings/demand cannot raise the headline tier.
const TIER_RANK: Record<StalenessTier, number> = {
  fresh: 0,
  recent: 1,
  aging: 2,
  stale: 3,
  dead: 4,
  unknown: 5,
};

export function worstScheduledTier(loops: LoopStatus[]): StalenessTier {
  const scheduled = loops.filter((l) => loopCadence(l.loop) === "scheduled");
  if (scheduled.length === 0) return "unknown";
  let worst: StalenessTier = "fresh";
  for (const l of scheduled) {
    const tier = scheduledLoopTier(l.age_seconds, l.never_run);
    if (TIER_RANK[tier] > TIER_RANK[worst]) worst = tier;
  }
  return worst;
}

export type FillOutcomeClass = "good" | "blocked" | "failed" | "neutral";

// fill_outcome enum is fixed at ('filled', 'unfillable', 'error') in
// 001_core_schema.sql. `error` is the actionable one (provider auth/network/
// schema failure); `unfillable` is demand the catalog genuinely cannot satisfy,
// which is a real signal but not a crash. Any future outcome renders neutral
// rather than being silently miscategorized.
export function fillOutcomeClass(outcome: string): FillOutcomeClass {
  if (outcome === "filled") return "good";
  if (outcome === "unfillable") return "blocked";
  if (outcome === "error") return "failed";
  return "neutral";
}

// never_run is rendered as a distinct label rather than an age, because
// formatAge(null) would say "no data" -- which reads as a failure to fetch the
// timestamp, not the truthful "this loop has never produced output yet".
export type EngineStatusWord =
  | "nominal"
  | "degraded"
  | "stalled"
  | "down"
  | "standby";

// The single headline word the rail shows for overall engine health. It maps
// the worst scheduled-loop tier to a state a glance can parse: nominal needs no
// action, degraded means ticks are slipping, stalled means a loop has missed
// many cycles, down means almost certainly stopped. "standby" is the fresh
// deployment (never_run) -- distinct from down so first install does not read
// as an outage.
export function engineStatusWord(tier: StalenessTier): EngineStatusWord {
  switch (tier) {
    case "fresh":
    case "recent":
      return "nominal";
    case "aging":
      return "degraded";
    case "stale":
      return "stalled";
    case "dead":
      return "down";
    case "unknown":
    default:
      return "standby";
  }
}

// never_run is rendered as a distinct label rather than an age, because
// formatAge(null) would say "no data" -- which reads as a failure to fetch the
// timestamp, not the truthful "this loop has never produced output yet".
export function loopAgeLabel(loop: LoopStatus): string {
  if (loop.never_run) return "never run";
  return formatAge(loop.age_seconds);
}
