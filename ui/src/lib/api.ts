import { API_BASE_URL } from "../config";

export const AUTH_TOKEN_KEY = "omni.auth.token";

export function getAuthToken(): string | null {
  try {
    return localStorage.getItem(AUTH_TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setAuthToken(token: string): void {
  try {
    localStorage.setItem(AUTH_TOKEN_KEY, token);
  } catch {
    /* storage unavailable; auth cannot persist across reloads */
  }
}

export function clearAuthToken(): void {
  try {
    localStorage.removeItem(AUTH_TOKEN_KEY);
  } catch {
    /* storage unavailable */
  }
}

export class ApiUnavailableError extends Error {
  constructor(
    public url: string,
    public cause: unknown,
  ) {
    super(`Could not reach the API at ${url}`);
    this.name = "ApiUnavailableError";
  }
}

export class ApiHttpError extends Error {
  constructor(
    public status: number,
    public url: string,
    public body: string,
  ) {
    super(`API responded ${status} from ${url}`);
    this.name = "ApiHttpError";
  }
}

export interface Entity {
  id: string;
  kind: string;
  symbol: string | null;
  name: string | null;
}
export interface EntitiesResponse {
  query: string;
  entities: Entity[];
}

export interface CoverageGroup {
  claim_type: string;
  count: number;
  private_count: number;
  newest_knowledge_date: string | null;
  age_seconds: number | null;
  source_count: number;
  sources: string[];
  mean_confidence: number | null;
}
export interface CoverageResponse {
  entity_id: string;
  groups: CoverageGroup[];
}

export interface Gap {
  id: string;
  claim_type: string;
  key: string | null;
  gap_class: string;
  audience_user_id: string | null;
  score: number;
  attempts: number;
  detail: unknown;
  detected_at: string | null;
}
export interface GapsResponse {
  entity_id: string;
  gaps: Gap[];
}

export interface Claim {
  id: string;
  claim_type: string;
  key: string | null;
  value: unknown;
  unit: string | null;
  source: string;
  event_date: string | null;
  knowledge_date: string | null;
  confidence: number | null;
  redistributable: string;
}
export interface ClaimsResponse {
  entity_id: string;
  limit: number;
  claims: Claim[];
}

export async function request<T>(
  path: string,
  headers: Record<string, string> = {},
): Promise<T> {
  const url = API_BASE_URL + path;
  let res: Response;
  try {
    res = await fetch(url, { headers: { accept: "application/json", ...headers } });
  } catch (err) {
    throw new ApiUnavailableError(url, err);
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new ApiHttpError(res.status, url, body);
  }
  return res.json() as Promise<T>;
}

export async function sendJson<T>(
  method: string,
  path: string,
  body?: unknown,
  headers: Record<string, string> = {},
): Promise<T> {
  const url = API_BASE_URL + path;
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: {
        accept: "application/json",
        "content-type": "application/json",
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
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

export function authHeaderIfPresent(): Record<string, string> {
  const token = getAuthToken();
  return token ? { authorization: `Bearer ${token}` } : {};
}

export const searchEntities = (q: string): Promise<EntitiesResponse> =>
  request<EntitiesResponse>(`/entities?q=${encodeURIComponent(q)}`);

// Coverage and gaps are audience-scoped server-side: an absent token reads as
// the shared network, a present token adds this viewer's own BYO claims. Attach
// the token when the client holds one so a logged-in user sees their private
// coverage; anonymous falls through unchanged.
export const getCoverage = (id: string): Promise<CoverageResponse> =>
  request<CoverageResponse>(
    `/coverage/${encodeURIComponent(id)}`,
    authHeaderIfPresent(),
  );

export const getGaps = (id: string): Promise<GapsResponse> =>
  request<GapsResponse>(
    `/gaps/${encodeURIComponent(id)}`,
    authHeaderIfPresent(),
  );

export const getClaims = (id: string, claimType: string): Promise<ClaimsResponse> =>
  request<ClaimsResponse>(
    `/coverage/${encodeURIComponent(id)}/claims?claim_type=${encodeURIComponent(claimType)}`,
    authHeaderIfPresent(),
  );

export function describeError(err: unknown): {
  message: string;
  detail?: string;
} {
  if (err instanceof ApiUnavailableError) {
    const reason =
      err.cause instanceof Error ? err.cause.message : String(err.cause);
    return {
      message: err.message,
      detail: `The coverage API did not answer (${reason}). Showing nothing instead of guessing.`,
    };
  }
  if (err instanceof ApiHttpError) {
    const detail = err.body
      ? `${err.status} — ${err.body.slice(0, 200)}`
      : `${err.status}`;
    return { message: err.message, detail };
  }
  return {
    message: err instanceof Error ? err.message : "Request failed.",
  };
}
