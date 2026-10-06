import { clearAuthToken, getAuthToken, request, sendJson } from "./api";

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

// Requires the current password; a wrong one renders identically to a wrong
// login so the endpoint cannot confirm guesses. On success the backend has
// revoked every session including this browser's: clearing the stored token
// and navigating away is not optional cleanup -- a client that keeps the
// now-dead token shows "password changed" while its next protected call
// 401s, and in-memory private state lingers with it. A full navigation to
// the login screen (with the reason shown) is the honest end state.
export async function changePassword(
  oldPassword: string,
  newPassword: string,
): Promise<void> {
  await authedSendJson<void>("POST", "/auth/change-password", {
    old_password: oldPassword,
    new_password: newPassword,
  });
  clearAuthToken();
  window.location.replace("/login?reason=password-changed");
}

// Logout-all revokes every session server-side; the browser's stored token
// dies with the rest of them and must not survive in localStorage.
export async function logoutAll(): Promise<void> {
  await authedSendJson<void>("POST", "/auth/logout-all");
  clearAuthToken();
  window.location.replace("/login?reason=sessions-revoked");
}

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
