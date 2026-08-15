import { describe, expect, it } from "vitest";
import { equalWeightAverage, riskShares, weightShare } from "./blend";

describe("equalWeightAverage", () => {
  it("averages percent-unit values without rescaling them", () => {
    // The four live regime picks measured 2026-08-14: XLK 34.26, GLD 12.81,
    // BND ~4.0, SGOV 4.24. A second x100 rendered this as +1434.5% on the
    // deployed page; the honest figure is ~14%.
    const blend = equalWeightAverage([34.26, 12.81, 4.0, 4.24]);

    expect(blend).toBeCloseTo(13.83, 2);
    expect(blend).toBeLessThan(100);
  });

  it("skips absent values rather than pricing them as zero contributions", () => {
    expect(equalWeightAverage([10, null, undefined, 30])).toBe(20);
  });

  it("returns 0 for an empty portfolio, not NaN", () => {
    expect(equalWeightAverage([])).toBe(0);
  });
});

describe("weightShare", () => {
  it("splits equally", () => {
    expect(weightShare(4)).toBeCloseTo(0.25);
  });

  it("refuses a divide-by-zero for an empty portfolio", () => {
    expect(weightShare(0)).toBe(0);
  });
});

describe("riskShares", () => {
  it("splits risk by measured volatility", () => {
    // Measured 2026-08-14: VTI ~18, GLD ~16, TLT ~14, SGOV ~0.2. Equal
    // capital means the weight cancels; the growth sleeve still dominates
    // risk, which is exactly what the strip exists to show.
    const shares = riskShares([18, 16.2, 14, 0.2]);

    expect(shares[0]).toBeCloseTo(18 / 48.4, 4);
    expect(shares.reduce((sum, s) => sum + s, 0)).toBeCloseTo(1, 6);
  });

  it("treats an unmeasured holding as no contribution, not zero-risk", () => {
    const shares = riskShares([20, null]);

    expect(shares).toHaveLength(2);
    expect(shares[1]).toBe(0);
  });

  it("returns empty when nothing is measured", () => {
    expect(riskShares([null, undefined])).toEqual([]);
  });
});
