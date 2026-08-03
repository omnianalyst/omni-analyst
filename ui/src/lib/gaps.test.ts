import { describe, expect, it } from "vitest";
import { contradictionTypes } from "./gaps";
import type { Gap } from "./api";

function gap(claim_type: string, gap_class: string): Gap {
  return {
    id: "x",
    claim_type,
    key: null,
    gap_class,
    audience_user_id: null,
    score: 1,
    attempts: 0,
    detail: null,
    detected_at: null,
  };
}

describe("contradictionTypes", () => {
  it("collects only claim types that have a contradictory gap", () => {
    const types = contradictionTypes([
      gap("lei_signal", "contradictory"),
      gap("lei_signal", "stale"),
      gap("fundamental_metric", "missing"),
      gap("price_snapshot", "contradictory"),
    ]);
    expect([...types].sort()).toEqual(["lei_signal", "price_snapshot"]);
  });

  it("returns an empty set when no gaps contradict", () => {
    expect(contradictionTypes([gap("a", "missing"), gap("b", "stale")]).size).toBe(0);
  });

  it("returns an empty set for no gaps at all", () => {
    expect(contradictionTypes([]).size).toBe(0);
  });
});
