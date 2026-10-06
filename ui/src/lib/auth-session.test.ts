// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AUTH_TOKEN_KEY, setAuthToken } from "./api";
import { changePassword, logoutAll } from "./auth";

// A password change revokes every session server-side, including this
// browser's own token (A08). The UI used to report success and keep the
// now-dead token: its next protected call 401'd while public endpoints
// still rendered an anonymous view. These tests pin the honest end state:
// a successful rotation clears storage and navigates to login; a failed
// one does neither -- one typo must not log the operator out.

describe("changePassword session handling", () => {
  const realFetch = globalThis.fetch;
  let replace: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    localStorage.removeItem(AUTH_TOKEN_KEY);
    replace = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, replace },
    });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    globalThis.fetch = realFetch;
    localStorage.removeItem(AUTH_TOKEN_KEY);
  });

  it("clears the stored token and navigates to login on success", async () => {
    setAuthToken("revoked-token");
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    await changePassword("old-password-1", "new-password-2");
    expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBeNull();
    expect(replace).toHaveBeenCalledWith("/login?reason=password-changed");
  });

  it("keeps the session and does not navigate on a wrong-password 400", async () => {
    setAuthToken("still-valid-token");
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "Current password is incorrect" }), {
        status: 400,
      }),
    );
    await expect(
      changePassword("wrong-old-password", "new-password-2"),
    ).rejects.toThrow();
    expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBe("still-valid-token");
    expect(replace).not.toHaveBeenCalled();
  });

  it("sends the stored bearer token with the rotation request", async () => {
    setAuthToken("bearer-under-test");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    globalThis.fetch = fetchMock;
    await changePassword("old-password-1", "new-password-2");
    const [, init] = fetchMock.mock.calls[0];
    expect(new Headers(init?.headers).get("authorization")).toBe(
      "Bearer bearer-under-test",
    );
  });
});

describe("logoutAll session handling", () => {
  const realFetch = globalThis.fetch;
  let replace: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    localStorage.removeItem(AUTH_TOKEN_KEY);
    replace = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, replace },
    });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    globalThis.fetch = realFetch;
    localStorage.removeItem(AUTH_TOKEN_KEY);
  });

  it("clears the stored token and navigates to login on success", async () => {
    setAuthToken("revoked-everywhere");
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    await logoutAll();
    expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBeNull();
    expect(replace).toHaveBeenCalledWith("/login?reason=sessions-revoked");
  });
});
