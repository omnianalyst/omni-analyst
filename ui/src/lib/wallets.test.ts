import { describe, expect, it } from "vitest";
import { familyName, shortAddress, sourceName } from "./wallets";

describe("wallet presentation", () => {
  it("shortens long public addresses without hiding both ends", () => {
    expect(shortAddress("0x1234567890abcdef1234567890abcdef12345678"))
      .toBe("0x12345…345678");
  });

  it("keeps short identifiers intact", () => {
    expect(shortAddress("short-address")).toBe("short-address");
  });

  it("uses human network and source names", () => {
    expect(familyName("evm")).toBe("Ethereum");
    expect(sourceName("metamask")).toBe("MetaMask");
  });
});
