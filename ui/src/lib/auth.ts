import { API_BASE_URL } from "../config";
import { ApiHttpError, ApiUnavailableError, getAuthToken } from "./api";

export { AUTH_TOKEN_KEY, getAuthToken } from "./api";

export class AuthRequiredError extends Error {
  constructor() {
    super("Authentication required");
    this.name = "AuthRequiredError";
  }
}

export async function authedGetJson<T>(path: string): Promise<T> {
  const token = getAuthToken();
  if (!token) throw new AuthRequiredError();
  const url = API_BASE_URL + path;
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { accept: "application/json", authorization: `Bearer ${token}` },
    });
  } catch (err) {
    throw new ApiUnavailableError(url, err);
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new ApiHttpError(res.status, url, body);
  }
  return res.json() as Promise<T>;
}

export async function authedSendJson<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const token = getAuthToken();
  if (!token) throw new AuthRequiredError();
  const url = API_BASE_URL + path;
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: {
        accept: "application/json",
        "content-type": "application/json",
        authorization: `Bearer ${token}`,
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
