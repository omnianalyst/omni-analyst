import { describe, expect, it } from "vitest";
import {
  ApiHttpError,
  ApiUnavailableError,
  describeError,
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
