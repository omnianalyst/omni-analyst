import { describe, expect, it } from "vitest";
import {
  chainSteps,
  confidenceWord,
  directionGlyph,
  directionWord,
  hitRateFelt,
  invalidationLevel,
  oddsOfWrong,
  regimeHeadline,
  regimeImplication,
} from "./explain";
import type { DeductionLayer } from "./briefing";

describe("confidenceWord", () => {
  it("buckets confidence into felt words at the right thresholds", () => {
    expect(confidenceWord(0.2)).toBe("low conviction");
    expect(confidenceWord(0.39)).toBe("low conviction");
    expect(confidenceWord(0.4)).toBe("moderate conviction");
    expect(confidenceWord(0.59)).toBe("moderate conviction");
    expect(confidenceWord(0.6)).toBe("high conviction");
    expect(confidenceWord(0.74)).toBe("high conviction");
    expect(confidenceWord(0.75)).toBe("very high conviction");
    expect(confidenceWord(null)).toBe("uncalibrated");
  });
});

describe("oddsOfWrong", () => {
  it("expresses the measured hit rate as inverse odds so a novice reads the loss rate", () => {
    // 0.74 -> 1/(1-0.74) = ~3.8 -> round 4 -> "~1 in 4 calls like this are wrong"
    expect(oddsOfWrong(0.74)).toBe("~1 in 4 calls like this are wrong");
    expect(oddsOfWrong(0.5)).toBe("~1 in 2 calls like this are wrong");
  });
  it("returns null at the extremes so the card never prints a misleading odds", () => {
    expect(oddsOfWrong(0.97)).toBeNull();
    expect(oddsOfWrong(0.03)).toBeNull();
    expect(oddsOfWrong(null)).toBeNull();
  });
  it("agrees with hitRateFelt, because they must describe the same fact", () => {
    // The bug this catches: the card fed oddsOfWrong the model's own confidence
    // while hitRateFelt showed the measured rate, so a finding with confidence
    // 0.51 and a measured 0.85 rendered "~1 in 2 wrong" directly beside "right
    // ~85% of the time". Both strings derive from one input or they can lie
    // about the same call in a single glance.
    for (const rate of [0.5, 0.68, 0.74, 0.85, 0.9]) {
      const odds = oddsOfWrong(rate);
      const felt = hitRateFelt(rate);
      expect(odds).not.toBeNull();
      const impliedWrong = Number(/1 in (\d+)/.exec(odds!)![1]);
      const statedRight = Number(/~(\d+)%/.exec(felt)![1]) / 100;
      // 1/impliedWrong is the wrong-rate; it must match 1 - statedRight to
      // within the rounding both strings apply.
      expect(Math.abs(1 / impliedWrong - (1 - statedRight))).toBeLessThan(0.06);
    }
  });

  it("does not dress a losing class up as a coin flip", () => {
    // 1/(1-0.35) rounds to 2, so the "1 in N" form would print "~1 in 2 wrong"
    // for a class that actually loses about two times in three. A majority
    // cannot be expressed as 1-in-N, so below half the sentence changes shape.
    expect(oddsOfWrong(0.35)).toBe("wrong more often than right");
    expect(oddsOfWrong(0.45)).toBe("wrong more often than right");
    expect(oddsOfWrong(0.5)).toBe("~1 in 2 calls like this are wrong");
  });
});

describe("invalidationLevel", () => {
  it("uses the lower barrier for an up call (trend breaks below) and upper for down", () => {
    expect(invalidationLevel("up", 190, 155)).toBe(155);
    expect(invalidationLevel("down", 190, 155)).toBe(190);
  });
  it("returns null when the relevant barrier is absent, never invents a level", () => {
    expect(invalidationLevel("up", 190, null)).toBeNull();
    expect(invalidationLevel(null, 190, 155)).toBeNull();
  });
});

describe("direction", () => {
  it("renders a glyph and a word for each direction", () => {
    expect(directionGlyph("up")).toBe("\u25B2");
    expect(directionGlyph("down")).toBe("\u25BC");
    expect(directionWord("down")).toBe("down");
    expect(directionWord(null)).toBe("neutral");
  });
});

describe("regime renderers", () => {
  it("headlines the cycle + risk and stays honest when no regime exists", () => {
    expect(regimeHeadline({ cycle_phase: "expansion", risk_regime: "risk_on" })).toBe(
      "Markets: expansion, risk on",
    );
    expect(regimeHeadline(null)).toBe("Market regime is still being assessed");
  });
  it("implications are generic and never advice-shaped (no 'should')", () => {
    const expansion = regimeImplication({ cycle_phase: "expansion", risk_regime: "risk_on" });
    expect(expansion).toMatch(/historically/);
    expect(expansion).not.toMatch(/should/);
    const contraction = regimeImplication({ cycle_phase: "contraction", risk_regime: "risk_off" });
    expect(isAdviceFree(contraction)).toBe(true);
  });
});

// guard: a string must not contain the regulated-advice verb. Kept as a helper
// so the intent is named at the call site.
function isAdviceFree(text: string): boolean {
  return !/should\b/i.test(text);
}

describe("chainSteps", () => {
  it("renders the macro -> sector -> stock chain as ordered plain-English steps", () => {
    const chain: DeductionLayer[] = [
      { layer: "macro", cycle_phase: "expansion", risk_regime: "risk_on" },
      { layer: "sector", etf_symbol: "XLK", trend: "up", macro_alignment: "favorable" },
      { layer: "stock", direction: "up", confidence: 0.74 },
    ];
    const steps = chainSteps(chain);
    expect(steps.map((s) => s.layer)).toEqual(["macro", "sector", "stock"]);
    expect(steps[0].text).toBe("economy expansion, risk on");
    expect(steps[1].text).toBe("XLK · up · macro-favorable");
    expect(steps[2].text).toBe("up, high conviction");
  });
  it("returns [] for an empty chain rather than a fabricated step", () => {
    expect(chainSteps([])).toEqual([]);
    expect(chainSteps(undefined)).toEqual([]);
  });
});

describe("hitRateFelt", () => {
  it("phrases the track record as a frequency, and is honest when uncalibrated", () => {
    expect(hitRateFelt(0.731)).toBe("right ~73% of the time on calls like this");
    expect(hitRateFelt(null)).toBe("not yet calibrated");
  });
});
