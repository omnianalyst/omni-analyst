// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AUTH_TOKEN_KEY,
  ApiHttpError,
  ApiUnavailableError,
  apiResponse,
  describeError,
  readJsonResponse,
  sendJson,
  setAuthToken,
} from "./api";

// describeError produces what a person reads. It used to return the Error
// subclass's own message, which named the internal host and the status code --
// "API responded 500 from http://localhost:8000/briefing" -- telling the reader
// nothing actionable and leaking infrastructure into the surface. These tests
// pin the two properties that matter: it says something a person can act on,
// and it never puts a URL on screen.

describe("describeError", () => {
  it("explains a network failure honestly rather than implying no coverage", () => {
    const err = new ApiUnavailableError(
      "http://localhost:8000/coverage/abc",
      new Error("fetch failed"),
    );
    const out = describeError(err);
    expect(out.message).toBe("Could not reach the server");
    // The distinction the product depends on: unreachable is not the same as
    // empty, and the copy must not let a reader take it as "nothing found".
    expect(out.detail).toMatch(/rather than something guessed/);
    expect(out.detail).not.toMatch(/no coverage/i);
  });

  it("never leaks the request URL into user-facing text", () => {
    const url = "http://localhost:8000/coverage/abc";
    for (const err of [
      new ApiUnavailableError(url, new Error("fetch failed")),
      new ApiHttpError(500, url, "Internal Server Error"),
    ]) {
      const out = describeError(err);
      expect(out.message).not.toContain(url);
      expect(out.message).not.toContain("localhost");
      expect(out.detail ?? "").not.toContain("localhost");
    }
  });

  it("turns an HTTP status into a sentence, not a status line", () => {
    const out = describeError(
      new ApiHttpError(404, "http://localhost:8000/coverage/abc", ""),
    );
    expect(out.message).toBe("That is not here.");

    expect(
      describeError(new ApiHttpError(401, "http://x/y", "")).message,
    ).toBe("You are not signed in.");
    expect(
      describeError(new ApiHttpError(503, "http://x/y", "")).message,
    ).toBe("The server could not answer.");
  });

  it("prefers the server's own problem+json detail, which is written for a person", () => {
    const body = JSON.stringify({
      type: "https://neutron.dev/errors/unauthorized",
      title: "Unauthorized",
      status: 401,
      detail: "Authentication required",
    });
    const out = describeError(new ApiHttpError(401, "http://x/y", body));
    expect(out.detail).toBe("Authentication required");
  });

  it("carries no detail when the body is not problem+json", () => {
    // An HTML error page or a stack trace is not an explanation. Showing a
    // truncated slice of one is worse than showing nothing.
    const out = describeError(
      new ApiHttpError(500, "http://x/y", "<html><body>502 Bad Gateway</body></html>"),
    );
    expect(out.detail).toBeUndefined();
  });

  it("says something generic for an error it does not recognise", () => {
    const out = describeError(new Error("boom"));
    expect(out.message).toBe("Something went wrong.");
    expect(out.detail).toBeUndefined();
  });
});

describe("shared transport", () => {
  const realFetch = globalThis.fetch;
  beforeEach(() => {
    localStorage.removeItem(AUTH_TOKEN_KEY);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    globalThis.fetch = realFetch;
    localStorage.removeItem(AUTH_TOKEN_KEY);
  });

  it("resolves a 204 success as undefined instead of a parse failure (U11)", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    await expect(sendJson("POST", "/x", { a: 1 })).resolves.toBeUndefined();
    await expect(readJsonResponse(new Response(null, { status: 205 }))).resolves.toBeUndefined();
  });

  it("clears only the token that was sent, not a newer session's (U12)", async () => {
    setAuthToken("token-new");
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response("nope", { status: 401 }),
    );
    await expect(
      sendJson("GET", "/x", undefined, { authorization: "Bearer token-old" }),
    ).rejects.toThrow();
    expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBe("token-new");
  });

  it("clears the stored token when THAT token was the rejected one (U12)", async () => {
    setAuthToken("token-a");
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response("nope", { status: 401 }),
    );
    await expect(
      sendJson("GET", "/x", undefined, { authorization: "Bearer token-a" }),
    ).rejects.toThrow();
    expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBeNull();
  });

  it("rejects a success that started under a replaced token (U13/U15)", async () => {
    setAuthToken("token-old");
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      setAuthToken("token-new");
      return new Response("{}", { status: 200 });
    });
    await expect(
      apiResponse("/x", { headers: { authorization: "Bearer token-old" } }),
    ).rejects.toThrow(/Authentication changed/);
  });

  it("prefixes the configured API base (U15)", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn().mockImplementation(async (url: string | URL) => {
      calls.push(String(url));
      return new Response("{}", { status: 200 });
    });
    await sendJson("GET", "/x");
    expect(calls).toHaveLength(1);
    expect(calls[0]).not.toBe("/x");
    expect(calls[0].endsWith("/x")).toBe(true);
  });

  it("refuses non-same-service paths (U15)", async () => {
    await expect(apiResponse("//evil.example/x")).rejects.toThrow(/same-service/);
    await expect(apiResponse("https://evil.example/x")).rejects.toThrow(/same-service/);
  });

  it("never sends a JSON content-type without a body (U15)", async () => {
    let sawHeaders: Headers | null = null;
    globalThis.fetch = vi.fn().mockImplementation(async (_url: string, init?: RequestInit) => {
      sawHeaders = new Headers(init?.headers);
      return new Response("{}", { status: 200 });
    });
    await sendJson("DELETE", "/x");
    expect(sawHeaders!.get("content-type")).toBeNull();
    await sendJson("DELETE", "/x", { a: 1 });
    expect(sawHeaders!.get("content-type")).toBe("application/json");
  });
});
