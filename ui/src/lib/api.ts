import { API_BASE_URL } from "../config";

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

export const searchEntities = (q: string): Promise<EntitiesResponse> =>
  getJson<EntitiesResponse>(`/entities?q=${encodeURIComponent(q)}`);

export const getCoverage = (id: string): Promise<CoverageResponse> =>
  getJson<CoverageResponse>(`/coverage/${encodeURIComponent(id)}`);

export const getGaps = (id: string): Promise<GapsResponse> =>
  getJson<GapsResponse>(`/gaps/${encodeURIComponent(id)}`);

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
