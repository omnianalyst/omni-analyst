import { describe, expect, it } from "vitest";
import type { RegimeValue, SectorEntry } from "./autonomous";
import {
  aggregateScorecard,
  regimeBadges,
  regimeMetrics,
  regimeTone,
  topReason,
  topSectors,
} from "./console";
import type { RefusalCounts, ScorecardRow } from "./briefing";

function regime(overrides: Partial<RegimeValue> = {}): RegimeValue {
  return {
    cycle_phase: "expansion",
    risk_regime: "risk_on",
    inflation_regime: "cooling",
    inflation_yoy: 2.1,
    recession_probability: 0.12,
    recession_assessment: "low",
    policy_stance: "dovish",
    yield_curve_inverted: false,
    yield_curve_spread: 0.5,
    sahm_triggered: false,
    sahm_indicator: 0.1,
    lei_negative: false,
    lei_change_6m: 1.2,
    output_gap: 0.4,
    ...overrides,
  };
}

function row(overrides: Partial<ScorecardRow> = {}): ScorecardRow {
  return {
    method: "m",
    surfaced: 0,
    resolved: 0,
    hits: 0,
    hit_rate: null,
    ...overrides,
  };
}

describe("regimeTone", () => {
  it("classifies the positive regime states", () => {
    expect(regimeTone("expansion")).toBe("pos");
    expect(regimeTone("risk_on")).toBe("pos");
    expect(regimeTone("cooling")).toBe("pos");
    expect(regimeTone("dovish")).toBe("pos");
  });

  it("classifies the negative regime states", () => {
    expect(regimeTone("contraction")).toBe("neg");
    expect(regimeTone("risk_off")).toBe("neg");
    expect(regimeTone("hawkish")).toBe("neg");
  });

  // An unrecognized state must render neutral, never guessed into pos/neg.
  it("renders an unknown state as neutral", () => {
    expect(regimeTone("stagflation_lite")).toBe("");
  });

  it("is case-insensitive", () => {
    expect(regimeTone("Expansion")).toBe("pos");
  });
});

describe("regimeBadges", () => {
  it("emits the four categorical calls with tones", () => {
    const badges = regimeBadges(regime());
    expect(badges.map((b) => b.label)).toEqual(["phase", "risk", "inflation", "policy"]);
    expect(badges[0]).toEqual({ label: "phase", value: "expansion", tone: "pos" });
  });

  it("humanizes the snake_case risk regime for display", () => {
    const badges = regimeBadges(regime({ risk_regime: "risk_off" }));
    expect(badges[1].value).toBe("risk off");
  });
});

describe("regimeMetrics", () => {
  it("formats recession probability and CPI as percentages", () => {
    const m = regimeMetrics(regime({ recession_probability: 0.07, inflation_yoy: 3.25 }));
    expect(m[0].value).toBe("7%");
    expect(m[1].value).toBe("3.3%");
  });

  it("renders a dash for a missing yield spread rather than a fabricated number", () => {
    const m = regimeMetrics(regime({ yield_curve_spread: null }));
    expect(m[2].value).toBe("\u2014");
  });
});

describe("aggregateScorecard", () => {
  it("pools hits and resolved across methods into one rate", () => {
    const summary = aggregateScorecard([
      row({ resolved: 10, hits: 6, surfaced: 20 }),
      row({ resolved: 5, hits: 4, surfaced: 8 }),
    ]);
    expect(summary.resolved).toBe(15);
    expect(summary.hits).toBe(10);
    expect(summary.surfaced).toBe(28);
    expect(summary.rate).toBeCloseTo(10 / 15, 5);
  });

  // The honesty invariant: with nothing resolved, there is no rate. Returning 0
  // would render as "0%" and read as a measured failure; null renders as "not
  // yet calibrated".
  it("reports a null rate when nothing has resolved", () => {
    const summary = aggregateScorecard([row({ resolved: 0, hits: 0, surfaced: 5 })]);
    expect(summary.rate).toBeNull();
  });

  it("sums an empty scorecard to zeros with a null rate", () => {
    const summary = aggregateScorecard([]);
    expect(summary).toEqual({ surfaced: 0, resolved: 0, hits: 0, rate: null });
  });
});

describe("topReason", () => {
  it("returns the highest-count refusal reason", () => {
    const counts: RefusalCounts = {
      confidence_below_the_calibrated_threshold: 4,
      class_has_too_few_resolved_predictions: 9,
      no_disconfirming_evidence_was_gathered: 2,
    };
    expect(topReason(counts)).toEqual({
      reason: "class_has_too_few_resolved_predictions",
      n: 9,
    });
  });

  it("returns null when nothing has been refused", () => {
    expect(topReason({})).toBeNull();
  });

  it("breaks ties deterministically by first-seen order", () => {
    const counts: RefusalCounts = { a: 3, b: 3 };
    expect(topReason(counts)?.reason).toBe("a");
  });
});

describe("topSectors", () => {
  function sector(symbol: string, rs: number): SectorEntry {
    return {
      symbol,
      name: symbol,
      score: {
        rs_percentile: rs,
        trend: "uptrend",
        macro_alignment: "favorable",
        cycle_phase: null,
        return_window: 0,
        etf_symbol: symbol,
      },
    };
  }

  it("orders by relative strength and caps to the requested count", () => {
    const top = topSectors([sector("A", 0.2), sector("B", 0.9), sector("C", 0.5)], 2);
    expect(top.map((s) => s.symbol)).toEqual(["B", "C"]);
  });

  // A missing percentile must not float to the top via NaN arithmetic; it ranks
  // last, as if the scanner had no signal for it.
  it("ranks a missing percentile last, never first", () => {
    const top = topSectors([sector("A", NaN), sector("B", 0.1)], 2);
    expect(top[0].symbol).toBe("B");
  });
});
