import { authedGetJson, authedSendJson } from "./auth";

export type ConditionKind =
  | "value_above"
  | "value_below"
  | "staleness_exceeds"
  | "contradiction";

export const CONDITION_KINDS: ConditionKind[] = [
  "value_above",
  "value_below",
  "staleness_exceeds",
  "contradiction",
];

// The wire values are stable identifiers; the labels are what a person picking
// from a dropdown should read. A <select> listing "staleness_exceeds" asks the
// reader to decode a schema.
const CONDITION_LABELS: Record<ConditionKind, string> = {
  value_above: "Value rises above",
  value_below: "Value falls below",
  staleness_exceeds: "Data goes stale",
  contradiction: "Sources disagree",
};

export function conditionLabel(kind: ConditionKind): string {
  return CONDITION_LABELS[kind] ?? kind;
}

export interface ValueThresholdCondition {
  kind: "value_above" | "value_below";
  threshold: number;
  field: string;
}

export interface StalenessCondition {
  kind: "staleness_exceeds";
  seconds: number;
}

export interface ContradictionCondition {
  kind: "contradiction";
}

export type AlertCondition =
  | ValueThresholdCondition
  | StalenessCondition
  | ContradictionCondition;

export interface Alert {
  id: string;
  user_id: string;
  entity_id: string;
  claim_type: string;
  condition: unknown;
  active: boolean;
  created_at: string | null;
  last_fired_at: string | null;
}

export interface AlertsResponse {
  alerts: Alert[];
}

export interface Firing {
  claim_id: string;
  fired_at: string | null;
  claim_type: string;
  key: string | null;
  event_date: string | null;
  value: unknown;
  source: string | null;
}

export interface FiringsResponse {
  alert_id: string;
  firings: Firing[];
}

export interface CreateAlertRequest {
  entity_id: string;
  claim_type: string;
  condition: AlertCondition;
}

export const listAlerts = (): Promise<AlertsResponse> =>
  authedGetJson<AlertsResponse>("/alerts");

export const createAlert = (req: CreateAlertRequest): Promise<Alert> =>
  authedSendJson<Alert>("POST", "/alerts", req);

export const updateAlert = (
  alertId: string,
  patch: { active?: boolean; condition?: AlertCondition },
): Promise<Alert> =>
  authedSendJson<Alert>(
    "PATCH",
    `/alerts/${encodeURIComponent(alertId)}`,
    patch,
  );

export const deleteAlert = (alertId: string): Promise<{ deleted: boolean }> =>
  authedSendJson<{ deleted: boolean }>(
    "DELETE",
    `/alerts/${encodeURIComponent(alertId)}`,
  );

export const listFirings = (alertId: string): Promise<FiringsResponse> =>
  authedGetJson<FiringsResponse>(
    `/alerts/${encodeURIComponent(alertId)}/firings`,
  );

export interface ConditionFormState {
  kind: ConditionKind;
  threshold: string;
  field: string;
  seconds: string;
}

export function defaultConditionForm(): ConditionFormState {
  return { kind: "value_above", threshold: "", field: "value", seconds: "" };
}

export function conditionForm(condition: unknown): ConditionFormState {
  const initial = defaultConditionForm();
  if (typeof condition !== "object" || condition === null) return initial;
  const value = condition as Record<string, unknown>;
  if (!CONDITION_KINDS.includes(value.kind as ConditionKind)) return initial;
  return {
    kind: value.kind as ConditionKind,
    threshold: value.threshold === undefined ? "" : String(value.threshold),
    field: typeof value.field === "string" ? value.field : "value",
    seconds: value.seconds === undefined ? "" : String(value.seconds),
  };
}

export type BuildResult =
  | { ok: true; condition: AlertCondition }
  | { ok: false; error: string };

export function buildCondition(form: ConditionFormState): BuildResult {
  switch (form.kind) {
    case "value_above":
    case "value_below": {
      const threshold = parseNumber(form.threshold);
      if (threshold === null) {
        return { ok: false, error: `${form.kind}.threshold must be a number` };
      }
      const field = form.field.trim();
      if (!field) {
        return { ok: false, error: `${form.kind}.field must be a non-empty string` };
      }
      return { ok: true, condition: { kind: form.kind, threshold, field } };
    }
    case "staleness_exceeds": {
      const seconds = parseNumber(form.seconds);
      if (seconds === null) {
        return { ok: false, error: "staleness_exceeds.seconds must be a number" };
      }
      if (seconds <= 0) {
        return { ok: false, error: "staleness_exceeds.seconds must be positive" };
      }
      return { ok: true, condition: { kind: "staleness_exceeds", seconds } };
    }
    case "contradiction":
      return { ok: true, condition: { kind: "contradiction" } };
  }
}

function parseNumber(raw: string): number | null {
  const s = raw.trim();
  if (s === "") return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

export function describeCondition(c: unknown): string {
  if (typeof c !== "object" || c === null) return "(malformed condition)";
  const kind = (c as { kind?: unknown }).kind;
  switch (kind) {
    case "value_above":
    case "value_below": {
      const t = (c as { threshold?: unknown }).threshold;
      const f = (c as { field?: unknown }).field ?? "value";
      const op = kind === "value_above" ? ">" : "<";
      return `${kind}: ${String(f)} ${op} ${String(t)}`;
    }
    case "staleness_exceeds": {
      const s = (c as { seconds?: unknown }).seconds;
      return `staleness_exceeds: ${String(s)}s`;
    }
    case "contradiction":
      return "contradiction";
    default:
      return `(unknown condition: ${String(kind)})`;
  }
}

export function describeLastFired(lastFiredAt: string | null): string {
  if (lastFiredAt === null) return "never fired";
  return `last fired ${lastFiredAt.slice(0, 19).replace("T", " ")}`;
}
