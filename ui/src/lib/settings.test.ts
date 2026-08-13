import { describe, expect, it } from "vitest";
import { blockedReason, canEnable, describeSource, type VenueEntry } from "./settings";

function venue(overrides: Partial<VenueEntry> = {}): VenueEntry {
  return {
    key: "questrade",
    label: "Questrade (Read-Only)",
    type: "equity",
    requires_process: false,
    description: "Canadian broker.",
    fields: [{ name: "refresh_token", label: "Refresh Token", type: "password", required: true }],
    configured: false,
    enabled: false,
    configuration_source: "unavailable",
    ...overrides,
  };
}

describe("describeSource", () => {
  it("names a plaintext row as an action, not a neutral state", () => {
    const out = describeSource(venue({ configured: true, configuration_source: "legacy" }));

    expect(out.tone).toBe("warn");
    expect(out.label).toBe("Stored in plain text");
    expect(out.detail).toMatch(/re-enter/);
  });

  it("says encrypted storage plainly", () => {
    const out = describeSource(venue({ configured: true, configuration_source: "encrypted" }));

    expect(out.tone).toBe("ok");
    expect(out.detail).toMatch(/[Ee]ncrypted at rest/);
  });

  it("tells the operator a deployment venue cannot be set from the browser", () => {
    const out = describeSource(
      venue({ key: "hyperliquid", configured: false, configuration_source: "deployment" }),
    );

    expect(out.detail).toMatch(/cannot be set from the browser/);
  });

  it("distinguishes a configured deployment venue from an unconfigured one", () => {
    const on = describeSource(venue({ configured: true, configuration_source: "deployment" }));
    const off = describeSource(venue({ configured: false, configuration_source: "deployment" }));

    expect(on.label).toBe("Deployment-managed");
    expect(off.label).toBe("Not configured");
  });
});

describe("canEnable / blockedReason", () => {
  it("refuses to offer a toggle for a venue with no credentials", () => {
    const entry = venue();

    expect(canEnable(entry)).toBe(false);
    expect(blockedReason(entry)).toBe("Add credentials first");
  });

  it("points at the deployment environment when that is where the gap is", () => {
    const entry = venue({ key: "hyperliquid", configuration_source: "deployment" });

    expect(blockedReason(entry)).toBe("Waiting on deployment secrets");
  });

  it("allows the toggle once credentials exist", () => {
    const entry = venue({ configured: true, configuration_source: "encrypted" });

    expect(canEnable(entry)).toBe(true);
    expect(blockedReason(entry)).toBeNull();
  });
});
