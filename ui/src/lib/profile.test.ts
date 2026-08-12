import { describe, expect, it } from "vitest";
import {
  ABSENT,
  describeFiling,
  formatMagnitude,
  formatNumber,
  formatPercent,
  sparklinePath,
  toneFor,
  type EntityProfile,
} from "./profile";

type Fundamental = EntityProfile["fundamentals"][number];

function filing(overrides: Partial<Fundamental> = {}): Fundamental {
  return {
    key: "Revenues",
    label: "Revenue",
    value: 25_000_000_000,
    unit: "USD",
    higher_is_better: true,
    period_end: "2026-06-30",
    knowable_from: "2026-07-23",
    fiscal_period: "Q2",
    fiscal_year: 2026,
    form: "10-Q",
    source: "sec_edgar",
    ...overrides,
  };
}

describe("formatMagnitude", () => {
  it("prints an unmeasured value as absent, never as zero", () => {
    expect(formatMagnitude(null)).toBe(ABSENT);
    expect(formatMagnitude(undefined)).toBe(ABSENT);
    expect(formatMagnitude(Number.NaN)).toBe(ABSENT);
  });

  it("keeps a measured zero distinct from absent", () => {
    expect(formatMagnitude(0, "USD")).toBe("$0.00");
  });

  it("scales to the magnitude a reader thinks in", () => {
    expect(formatMagnitude(1_480_000_000_000, "USD")).toBe("$1.48T");
    expect(formatMagnitude(25_000_000_000, "USD")).toBe("$25.00B");
    expect(formatMagnitude(1_524_000_000, "USD")).toBe("$1.52B");
    expect(formatMagnitude(216_859_000, "USD")).toBe("$216.86M");
  });

  it("keeps the sign on a loss", () => {
    expect(formatMagnitude(-128_304_000, "USD")).toBe("-$128.30M");
  });

  it("omits the currency prefix for a non-USD unit", () => {
    expect(formatMagnitude(3_210_000_000, "shares")).toBe("3.21B");
  });
});

describe("formatPercent", () => {
  it("signs a gain and leaves a loss with its own minus", () => {
    expect(formatPercent(12.345)).toBe("+12.35%");
    expect(formatPercent(-8.1)).toBe("-8.10%");
  });

  it("is absent rather than zero when unmeasured", () => {
    expect(formatPercent(null)).toBe(ABSENT);
    expect(formatPercent(0)).toBe("0.00%");
  });
});

describe("toneFor", () => {
  it("stays neutral when the direction is genuinely ambiguous", () => {
    expect(toneFor(500, null)).toBe("neutral");
    expect(toneFor(-500, null)).toBe("neutral");
  });

  it("stays neutral for an unmeasured value", () => {
    expect(toneFor(null)).toBe("neutral");
    expect(toneFor(Number.NaN)).toBe("neutral");
  });

  it("reads a loss as negative where more is better", () => {
    expect(toneFor(-128, true)).toBe("negative");
    expect(toneFor(128, true)).toBe("positive");
  });

  it("inverts when less is better", () => {
    expect(toneFor(-5, false)).toBe("positive");
  });
});

describe("describeFiling", () => {
  it("reads as a filing a human recognises", () => {
    expect(describeFiling(filing())).toBe("Q2 FY2026 10-Q");
  });

  it("degrades to the period when the filing metadata is absent", () => {
    expect(
      describeFiling(filing({ fiscal_period: null, fiscal_year: null, form: null })),
    ).toBe("period ending 2026-06-30");
  });

  it("keeps whatever parts do exist", () => {
    expect(describeFiling(filing({ fiscal_period: null }))).toBe("FY2026 10-Q");
  });
});

describe("sparklinePath", () => {
  it("refuses a flat series rather than emitting NaN coordinates", () => {
    const flat = [
      { date: "2026-01-01", close: 100 },
      { date: "2026-01-02", close: 100 },
      { date: "2026-01-03", close: 100 },
    ];
    expect(sparklinePath(flat, 100, 10)).toBeNull();
  });

  it("refuses a series too short to draw", () => {
    expect(sparklinePath([{ date: "2026-01-01", close: 100 }], 100, 10)).toBeNull();
    expect(sparklinePath([], 100, 10)).toBeNull();
  });

  it("spans the full box, lowest value at the bottom", () => {
    const path = sparklinePath(
      [
        { date: "2026-01-01", close: 100 },
        { date: "2026-01-02", close: 200 },
      ],
      100,
      10,
    );
    expect(path).toBe("M0.00,10.00 L100.00,0.00");
  });

  it("produces only finite coordinates", () => {
    const path = sparklinePath(
      [
        { date: "2026-01-01", close: 1 },
        { date: "2026-01-02", close: 1.0000001 },
        { date: "2026-01-03", close: 0.9999999 },
      ],
      640,
      120,
    );
    expect(path).not.toBeNull();
    expect(path).not.toMatch(/NaN|Infinity/);
  });
});

describe("formatNumber", () => {
  it("is absent rather than zero when unmeasured", () => {
    expect(formatNumber(null)).toBe(ABSENT);
    expect(formatNumber(0)).toBe("0.00");
  });
});
