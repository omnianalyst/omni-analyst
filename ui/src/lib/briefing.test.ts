import { describe, expect, it } from "vitest";
import {
  briefingHeading,
  explainRefusal,
  formatConfidence,
  formatHitRate,
  refusalTotal,
} from "./briefing";

describe("formatHitRate", () => {
  it("renders null as 'not yet calibrated', never as 0%", () => {
    expect(formatHitRate(null)).toBe("not yet calibrated");
    expect(formatHitRate(null)).not.toMatch(/0\s?%/);
  });

  it("renders undefined and NaN as 'not yet calibrated'", () => {
    expect(formatHitRate(undefined)).toBe("not yet calibrated");
    expect(formatHitRate(NaN)).toBe("not yet calibrated");
  });

  it("renders a real rate as a rounded percentage", () => {
    expect(formatHitRate(0.6)).toBe("60%");
    expect(formatHitRate(0.6666)).toBe("67%");
    expect(formatHitRate(1)).toBe("100%");
  });

  it("renders a genuine zero rate as 0%, distinct from an uncalibrated null", () => {
    expect(formatHitRate(0)).toBe("0%");
  });
});

describe("formatConfidence", () => {
  it("formats to two decimals", () => {
    expect(formatConfidence(0.7)).toBe("0.70");
  });

  it("renders a dash for a missing value", () => {
    expect(formatConfidence(null)).toBe("\u2014");
  });
});

describe("explainRefusal", () => {
  it("explains the uncalibrated refusal in plain language", () => {
    expect(
      explainRefusal("class_has_too_few_resolved_predictions"),
    ).toMatch(/calibrate|threshold/i);
  });

  it("explains the below-threshold refusal", () => {
    expect(
      explainRefusal("confidence_below_the_calibrated_threshold"),
    ).toMatch(/threshold/);
  });

  it("explains the missing disconfirming search", () => {
    expect(
      explainRefusal("no_disconfirming_evidence_was_gathered"),
    ).toMatch(/disconfirming/);
  });

  it("explains the non-falsifiable refusal", () => {
    expect(
      explainRefusal("no_falsifiable_prediction_could_be_written"),
    ).toMatch(/falsifiable|prediction/);
  });

  it("passes an unknown reason through verbatim", () => {
    expect(explainRefusal("brand_new_reason")).toBe("brand_new_reason");
  });
});

describe("refusalTotal", () => {
  it("sums refusal counts across reasons", () => {
    expect(refusalTotal({ a: 3, b: 7, c: 0 })).toBe(10);
  });

  it("is zero when nothing has been refused", () => {
    expect(refusalTotal({})).toBe(0);
  });
});

describe("briefingHeading", () => {
  it("states honestly that nothing met the bar for an empty feed", () => {
    expect(briefingHeading([])).toBe("Nothing met the bar");
    expect(briefingHeading([])).not.toMatch(/loading|error/i);
  });

  it("counts surfaced calls when the feed is non-empty", () => {
    // "call" is the one noun for what the system surfaces. The UI previously
    // mixed call / finding / pick for the same object; "finding" survives only
    // as the internal table name.
    expect(briefingHeading([{ id: "1" } as never])).toBe("1 call surfaced");
    expect(
      briefingHeading([{ id: "1" } as never, { id: "2" } as never]),
    ).toBe("2 calls surfaced");
  });
});
