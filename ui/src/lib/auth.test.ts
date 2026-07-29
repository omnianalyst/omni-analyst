import { describe, expect, it } from "vitest";
import { AUTH_TOKEN_KEY, getAuthToken } from "./auth";

describe("AUTH_TOKEN_KEY", () => {
  it("is a stable, documented storage key so an operator can set a token by hand", () => {
    expect(AUTH_TOKEN_KEY).toBe("omni.auth.token");
    expect(typeof AUTH_TOKEN_KEY).toBe("string");
  });
});

describe("getAuthToken", () => {
  it("returns null honestly when no token is stored (node has no localStorage)", () => {
    expect(getAuthToken()).toBeNull();
  });

  it("never throws when localStorage is unavailable, so the UI can render the auth-required state instead of crashing", () => {
    expect(() => getAuthToken()).not.toThrow();
  });
});
