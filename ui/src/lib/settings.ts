import { authedGetJson, authedSendJson } from "./auth";

export type ConfigurationSource = "deployment" | "encrypted" | "legacy" | "unavailable";

export interface VenueField {
  name: string;
  label: string;
  type: "text" | "password" | "checkbox" | "select";
  options?: string[];
  required: boolean;
}

export interface VenueEntry {
  key: string;
  label: string;
  type: string;
  connectable: boolean;
  requires_process: boolean;
  description: string;
  fields?: VenueField[];
  configured: boolean;
  enabled: boolean;
  configuration_source: ConfigurationSource;
}

export interface ProviderEntry {
  key: string;
  label: string;
  category: string;
  settings_field: string;
  key_required: boolean;
  wired: boolean;
  configured: boolean;
}

export interface SettingsData {
  provider_catalog: ProviderEntry[];
  venue_catalog: VenueEntry[];
}

export type VenueConnectionState =
  | "connected"
  | "disabled"
  | "not_configured"
  | "error"
  | "scheduler_only";

export interface VenueLiveStatus {
  key: string;
  status: VenueConnectionState;
  checked_at: string;
  error: string | null;
  positions: unknown[];
  balances: unknown[];
}

export interface VenueStatusResponse {
  checked_at: string;
  venues: VenueLiveStatus[];
}

export const getSettings = (): Promise<SettingsData> =>
  authedGetJson<SettingsData>("/settings/config");

export const getVenueStatus = (): Promise<VenueStatusResponse> =>
  authedGetJson<VenueStatusResponse>("/settings/venues/status");

export const toggleVenue = (key: string, enabled: boolean) =>
  authedSendJson<{ status: string; venue_status: string }>(
    "POST", `/settings/venue/${key}/toggle`, { enabled },
  );

export const saveVenueCredentials = (key: string, credentials: Record<string, unknown>) =>
  authedSendJson<{ status: string; encrypted: boolean; venue_status: string }>(
    "POST", `/settings/venue/${key}/credentials`, { credentials },
  );

export const clearVenueCredentials = (key: string) =>
  authedSendJson<{ status: string }>("DELETE", `/settings/venue/${key}/credentials`);

/** What the operator is told about how a venue's credentials are held.
 *
 * `legacy` is deliberately phrased as an action rather than a state. A row
 * predating the credential key is readable in a database dump, and describing
 * it neutrally would leave it sitting there.
 */
export function describeSource(entry: VenueEntry): { label: string; tone: string; detail: string } {
  if (!entry.connectable) {
    return {
      label: "Scheduler deployment",
      tone: "quiet",
      detail: "Configured outside the API. Credential and connection state are not visible here.",
    };
  }
  switch (entry.configuration_source) {
    case "deployment":
      return {
        label: entry.configured ? "Deployment-managed" : "Not configured",
        tone: entry.configured ? "ok" : "quiet",
        detail: entry.configured
          ? "Held in the deployment environment. The API process never receives these."
          : "Add these to the deployment environment; they cannot be set from the browser.",
      };
    case "encrypted":
      return {
        label: "Stored, encrypted",
        tone: "ok",
        detail: "Encrypted at rest under this deployment's credential key.",
      };
    case "legacy":
      return {
        label: "Stored in plain text",
        tone: "warn",
        detail: "Saved before encryption existed and readable in a database dump. Clear it and re-enter.",
      };
    default:
      return {
        label: "Not configured",
        tone: "quiet",
        detail: "No credentials stored.",
      };
  }
}

/** Whether a venue can meaningfully be switched on right now. */
export function canEnable(entry: VenueEntry): boolean {
  return entry.connectable && entry.configured;
}

/** Why a toggle is unavailable, or null when it is available. */
export function blockedReason(entry: VenueEntry): string | null {
  if (!entry.connectable) return "Managed by the scheduler";
  if (entry.configured) return null;
  if (entry.configuration_source === "deployment") {
    return "Waiting on deployment secrets";
  }
  return "Add credentials first";
}
