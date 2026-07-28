import { describe, expect, it } from "vitest";
import {
  explainLicenceTier,
  explainShortfall,
  formatCost,
  parseNeeds,
} from "./objective";

describe("formatCost", () => {
  it("formats a fractional cost to two decimals", () => {
    expect(formatCost(1.5)).toBe("1.50");
    expect(formatCost(0.25)).toBe("0.25");
  });

  it("formats a whole-number total without dropping the decimals", () => {
    expect(formatCost(10)).toBe("10.00");
  });

  it("keeps zero visible rather than rendering an empty cell", () => {
    expect(formatCost(0)).toBe("0.00");
  });

  it("renders a dash for a missing cost instead of fabricating 0.00", () => {
    expect(formatCost(null)).toBe("\u2014");
    expect(formatCost(undefined)).toBe("\u2014");
    expect(formatCost(NaN)).toBe("\u2014");
  });
});

describe("explainShortfall", () => {
  it("turns a missing producer into a plain-language remedy", () => {
    const s = explainShortfall("no_capability_produces_this");
    expect(s).toMatch(/new adapter/);
    expect(s).not.toBe("no_capability_produces_this");
  });

  it("names the shareable/licence constraint behind an only-licensed refusal", () => {
    const s = explainShortfall("only_licensed_sources_can_produce_this");
    expect(s).toMatch(/shareable|licen[cs]e/i);
  });

  it("names the budget behind an over-budget shortfall", () => {
    const s = explainShortfall("cheapest_viable_plan_exceeds_budget");
    expect(s).toMatch(/budget/);
  });

  it("passes an unknown reason through verbatim rather than inventing one", () => {
    expect(explainShortfall("some_new_constraint")).toBe("some_new_constraint");
  });
});

describe("explainLicenceTier", () => {
  it("marks a private step as BYO-licensed and owner-scoped", () => {
    expect(explainLicenceTier("private")).toMatch(/BYO|only to you/i);
  });

  it("marks a shared step as redistributable", () => {
    expect(explainLicenceTier("shared")).toMatch(/redistribut/i);
  });
});

describe("parseNeeds", () => {
  it("splits and trims comma-separated claim types", () => {
    expect(parseNeeds(" price.close, market_cap , eps ")).toEqual([
      "price.close",
      "market_cap",
      "eps",
    ]);
  });

  it("drops empty entries so a trailing comma does not plan an empty need", () => {
    expect(parseNeeds("price.close,")).toEqual(["price.close"]);
    expect(parseNeeds("")).toEqual([]);
  });
});
