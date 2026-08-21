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

import {
  describeEdgeMonitor,
  formatExcessBps,
  formatP,
  type EdgeBookState,
  type EdgeMonitor,
} from "./research";

function edgeBook(overrides: Partial<EdgeBookState> = {}): EdgeBookState {
  return {
    book: "etf_tsmom_252",
    promoted: true,
    state: "insufficient",
    scored_sessions: 0,
    recent_sessions: 0,
    mean_session_excess: null,
    decay_p: null,
    window_start: null,
    window_end: null,
    reason: "0 scored outcome(s) against the 30 this monitor needs",
    as_of: "2026-08-18",
    evaluated_at: "2026-08-19T01:30:00+00:00",
    ...overrides,
  };
}

function monitor(overrides: Partial<EdgeMonitor> = {}): EdgeMonitor {
  return { books: [edgeBook()], alerts: [], ...overrides };
}

describe("formatExcessBps", () => {
  it("keeps absent absent rather than printing a zero that was never measured", () => {
    expect(formatExcessBps(null)).toBe(ABSENT);
    expect(formatExcessBps(undefined)).toBe(ABSENT);
    expect(formatExcessBps(Number.NaN)).toBe(ABSENT);
  });

  it("converts session excess to basis points", () => {
    expect(formatExcessBps(0.0004)).toBe("4.0 bps");
    expect(formatExcessBps(-0.0031)).toBe("-31.0 bps");
  });
});

describe("formatP", () => {
  it("keeps an unmeasured p absent", () => {
    expect(formatP(null)).toBe(ABSENT);
  });

  it("prints a probability to four places", () => {
    expect(formatP(0.0005)).toBe("0.0005");
  });
});

describe("describeEdgeMonitor", () => {
  it("says when nothing is being watched yet", () => {
    expect(describeEdgeMonitor(monitor({ books: [] }))).toBe(
      "No live rule is being watched yet.",
    );
  });

  it("reports an accumulating forward record without claiming decay", () => {
    expect(describeEdgeMonitor(monitor())).toBe(
      "1 live rule holds its evidence · none has stopped working · still building the forward record",
    );
  });

  it("names a decayed promoted edge", () => {
    const line = describeEdgeMonitor(
      monitor({
        books: [edgeBook({ state: "decayed" })],
        alerts: ["etf_tsmom_252"],
      }),
    );
    expect(line).toContain("stopped working: etf_tsmom_252");
  });

  it("counts a measuring record separately from an accumulating one", () => {
    const line = describeEdgeMonitor(
      monitor({
        books: [
          edgeBook({ book: "etf_tsmom_252", state: "holding" }),
          edgeBook({ book: "etf_other", state: "insufficient" }),
        ],
      }),
    );
    expect(line).toContain("1 of 2 still building their record");
  });
});
