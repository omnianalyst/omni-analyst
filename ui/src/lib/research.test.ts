import { describe, expect, it } from "vitest";
import {
  ABSENT,
  describeRecord,
  formatT,
  isPass,
  shareOfBar,
  type HypothesisTest,
  type ResearchSummary,
} from "./research";

function test(overrides: Partial<HypothesisTest> = {}): HypothesisTest {
  return {
    name: "trend.sma",
    source: "binance_ohlcv",
    cells: 4,
    verdict: "fail",
    recorded_at: "2026-08-10T22:06:48.402039+00:00",
    detail: { bar: 3.17, best_recent_third_t: 1.93 },
    mirrored_at: "2026-08-12T00:00:00+00:00",
    ...overrides,
  };
}

function summary(overrides: Partial<ResearchSummary> = {}): ResearchSummary {
  return {
    tests: 49,
    cells: 154,
    passed: 0,
    failed: 49,
    bar: 3.17,
    fdr_bar: 2.54,
    best_t: 1.93,
    sources: ["binance_ohlcv"],
    last_recorded_at: "2026-08-10T22:06:48.402039+00:00",
    last_mirrored_at: "2026-08-12T00:00:00+00:00",
    ...overrides,
  };
}

describe("isPass", () => {
  it("reads the harness's uppercase PASS as a pass", () => {
    expect(isPass(test({ verdict: "PASS" }))).toBe(true);
  });

  it("does not treat a failure as a pass", () => {
    expect(isPass(test({ verdict: "fail" }))).toBe(false);
  });
});

describe("formatT", () => {
  it("prints an unmeasured statistic as absent, not as zero", () => {
    expect(formatT(null)).toBe(ABSENT);
    expect(formatT(undefined)).toBe(ABSENT);
    expect(formatT(Number.NaN)).toBe(ABSENT);
  });

  it("keeps a measured zero distinct from absent", () => {
    expect(formatT(0)).toBe("0.00");
  });

  it("formats to two places", () => {
    expect(formatT(1.9345)).toBe("1.93");
  });
});

describe("shareOfBar", () => {
  it("uses the bar the test was actually judged against", () => {
    const value = shareOfBar(test({ detail: { bar: 4, best_recent_third_t: 2 } }), 100);
    expect(value).toBeCloseTo(0.5);
  });

  it("falls back to the current bar only when the test recorded none", () => {
    const value = shareOfBar(test({ detail: { best_recent_third_t: 2 } }), 4);
    expect(value).toBeCloseTo(0.5);
  });

  it("returns null rather than zero when the statistic is missing", () => {
    expect(shareOfBar(test({ detail: {} }), 3.17)).toBeNull();
  });

  it("never implies a failed result exceeded its bar", () => {
    const value = shareOfBar(test({ detail: { bar: 2, best_recent_third_t: 9 } }), 2);
    expect(value).toBe(1);
  });

  it("treats a negative t by magnitude, since the bar is on |t|", () => {
    const value = shareOfBar(test({ detail: { bar: 4, best_recent_third_t: -2 } }), 4);
    expect(value).toBeCloseTo(0.5);
  });
});

describe("describeRecord", () => {
  it("states plainly that nothing cleared the bar", () => {
    expect(describeRecord(summary())).toBe(
      "49 hypotheses tested across 154 statistics. None cleared the bar.",
    );
  });

  it("reports survivors when there are some", () => {
    expect(describeRecord(summary({ passed: 1, failed: 48 }))).toContain("1 cleared the bar");
  });

  it("does not claim a search that has not happened", () => {
    expect(describeRecord(summary({ tests: 0, cells: 0, failed: 0 }))).toBe(
      "No hypothesis has been recorded yet.",
    );
  });

  it("singularises a lone test", () => {
    expect(describeRecord(summary({ tests: 1, cells: 1, failed: 1 }))).toBe(
      "1 hypothesis tested across 1 statistic. None cleared the bar.",
    );
  });
});
