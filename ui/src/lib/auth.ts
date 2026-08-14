import { getAuthToken, request, sendJson } from "./api";

export {
  AUTH_STATE_EVENT,
  AUTH_TOKEN_KEY,
  clearAuthToken,
  getAuthToken,
  setAuthToken,
} from "./api";

export class AuthRequiredError extends Error {
  constructor() {
    super("Authentication required");
    this.name = "AuthRequiredError";
  }
}

export interface AuthUser {
  id: string;
  email: string;
  created_at: string | null;
  active: boolean;
}

export interface LoginResponse {
  token: string;
  token_type: string;
  expires_in: number;
}

// The authed helpers guard on a present token (a watchlist is private to its
// owner; an absent token is a 401, never a silent anonymous read) and delegate
// to the shared GET/POST cores in api.ts so the fetch+error mapping lives once.
export async function authedGetJson<T>(path: string): Promise<T> {
  const token = getAuthToken();
  if (!token) throw new AuthRequiredError();
  return request<T>(path, { authorization: `Bearer ${token}` });
}

export async function authedSendJson<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const token = getAuthToken();
  if (!token) throw new AuthRequiredError();
  return sendJson<T>(method, path, body, { authorization: `Bearer ${token}` });
}

// Login and register are anonymous POSTs (no token yet). /auth/register returns
// the user dict (no token); the LoginView follows a successful register with a
// login call to obtain one, so the caller always ends with a stored token.
export const register = (email: string, password: string): Promise<AuthUser> =>
  sendJson<AuthUser>("POST", "/auth/register", { email, password });

export const login = (email: string, password: string): Promise<LoginResponse> =>
  sendJson<LoginResponse>("POST", "/auth/login", { email, password });

export interface SetupStatus {
  setup_required: boolean;
}

export interface SetupResponse extends LoginResponse {
  user: AuthUser;
}

// setup-status is anonymous (the UI needs it before any identity exists to
// pick the redirect target); setup is the one-shot first-run operator
// provisioning that the backend refuses once any user exists.
export const fetchSetupStatus = (): Promise<SetupStatus> =>
  request<SetupStatus>("/auth/setup-status");

export const setup = (
  email: string,
  password: string,
): Promise<SetupResponse> =>
  sendJson<SetupResponse>("POST", "/auth/setup", { email, password });
