import { describe, expect, it } from "vitest";
import { asymmetry } from "./explain";

describe("asymmetry", () => {
  it("measures the film's trade: risking 10 cents to make 8 bucks", () => {
    // entry 100, wrong-side at 99, target at 180 -> 1% risk, 80% payoff, 80:1
    const a = asymmetry("up", 100, 180, 99);

    expect(a).not.toBeNull();
    expect(a!.riskPct).toBeCloseTo(1, 6);
    expect(a!.payoffPct).toBeCloseTo(80, 6);
    expect(a!.ratio).toBeCloseTo(80, 6);
  });

  it("reads a down call against its own barriers", () => {
    // down from 100: invalidation above at 105, target below at 80
    const a = asymmetry("down", 100, 105, 80);

    expect(a).not.toBeNull();
    expect(a!.riskPct).toBeCloseTo(5, 6);
    expect(a!.payoffPct).toBeCloseTo(20, 6);
    expect(a!.ratio).toBeCloseTo(4, 6);
  });

  it("refuses degenerate geometry rather than inventing a ratio", () => {
    // invalidation on the wrong side of entry (down call, lower above entry)
    expect(asymmetry("down", 100, 105, 110)).toBeNull();
    // zero risk: invalidation at entry -- division by zero dressed as a trade
    expect(asymmetry("up", 100, 130, 100)).toBeNull();
    // missing barriers
    expect(asymmetry("up", 100, null, 90)).toBeNull();
    expect(asymmetry(null, 100, 130, 90)).toBeNull();
  });

  it("refuses a nonpositive entry price", () => {
    expect(asymmetry("up", 0, 130, 90)).toBeNull();
    expect(asymmetry("up", -5, 130, 90)).toBeNull();
  });
});
