import { describe, expect, it } from "vitest";
import {
  ABSENT,
  DEFAULT_ELIGIBILITY_NOTIONAL,
  RECONCILIATION_STATUSES,
  decimalSign,
  describeCheckedAt,
  describeDivergenceKind,
  describeReconciliation,
  eligibilityPath,
  formatDecimal,
  formatQuantity,
  formatTimestamp,
  methodIsEligibleAnywhere,
  positionSide,
  presentEligibility,
  readsAsHealthy,
  refusalLabel,
  sideLabel,
  sortVenuesBySeverity,
  unresolvedVenues,
  type EligibilityReport,
  type MethodEligibility,
  type VenueReconciliation,
} from "./trading";

function venue(
  name: string,
  status: string,
  checkedAt: string | null = null,
): VenueReconciliation {
  return { venue: name, status, checked_at: checkedAt, discrepancies: [] };
}

function method(over: Partial<MethodEligibility> = {}): MethodEligibility {
  return {
    method: "cross_sectional_momentum",
    entity_kind: "equity",
    status: "uncalibrated",
    total_n: 12,
    resolved_n: 0,
    measured_n: 0,
    live_resolved_n: 0,
    hit_rate: null,
    hit_rate_interval: null,
    walk_forward: null,
    expectancy: {
      gross_bps: null,
      target_bps: null,
      stop_bps: null,
      sample_n: 0,
      refusal: "no calibrated hit rate; expectancy is unknown, not zero",
    },
    realised: {
      n: 0,
      effective_n: 0,
      positive_entities: 0,
      round_trip_cost_bps: "22",
      cost_venue: "cex_taker",
      gross_bps: null,
      net_bps: null,
      assumed_share: null,
      concentration: null,
      refusal: "no resolved directional predictions",
    },
    gates: [
      {
        phase: "paper",
        eligible: false,
        reason: "no_confidence_bucket_has_enough_resolved_predictions",
        detail: "0 resolved",
      },
      {
        phase: "micro",
        eligible: false,
        reason: "too_few_resolved_predictions_for_this_method_and_kind",
        detail: "0 of 30",
      },
      {
        phase: "scale",
        eligible: false,
        reason: "no_walk_forward_validation_has_been_run",
        detail: "no windows",
      },
    ],
    ...over,
  };
}

function report(methods: MethodEligibility[]): EligibilityReport {
  return {
    as_of: "2026-08-08T12:00:00+00:00",
    notional: "1000",
    target_hit_rate: 0.55,
    walk_forward_windows: 4,
    min_per_window: 30,
    venues_are_modelled: true,
    gate_parameters: {
      round_trip_cost_bps: "22",
      cost_venue: "cex_taker",
      min_expectancy_bps: "5",
      min_effective_n: 30,
      max_assumed_share: "0.5",
      max_concentration: "0.5",
    },
    methods,
  };
}

describe("formatDecimal", () => {
  it("renders an absent figure as the absent marker, never as a zero", () => {
    expect(formatDecimal(null)).toBe(ABSENT);
    expect(formatDecimal(undefined)).toBe(ABSENT);
    expect(formatDecimal(null)).not.toBe("0");
    expect(formatDecimal(null)).not.toBe("0.00");
  });

  it("keeps a real zero distinguishable from an absent figure", () => {
    expect(formatDecimal("0")).toBe("0");
    expect(formatDecimal("0")).not.toBe(formatDecimal(null));
  });

  it("passes the server's decimal string through without a float round trip", () => {
    // Number("10250.00").toString() is "10250", and 0.1 + 0.2 arithmetic on a
    // NAV is the defect the string convention exists to prevent.
    expect(formatDecimal("10250.00")).toBe("10250.00");
    expect(formatDecimal("0.000000000000000001")).toBe("0.000000000000000001");
    expect(formatDecimal("-2.50")).toBe("-2.50");
  });
});

describe("decimalSign", () => {
  it("reads a negative quantity as negative", () => {
    expect(decimalSign("-2")).toBe(-1);
    expect(decimalSign("-0.0001")).toBe(-1);
    expect(decimalSign(" -12.5 ")).toBe(-1);
  });

  it("reads a positive quantity as positive", () => {
    expect(decimalSign("2")).toBe(1);
    expect(decimalSign("+2")).toBe(1);
    expect(decimalSign("0.5")).toBe(1);
  });

  it("reads every spelling of zero as zero", () => {
    expect(decimalSign("0")).toBe(0);
    expect(decimalSign("0.00")).toBe(0);
    expect(decimalSign("-0.0")).toBe(0);
  });

  it("refuses a string that is not a decimal rather than guessing a sign", () => {
    expect(decimalSign("NaN")).toBeNull();
    expect(decimalSign("Infinity")).toBeNull();
    expect(decimalSign("")).toBeNull();
    expect(decimalSign("two")).toBeNull();
    expect(decimalSign(null)).toBeNull();
  });
});

describe("formatQuantity", () => {
  it("renders a short with its sign intact", () => {
    expect(formatQuantity("-2")).toBe("-2");
    expect(formatQuantity("-2")).not.toBe("2");
    expect(formatQuantity("-0.5")).toBe("-0.5");
  });

  it("renders a long unchanged, so long and short never print alike", () => {
    expect(formatQuantity("2")).toBe("2");
    expect(formatQuantity("2")).not.toBe(formatQuantity("-2"));
  });

  it("renders an absent quantity as absent, not as zero", () => {
    expect(formatQuantity(null)).toBe(ABSENT);
    expect(formatQuantity(null)).not.toBe("0");
  });
});

describe("positionSide", () => {
  it("calls a negative quantity a short", () => {
    expect(positionSide({ quantity: "-2", is_short: true })).toBe("short");
    expect(positionSide({ quantity: "-2" })).toBe("short");
  });

  it("calls a positive quantity a long", () => {
    expect(positionSide({ quantity: "2", is_short: false })).toBe("long");
    expect(positionSide({ quantity: "2" })).toBe("long");
  });

  it("never gives a short and a long of the same size the same side", () => {
    expect(positionSide({ quantity: "-2", is_short: true })).not.toBe(
      positionSide({ quantity: "2", is_short: false }),
    );
  });

  it("calls a zero quantity flat, not long", () => {
    expect(positionSide({ quantity: "0", is_short: false })).toBe("flat");
  });

  it("reports a disagreement between the sign and is_short instead of picking one", () => {
    expect(positionSide({ quantity: "-2", is_short: false })).toBe("contradictory");
    expect(positionSide({ quantity: "2", is_short: true })).toBe("contradictory");
  });

  it("says unknown for a quantity it could not read, rather than long", () => {
    expect(positionSide({ quantity: null })).toBe("unknown");
    expect(positionSide({ quantity: "n/a" })).toBe("unknown");
  });

  it("labels every side distinctly", () => {
    const labels = (["long", "short", "flat", "unknown", "contradictory"] as const).map(
      sideLabel,
    );
    expect(new Set(labels).size).toBe(labels.length);
    expect(sideLabel("short")).not.toBe(sideLabel("long"));
  });
});

describe("describeReconciliation", () => {
  it("covers exactly the four statuses the contract allows", () => {
    expect(RECONCILIATION_STATUSES).toEqual([
      "reconciled",
      "diverged",
      "never_run",
      "stale",
    ]);
  });

  it("treats only a reconciled venue as healthy", () => {
    for (const status of RECONCILIATION_STATUSES) {
      expect(readsAsHealthy(status)).toBe(status === "reconciled");
    }
  });

  it("does not read a venue that was never checked as one that passed", () => {
    const never = describeReconciliation("never_run");
    const clear = describeReconciliation("reconciled");
    expect(never.tone).toBe("unresolved");
    expect(never.tone).not.toBe(clear.tone);
    expect(never.label).not.toBe(clear.label);
    expect(never.explanation).not.toBe(clear.explanation);
    expect(readsAsHealthy("never_run")).toBe(false);
  });

  it("does not read a stale check as a current one", () => {
    const stale = describeReconciliation("stale");
    expect(stale.tone).toBe("unresolved");
    expect(stale.tone).not.toBe(describeReconciliation("reconciled").tone);
    expect(readsAsHealthy("stale")).toBe(false);
  });

  it("gives a diverged venue its own tone, separate from the unresolved ones", () => {
    expect(describeReconciliation("diverged").tone).toBe("diverged");
    expect(describeReconciliation("diverged").tone).not.toBe(
      describeReconciliation("never_run").tone,
    );
  });

  it("does not read an unrecognised status as healthy", () => {
    expect(readsAsHealthy("checked_probably")).toBe(false);
    expect(describeReconciliation("checked_probably").tone).toBe("unknown");
  });

  it("gives every status its own label", () => {
    const labels = RECONCILIATION_STATUSES.map((s) => describeReconciliation(s).label);
    expect(new Set(labels).size).toBe(RECONCILIATION_STATUSES.length);
  });
});

describe("sortVenuesBySeverity", () => {
  it("puts unresolved and diverged venues above reconciled ones", () => {
    const sorted = sortVenuesBySeverity([
      venue("binance", "reconciled", "2026-08-08T11:59:00+00:00"),
      venue("kraken", "never_run"),
      venue("okx", "diverged", "2026-08-08T11:40:00+00:00"),
    ]);
    expect(sorted.map((v) => v.venue)).toEqual(["okx", "kraken", "binance"]);
  });

  it("does not reorder the caller's array", () => {
    const input = [venue("a", "reconciled"), venue("b", "never_run")];
    sortVenuesBySeverity(input);
    expect(input.map((v) => v.venue)).toEqual(["a", "b"]);
  });
});

describe("unresolvedVenues", () => {
  it("counts never_run and stale as unresolved alongside diverged", () => {
    const venues = [
      venue("binance", "reconciled", "2026-08-08T11:59:00+00:00"),
      venue("kraken", "never_run"),
      venue("okx", "diverged", "2026-08-08T11:40:00+00:00"),
      venue("bybit", "stale", "2026-08-01T00:00:00+00:00"),
    ];
    expect(unresolvedVenues(venues).map((v) => v.venue)).toEqual([
      "kraken",
      "okx",
      "bybit",
    ]);
  });

  it("is empty only when every venue actually reconciled", () => {
    expect(unresolvedVenues([venue("binance", "reconciled")])).toHaveLength(0);
  });
});

describe("describeDivergenceKind", () => {
  it("names every divergence kind the contract lists", () => {
    const kinds = [
      "position_quantity",
      "position_missing_locally",
      "position_missing_at_venue",
      "cash_balance",
      "cash_locked",
      "unknown_symbol",
      "venue_unavailable",
    ];
    const described = kinds.map(describeDivergenceKind);
    for (const [i, kind] of kinds.entries()) {
      expect(described[i]).not.toBe(kind);
    }
    expect(new Set(described).size).toBe(kinds.length);
  });

  it("passes an unrecognised kind through rather than inventing a description", () => {
    expect(describeDivergenceKind("some_future_kind")).toBe("some_future_kind");
  });

  it("does not describe a position missing at the venue as one missing locally", () => {
    expect(describeDivergenceKind("position_missing_at_venue")).not.toBe(
      describeDivergenceKind("position_missing_locally"),
    );
  });
});

describe("describeCheckedAt", () => {
  it("says never checked when no check has run", () => {
    expect(describeCheckedAt(null)).toBe("never checked");
  });

  it("renders the time of a check that did run", () => {
    expect(describeCheckedAt("2026-08-08T11:59:00+00:00")).toBe(
      "checked 2026-08-08 11:59:00",
    );
  });
});

describe("formatTimestamp", () => {
  it("renders an absent timestamp as absent", () => {
    expect(formatTimestamp(null)).toBe(ABSENT);
    expect(formatTimestamp("")).toBe(ABSENT);
  });

  it("renders a present timestamp readably", () => {
    expect(formatTimestamp("2026-08-08T12:00:00+00:00")).toBe("2026-08-08 12:00:00");
  });
});

describe("refusalLabel", () => {
  it("gives every refusal the policy tier can emit a stated reason", () => {
    const reasons = [
      "no_confidence_bucket_has_enough_resolved_predictions",
      "calibrated_hit_rate_is_below_the_target",
      "too_few_resolved_predictions_for_this_method_and_kind",
      "too_much_of_the_realised_pnl_was_assumed_rather_than_measured",
      "the_realised_edge_is_carried_by_a_single_entity",
      "net_expectancy_per_trade_is_below_the_required_minimum",
      "no_walk_forward_validation_has_been_run",
      "walk_forward_did_not_hold_out_of_sample",
      "too_few_live_resolved_predictions_backfill_does_not_count",
      "the_current_trading_phase_forbids_holding_capital",
    ];
    const labels = reasons.map(refusalLabel);
    for (const [i, reason] of reasons.entries()) {
      expect(labels[i]).not.toBe(reason);
      expect(labels[i].length).toBeGreaterThan(0);
    }
    expect(new Set(labels).size).toBe(reasons.length);
  });

  it("shows an unrecognised reason code verbatim rather than a generic refusal", () => {
    expect(refusalLabel("some_future_reason")).toBe("some_future_reason");
  });

  it("does not invent a reason when the server stated none", () => {
    expect(refusalLabel(null)).toBe("Refused without a stated reason.");
    expect(refusalLabel(null)).not.toBe(
      refusalLabel("no_walk_forward_validation_has_been_run"),
    );
  });
});

describe("methodIsEligibleAnywhere", () => {
  it("is false when every phase refused", () => {
    expect(methodIsEligibleAnywhere(method())).toBe(false);
  });

  it("is true when any single phase permitted", () => {
    const m = method({
      gates: [
        { phase: "paper", eligible: true, reason: null, detail: "passed" },
        {
          phase: "micro",
          eligible: false,
          reason: "too_few_resolved_predictions_for_this_method_and_kind",
          detail: "12 of 30",
        },
      ],
    });
    expect(methodIsEligibleAnywhere(m)).toBe(true);
  });
});

describe("presentEligibility", () => {
  it("presents an empty method list as the gate refusing, not as an error", () => {
    const v = presentEligibility(report([]));
    expect(v.kind).toBe("refused");
    expect(v.kind).not.toBe("unreadable");
    expect(v.explanation.length).toBeGreaterThan(0);
  });

  it("presents methods that were all refused as the gate working", () => {
    const v = presentEligibility(report([method()]));
    expect(v.kind).toBe("refused");
    expect(v.methods).toHaveLength(1);
  });

  it("presents a method eligible in one phase as permitted", () => {
    const m = method({
      gates: [
        { phase: "paper", eligible: true, reason: null, detail: "passed" },
        {
          phase: "micro",
          eligible: false,
          reason: "net_expectancy_per_trade_is_below_the_required_minimum",
          detail: "3 bps of 5",
        },
      ],
    });
    expect(presentEligibility(report([m])).kind).toBe("permitted");
  });

  it("distinguishes a refusal from a payload it could not read", () => {
    const malformed = { ...report([]), methods: undefined } as unknown as EligibilityReport;
    expect(presentEligibility(malformed).kind).toBe("unreadable");
    expect(presentEligibility(malformed).methods).toHaveLength(0);
    expect(presentEligibility(report([])).kind).not.toBe(
      presentEligibility(malformed).kind,
    );
  });

  it("gives a refusal and a permission different headlines", () => {
    const permitted = presentEligibility(
      report([
        method({
          gates: [{ phase: "paper", eligible: true, reason: null, detail: "passed" }],
        }),
      ]),
    );
    expect(presentEligibility(report([])).headline).not.toBe(permitted.headline);
  });
});

describe("eligibilityPath", () => {
  it("carries the notional the cost model requires", () => {
    expect(eligibilityPath("1000")).toBe("/trading/eligibility?notional=1000");
    expect(eligibilityPath(DEFAULT_ELIGIBILITY_NOTIONAL)).toContain("notional=");
  });

  it("encodes a notional that would otherwise break the query string", () => {
    expect(eligibilityPath("1 000&x=1")).toBe(
      "/trading/eligibility?notional=1%20000%26x%3D1",
    );
  });
});
