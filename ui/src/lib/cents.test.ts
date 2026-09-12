import { describe, expect, it } from "vitest";
import { parseCents } from "./cents";

describe("parseCents", () => {
  it("rejects sub-cent precision rather than rounding it away (U20)", () => {
    expect(() => parseCents("1.005")).toThrow();
    expect(() => parseCents("-1.005")).toThrow();
    expect(() => parseCents("0.1.2")).toThrow();
    expect(() => parseCents("abc")).toThrow();
    expect(() => parseCents("")).toThrow();
  });

  it("parses exact cents with either sign", () => {
    expect(parseCents("1.01")).toBe(101);
    expect(parseCents("-1.01")).toBe(-101);
    expect(parseCents("0.1")).toBe(10);
    expect(parseCents("42")).toBe(4200);
    expect(parseCents("+3.5")).toBe(350);
    expect(parseCents(" 2.75 ")).toBe(275);
  });

  it("refuses values beyond the exact integer range", () => {
    expect(() => parseCents("99999999999999999999")).toThrow(/exact supported range/);
  });
});
