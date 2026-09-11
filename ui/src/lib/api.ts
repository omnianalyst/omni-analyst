import { API_BASE_URL } from "../config";

export const AUTH_TOKEN_KEY = "omni.auth.token";
export const AUTH_STATE_EVENT = "omni:auth-state";

function notifyAuthState(token: string | null): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(AUTH_STATE_EVENT, { detail: { token } }));
  }
}

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
  notifyAuthState(token);
}

export function clearAuthToken(): void {
  try {
    localStorage.removeItem(AUTH_TOKEN_KEY);
  } catch {
    /* storage unavailable */
  }
  notifyAuthState(null);
}

// A request that carried a token and came back 401 means that session is dead
// (expired or rejected). Left unhandled, every authed panel fills with 401
// errors and the app reads as broken; clearing the stale token lets the layout
// immediately hide private content and redirect to /login. Only the token that
// was ACTUALLY SENT is cleared: a slow request carrying expired token A can
// resolve after re-login installed token B, and clearing "whatever is stored"
// would kill the newer session. Anonymous 401s -- a request that sent no
// token -- are left alone, because "auth required" there is a real condition
// (e.g. a wrong password on /auth/login), not a stale session.
function handleStaleSession(status: number, headers: HeadersInit): void {
  if (status !== 401) return;
  const sent = new Headers(headers).get("authorization");
  const current = getAuthToken();
  if (current !== null && sent === `Bearer ${current}`) {
    clearAuthToken();
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

export class AuthSessionChangedError extends Error {
  constructor() {
    super("Authentication changed while the request was running");
  }
}

// A 204/205 success carries no body (e.g. password change). Parsing it as
// JSON rejects and reports a failure AFTER the server already acted -- the
// user retries an operation that succeeded.
export async function readJsonResponse<T>(response: Response): Promise<T> {
  if (response.status === 204 || response.status === 205) {
    return undefined as T;
  }
  return response.json() as Promise<T>;
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
  entity: Entity;
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

// One transport for every API call: same-origin path discipline, API-base
// awareness, default accept, JSON content-type only when a body exists, and
// the token-specific 401 handler. `apiResponse` also serves non-JSON callers
// (downloads) that need the same base and status handling.
export async function apiResponse(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new Error("API paths must be same-service absolute paths");
  }
  const url = API_BASE_URL + path;
  const headers = new Headers(init.headers);
  if (!headers.has("accept")) headers.set("accept", "application/json");
  if (typeof init.body === "string" && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  const sent = headers.get("authorization");
  let response: Response;
  try {
    response = await fetch(url, { ...init, headers });
  } catch (cause) {
    if (init.signal?.aborted) throw cause;
    throw new ApiUnavailableError(url, cause);
  }
  if (sent !== null && sent !== `Bearer ${getAuthToken()}`) {
    // The session was replaced while this request ran; applying its success
    // would populate another session's state.
    throw new AuthSessionChangedError();
  }
  if (!response.ok) {
    handleStaleSession(response.status, headers);
    const body = await response.text().catch(() => "");
    throw new ApiHttpError(response.status, url, body);
  }
  return response;
}

export async function request<T>(
  path: string,
  headers: Record<string, string> = {},
): Promise<T> {
  return readJsonResponse<T>(await apiResponse(path, { headers }));
}

export async function sendJson<T>(
  method: string,
  path: string,
  body?: unknown,
  headers: Record<string, string> = {},
): Promise<T> {
  // A bodiless request must not declare a JSON content-type: the server
  // parses a request that claims a body, and a DELETE with content-type
  // but no body is malformed before the route ever runs (observed as
  // every DELETE returning 400 while the same call at the HTTP level
  // succeeded -- bulletin removal, 2026-08-22).
  return readJsonResponse<T>(
    await apiResponse(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}

export function authHeaderIfPresent(): Record<string, string> {
  const token = getAuthToken();
  return token ? { authorization: `Bearer ${token}` } : {};
}

export const searchEntities = (q: string): Promise<EntitiesResponse> =>
  request<EntitiesResponse>(`/entities?q=${encodeURIComponent(q)}`);

export const CREATABLE_KINDS = ["company", "etf", "crypto_asset"] as const;
export type CreatableKind = (typeof CREATABLE_KINDS)[number];

export function createEntity(
  symbol: string,
  kind: CreatableKind,
  name?: string,
): Promise<Entity> {
  return sendJson<Entity>(
    "POST",
    "/entities",
    { symbol, kind, name },
    authHeaderIfPresent(),
  );
}

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

export const getClaims = (id: string, claimType?: string | null): Promise<ClaimsResponse> =>
  request<ClaimsResponse>(
    `/coverage/${encodeURIComponent(id)}/claims${claimType ? `?claim_type=${encodeURIComponent(claimType)}` : ""}`,
    authHeaderIfPresent(),
  );

// The server speaks RFC 7807 problem+json. Its `detail` is written for a
// person; the rest of the envelope is not.
function problemDetail(body: string): string | null {
  try {
    const parsed = JSON.parse(body) as { detail?: unknown; title?: unknown };
    for (const field of [parsed.detail, parsed.title]) {
      if (typeof field === "string" && field.trim()) return field.trim();
    }
  } catch {
    /* not problem+json */
  }
  return null;
}

function statusMessage(status: number): string {
  // 403 is separate from 401 on purpose. This API never issues one -- it
  // answers 404 where a lesser design would say "exists, but not yours" -- so a
  // 403 reaching here came from something in front of it, and telling the
  // reader to sign in would be a guess about a layer we do not control.
  if (status === 401) return "You are not signed in.";
  if (status === 403) return "That is not allowed.";
  if (status === 404) return "That is not here.";
  if (status === 429) return "Too many requests — wait a moment.";
  if (status >= 500) return "The server could not answer.";
  return "That request was not accepted.";
}

// What a person should read when something fails. The previous version put the
// message from an Error subclass straight on screen -- "API responded 500 from
// http://localhost:8000/briefing" -- which names an internal host and a status
// code and tells the reader nothing they can act on. The raw text stays on the
// error object for the console; only the human sentence is returned here.
export function describeError(err: unknown): {
  message: string;
  detail?: string;
} {
  if (err instanceof ApiUnavailableError) {
    return {
      message: "Could not reach the server",
      detail:
        "Nothing is being shown rather than something guessed. Check your connection and try again.",
    };
  }
  if (err instanceof ApiHttpError) {
    const fromServer = problemDetail(err.body);
    return {
      message: statusMessage(err.status),
      detail: fromServer ?? undefined,
    };
  }
  return { message: "Something went wrong." };
}
