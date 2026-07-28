import { describe, expect, it } from "vitest";
import {
  ApiHttpError,
  ApiUnavailableError,
  describeError,
} from "./api";

describe("describeError", () => {
  it("explains a network failure honestly rather than implying no coverage", () => {
    const err = new ApiUnavailableError(
      "http://localhost:8000/coverage/abc",
      new Error("fetch failed"),
    );
    const out = describeError(err);
    expect(out.message).toBe("Could not reach the API at http://localhost:8000/coverage/abc");
    expect(out.detail).toMatch(/did not answer/);
    expect(out.detail).not.toMatch(/no coverage/i);
  });

  it("surfaces the HTTP status for a non-ok response", () => {
    const err = new ApiHttpError(404, "http://localhost:8000/coverage/abc", "Not found");
    const out = describeError(err);
    expect(out.message).toBe("API responded 404 from http://localhost:8000/coverage/abc");
    expect(out.detail).toContain("404");
  });

  it("falls back to the error message for unknown errors", () => {
    const out = describeError(new Error("boom"));
    expect(out.message).toBe("boom");
    expect(out.detail).toBeUndefined();
  });
});
