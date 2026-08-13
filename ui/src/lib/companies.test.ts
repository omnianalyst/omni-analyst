import { describe, expect, it } from "vitest";
import { ABSENT, describeCoverage, percent, tierRows, tone } from "./companies";

describe("tierRows", () => {
  it("renders an unreached tier as a visible zero, not an omitted row", () => {
    const rows = tierRows({ low: 3, medium: 40 });
    const high = rows.find((r) => r.tier === "high");

    expect(high).toEqual({ tier: "high", count: 0 });
    expect(rows.map((r) => r.tier)).toEqual(["low", "medium", "high", "unrated"]);
  });

  it("survives a missing census without inventing counts", () => {
    expect(tierRows(undefined).every((r) => r.count === 0)).toBe(true);
  });
});

describe("percent", () => {
  it("is absent rather than zero when unmeasured", () => {
    expect(percent(null)).toBe(ABSENT);
    expect(percent(Number.NaN)).toBe(ABSENT);
  });

  it("keeps a measured zero distinct from absent", () => {
    expect(percent(0)).toBe("0.0%");
  });

  it("signs a gain", () => {
    expect(percent(12.34)).toBe("+12.3%");
    expect(percent(-4.5)).toBe("-4.5%");
  });
});

describe("tone", () => {
  it("is neutral for unmeasured and for exactly zero", () => {
    expect(tone(null)).toBe("");
    expect(tone(0)).toBe("");
  });

  it("signs a direction when there is one", () => {
    expect(tone(1)).toBe("positive");
    expect(tone(-1)).toBe("negative");
  });
});

describe("describeCoverage", () => {
  it("names what was held back rather than quietly dropping it", () => {
    expect(
      describeCoverage({ measured: 480, with_prices: 505, too_thin: 25, min_sessions: 60 }),
    ).toBe("480 of 505 companies ranked, 25 held back for fewer than 60 sessions.");
  });

  it("omits the held-back clause when nothing was", () => {
    expect(
      describeCoverage({ measured: 505, with_prices: 505, too_thin: 0, min_sessions: 60 }),
    ).toBe("505 of 505 companies ranked.");
  });

  it("says plainly when nothing is stored", () => {
    expect(
      describeCoverage({ measured: 0, with_prices: 0, too_thin: 0, min_sessions: 60 }),
    ).toBe("No company price history is stored yet.");
  });
});
