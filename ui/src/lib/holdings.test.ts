import { describe, expect, it } from "vitest";
import { describeHoldings, type HoldingsSummary } from "./holdings";

function summary(overrides: Partial<HoldingsSummary> = {}): HoldingsSummary {
  return {
    positions: 3,
    priced: 3,
    total_value: 1000,
    total_pnl: 120,
    ...overrides,
  };
}

describe("describeHoldings", () => {
  it("invites the first position rather than showing an empty dashboard", () => {
    expect(describeHoldings(summary({ positions: 0, priced: 0, total_value: null, total_pnl: null })))
      .toContain("No positions tracked yet");
  });

  it("states a fully priced portfolio plainly", () => {
    expect(describeHoldings(summary())).toBe("3 positions valued.");
  });

  it("names an unpriced count and refuses to imply a total exists", () => {
    const line = describeHoldings(summary({ priced: 2, total_value: null, total_pnl: null }));
    expect(line).toContain("2 of 3");
    expect(line).toContain("only when every position has a price");
  });
});
