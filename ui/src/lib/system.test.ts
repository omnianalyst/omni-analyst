import { describe, expect, it } from "vitest";
import {
  engineStatusWord,
  fillOutcomeClass,
  loopAgeLabel,
  loopCadence,
  scheduledLoopTier,
  unhealthyLoops,
  worstScheduledTier,
  type LoopHealthEntry,
  type LoopStatus,
  type SystemHealth,
} from "./system";

const MIN = 60;
const HOUR = 60 * MIN;
const YEAR = 365 * 24 * HOUR;

function loop(
  name: string,
  ageSeconds: number | null,
  neverRun = false,
): LoopStatus {
  return { loop: name, last_activity: null, age_seconds: ageSeconds, never_run: neverRun };
}

function healthEntry(
  entry: Pick<LoopHealthEntry, "loop" | "state"> & Partial<LoopHealthEntry>,
): LoopHealthEntry {
  return {
    last_status: entry.state === "failing" ? "failure" : "success",
    last_success_at: null,
    last_failure_at: null,
    consecutive_failures: 0,
    last_error: null,
    last_result: null,
    expected_interval_seconds: 60,
    ...entry,
  };
}

describe("scheduledLoopTier", () => {
  it("grades a just-ticked loop fresh and steps down as it ages", () => {
    expect(scheduledLoopTier(0, false)).toBe("fresh");
    expect(scheduledLoopTier(2 * MIN - 1, false)).toBe("fresh");
    expect(scheduledLoopTier(2 * MIN, false)).toBe("recent");
    expect(scheduledLoopTier(15 * MIN, false)).toBe("aging");
    expect(scheduledLoopTier(HOUR, false)).toBe("stale");
    expect(scheduledLoopTier(6 * HOUR, false)).toBe("dead");
  });

  it("treats clock skew (negative age) as fresh, not as a broken state", () => {
    expect(scheduledLoopTier(-5, false)).toBe("fresh");
  });

  // The honesty invariant for this rail: a loop that has never produced output
  // is a fresh deployment, not a dead one. If never_run graded as "dead" the
  // rail would scream red on first install.
  it("grades a never-run loop as unknown, never dead", () => {
    expect(scheduledLoopTier(null, true)).toBe("unknown");
    expect(scheduledLoopTier(null, true)).not.toBe("dead");
  });

  it("grades a missing age as unknown", () => {
    expect(scheduledLoopTier(null, false)).toBe("unknown");
    expect(scheduledLoopTier(Number.NaN, false)).toBe("unknown");
  });
});

describe("loopCadence", () => {
  it("classifies the scheduler-driven loops as scheduled", () => {
    expect(loopCadence("prediction")).toBe("scheduled");
    expect(loopCadence("fill")).toBe("scheduled");
  });

  // Findings, demand and claims are written only when there is something to
  // say; their quiet is the conviction gate working, not a stall.
  it("classifies the on-demand loops as on_demand", () => {
    expect(loopCadence("finding")).toBe("on_demand");
    expect(loopCadence("demand")).toBe("on_demand");
    expect(loopCadence("claim_ingest")).toBe("on_demand");
  });

  // Safe default for a loop name the client doesn't know: on_demand, so an
  // unknown loop can never raise a false health alarm.
  it("defaults an unknown loop to on_demand", () => {
    expect(loopCadence("brand_new_loop")).toBe("on_demand");
  });
});

describe("worstScheduledTier", () => {
  it("returns unknown when there are no scheduled loops", () => {
    expect(worstScheduledTier([loop("finding", 10 * YEAR)])).toBe("unknown");
  });

  // The whole point of splitting cadence: a finding loop silent for a year must
  // NOT drag the headline tier to dead, because that silence is legitimate.
  it("ignores on_demand loops even when they are extremely stale", () => {
    const stale = [
      loop("prediction", MIN, false),
      loop("finding", 10 * YEAR, false),
    ];
    expect(worstScheduledTier(stale)).toBe("fresh");
  });

  it("reports the worst tier among scheduled loops", () => {
    expect(
      worstScheduledTier([
        loop("prediction", 5 * MIN, false),
        loop("fill", 7 * HOUR, false),
      ]),
    ).toBe("dead");
  });

  it("reads unknown on a fresh deployment where scheduled loops have never run", () => {
    expect(
      worstScheduledTier([
        loop("prediction", null, true),
        loop("fill", null, true),
      ]),
    ).toBe("unknown");
  });
});

describe("fillOutcomeClass", () => {
  it("maps the fill_outcome enum to display classes", () => {
    expect(fillOutcomeClass("filled")).toBe("good");
    expect(fillOutcomeClass("unfillable")).toBe("blocked");
    expect(fillOutcomeClass("error")).toBe("failed");
  });

  // A future outcome value must render neutrally rather than being silently
  // colored as a success or a failure.
  it("renders an unknown outcome as neutral", () => {
    expect(fillOutcomeClass("brand_new_outcome")).toBe("neutral");
  });
});

describe("loopAgeLabel", () => {
  // never_run must read as a distinct human state, not as "no data" (which
  // would imply a failed timestamp fetch rather than an honest first-run).
  it("labels a never-run loop explicitly, not as missing data", () => {
    expect(loopAgeLabel(loop("prediction", null, true))).toBe("never run");
    expect(loopAgeLabel(loop("prediction", null, true))).not.toBe("no data");
  });

  it("formats a real age via the shared formatter", () => {
    expect(loopAgeLabel(loop("prediction", 3 * HOUR, false))).toBe("3 hours ago");
  });
});

describe("engineStatusWord", () => {
  // A fresh deployment must never read as an outage: standby is distinct from
  // an inactive loop, and an idle loop reads as "inactive" (ask for a glance),
  // never as "down" (asserts broken).
  it("reports standby for an unknown tier, never an outage word", () => {
    expect(engineStatusWord("unknown")).toBe("standby");
    expect(engineStatusWord("unknown")).not.toBe("inactive");
  });

  it("collapses healthy tiers to nominal and graduates the rest", () => {
    expect(engineStatusWord("fresh")).toBe("nominal");
    expect(engineStatusWord("recent")).toBe("nominal");
    expect(engineStatusWord("aging")).toBe("degraded");
    expect(engineStatusWord("stale")).toBe("stalled");
    expect(engineStatusWord("dead")).toBe("inactive");
  });
});

describe("unhealthyLoops", () => {
  function health(loops: SystemHealth["loops"]): SystemHealth {
    return { overall: "ok", loops };
  }

  it("returns nothing when every loop is ok or the view is empty", () => {
    expect(unhealthyLoops(undefined)).toEqual([]);
    expect(unhealthyLoops(health([]))).toEqual([]);
    expect(
      unhealthyLoops(
        health([healthEntry({ loop: "sweep", state: "ok", last_success_at: "t" })]),
      ),
    ).toEqual([]);
  });

  it("flags a failing loop with its error and streak, failing above stale", () => {
    const out = unhealthyLoops(
      health([
        healthEntry({ loop: "sweep", state: "ok", last_success_at: "t" }),
        healthEntry({ loop: "predict", state: "stale", last_success_at: "old" }),
        healthEntry({ loop: "fill", state: "failing", last_success_at: "t", last_failure_at: "t2", consecutive_failures: 3, last_error: "RuntimeError: NoCoverage" }),
      ]),
    );
    // Failing sorts first; the streak count and error text are both carried --
    // a wrong impl that dropped either would fail this assertion.
    expect(out[0]).toEqual({ loop: "fill", flag: "failing", detail: "failing (3x in a row): RuntimeError: NoCoverage" });
    expect(out[1].flag).toBe("stale");
    expect(out).toHaveLength(2);
  });

  it("omits the streak when a loop has failed exactly once", () => {
    const out = unhealthyLoops(
      health([
        healthEntry({ loop: "resolve", state: "failing", last_success_at: null, last_failure_at: "t", consecutive_failures: 1, last_error: "boom" }),
      ]),
    );
    expect(out[0].detail).toBe("failing: boom");
  });
});
