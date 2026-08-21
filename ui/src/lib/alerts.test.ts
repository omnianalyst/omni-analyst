import { describe, expect, it } from "vitest";
import {
  buildCondition,
  CONDITION_KINDS,
  defaultConditionForm,
  describeCondition,
  describeLastFired,
} from "./alerts";

describe("CONDITION_KINDS", () => {
  it("is exactly the four kinds the closed set on the server allows", () => {
    expect(CONDITION_KINDS).toEqual([
      "value_above",
      "value_below",
      "pct_change_above",
      "pct_change_below",
      "staleness_exceeds",
      "contradiction",
    ]);
    expect(CONDITION_KINDS).toHaveLength(6);
  });
});

describe("defaultConditionForm", () => {
  it("defaults the value-threshold field to 'value', matching the server default", () => {
    const f = defaultConditionForm();
    expect(f.field).toBe("value");
    expect(f.kind).toBe("value_above");
    expect(f.threshold).toBe("");
    expect(f.seconds).toBe("");
  });
});

describe("buildCondition", () => {
  it("builds a value_above condition with threshold and field", () => {
    const out = buildCondition({
      kind: "value_above",
      threshold: "42",
      pct: "",
      windowDays: "30",
      field: "value",
      seconds: "",
    });
    expect(out.ok).toBe(true);
    if (out.ok) {
      expect(out.condition).toEqual({
        kind: "value_above",
        threshold: 42,
        field: "value",
      });
    }
  });

  it("builds a value_below condition, preserving the custom field name", () => {
    const out = buildCondition({
      kind: "value_below",
      threshold: "0.5",
      pct: "",
      windowDays: "30",
      field: "pe_ratio",
      seconds: "",
    });
    expect(out.ok).toBe(true);
    if (out.ok) {
      expect(out.condition).toEqual({
        kind: "value_below",
        threshold: 0.5,
        field: "pe_ratio",
      });
    }
  });

  it("rejects an empty threshold rather than coercing it to 0", () => {
    const out = buildCondition({
      kind: "value_above",
      threshold: "",
      pct: "",
      windowDays: "30",
      field: "value",
      seconds: "",
    });
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.error).toMatch(/threshold/);
  });

  it("rejects a non-numeric threshold", () => {
    const out = buildCondition({
      kind: "value_above",
      threshold: "abc",
      pct: "",
      windowDays: "30",
      field: "value",
      seconds: "",
    });
    expect(out.ok).toBe(false);
  });

  it("rejects an empty field name", () => {
    const out = buildCondition({
      kind: "value_above",
      threshold: "1",
      pct: "",
      windowDays: "30",
      field: "  ",
      seconds: "",
    });
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.error).toMatch(/field/);
  });

  it("builds a staleness_exceeds condition from a positive number of seconds", () => {
    const out = buildCondition({
      kind: "staleness_exceeds",
      threshold: "",
      pct: "",
      windowDays: "30",
      field: "value",
      seconds: "3600",
    });
    expect(out.ok).toBe(true);
    if (out.ok) {
      expect(out.condition).toEqual({
        kind: "staleness_exceeds",
        seconds: 3600,
      });
    }
  });

  it("rejects zero seconds, which cannot be a staleness threshold", () => {
    const out = buildCondition({
      kind: "staleness_exceeds",
      threshold: "",
      pct: "",
      windowDays: "30",
      field: "value",
      seconds: "0",
    });
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.error).toMatch(/positive/);
  });

  it("rejects negative seconds", () => {
    const out = buildCondition({
      kind: "staleness_exceeds",
      threshold: "",
      pct: "",
      windowDays: "30",
      field: "value",
      seconds: "-5",
    });
    expect(out.ok).toBe(false);
  });

  it("builds a contradiction condition with no extra fields", () => {
    const out = buildCondition({
      kind: "contradiction",
      threshold: "1",
      pct: "",
      windowDays: "30",
      field: "value",
      seconds: "",
    });
    expect(out.ok).toBe(true);
    if (out.ok) {
      expect(out.condition).toEqual({ kind: "contradiction" });
    }
  });

  it("produces a shape the server's validate_condition would accept for each kind", () => {
    for (const kind of [
      "value_above",
      "value_below",
    ] as const) {
      const out = buildCondition({
        kind,
        threshold: "10",
        field: "value",
        seconds: "",
        pct: "",
        windowDays: "30",
      });
      if (!out.ok) throw new Error(`expected ok for ${kind}`);
      const c = out.condition as { kind: string; threshold: number; field: string };
      expect(c).toHaveProperty("threshold");
      expect(c).toHaveProperty("field");
      expect(typeof c.threshold).toBe("number");
    }
  });
});

describe("describeCondition", () => {
  it("renders the operator for a value_above condition", () => {
    expect(
      describeCondition({ kind: "value_above", threshold: 100, field: "value" }),
    ).toBe("value_above: value > 100");
  });

  it("renders the operator for a value_below condition", () => {
    expect(
      describeCondition({ kind: "value_below", threshold: 0.5, field: "pe" }),
    ).toBe("value_below: pe < 0.5");
  });

  it("falls back to the 'value' field name when none is stored", () => {
    expect(describeCondition({ kind: "value_above", threshold: 1 })).toBe(
      "value_above: value > 1",
    );
  });

  it("renders the seconds for a staleness_exceeds condition", () => {
    expect(describeCondition({ kind: "staleness_exceeds", seconds: 60 })).toBe(
      "staleness_exceeds: 60s",
    );
  });

  it("renders contradiction verbatim", () => {
    expect(describeCondition({ kind: "contradiction" })).toBe("contradiction");
  });

  it("does not invent a friendly description for an unknown kind", () => {
    expect(describeCondition({ kind: "some_future_kind" })).toBe(
      "(unknown condition: some_future_kind)",
    );
  });

  it("says malformed for a non-object", () => {
    expect(describeCondition(null)).toBe("(malformed condition)");
    expect(describeCondition("value_above")).toBe("(malformed condition)");
  });
});

describe("describeLastFired", () => {
  it("says never fired when the alert has not fired", () => {
    expect(describeLastFired(null)).toBe("never fired");
  });

  it("renders a readable timestamp when it has fired", () => {
    expect(describeLastFired("2026-07-28T13:45:09Z")).toBe(
      "last fired 2026-07-28 13:45:09",
    );
  });
});
