import { authedGetJson } from "./auth";

// What is printed where the API returned null. Absent is not zero: a zero is a
// measurement, and printing one for an unmeasured field asserts a check nobody
// ran. Every formatter here funnels null to this rather than to a number.
export const ABSENT = "\u2014";

export type MarketType = "spot" | "perpetual";

export interface Position {
  venue: string;
  symbol: string;
  market_type: MarketType;
  quantity: string;
  average_entry: string | null;
  notional: string | null;
  is_short: boolean | null;
  as_of: string | null;
}

export interface CashPosition {
  venue: string;
  asset: string;
  free: string | null;
  locked: string | null;
  as_of: string | null;
}

export interface Portfolio {
  portfolio_id: string;
  as_of: string | null;
  nav: string | null;
  cash: string | null;
  gross_exposure: string | null;
  net_exposure: string | null;
  positions: Position[];
  cash_positions: CashPosition[];
}

export type ReconciliationStatus =
  | "reconciled"
  | "diverged"
  | "never_run"
  | "stale";

export const RECONCILIATION_STATUSES: ReconciliationStatus[] = [
  "reconciled",
  "diverged",
  "never_run",
  "stale",
];

export interface Divergence {
  kind: string;
  venue: string;
  symbol: string | null;
  local: string | null;
  remote: string | null;
  detail: string | null;
}

export interface VenueReconciliation {
  venue: string;
  status: string;
  checked_at: string | null;
  discrepancies: Divergence[];
}

export interface ReconciliationReport {
  as_of: string | null;
  venues: VenueReconciliation[];
}

export interface GateVerdict {
  phase: string;
  eligible: boolean;
  reason: string | null;
  detail: string;
}

export interface WalkForward {
  windows: number;
  qualifying_windows: number;
  min_per_window: number;
  pooled_n: number;
  pooled_hits: number;
  total_test_n: number;
  pooled_hit_rate: number | null;
  interval: [number, number] | null;
  live_pooled_n: number;
  backfilled_pooled_n: number;
  positive: boolean;
}

export interface Expectancy {
  gross_bps: string | null;
  target_bps: string | null;
  stop_bps: string | null;
  sample_n: number;
  refusal: string | null;
}

export interface Realised {
  n: number;
  effective_n: number;
  positive_entities: number;
  round_trip_cost_bps: string | null;
  cost_venue: string;
  gross_bps: string | null;
  net_bps: string | null;
  assumed_share: string | null;
  concentration: string | null;
  refusal: string | null;
}

export interface MethodEligibility {
  method: string;
  entity_kind: string;
  status: string;
  total_n: number;
  resolved_n: number;
  measured_n: number;
  live_resolved_n: number;
  hit_rate: number | null;
  hit_rate_interval: [number, number] | null;
  walk_forward: WalkForward | null;
  expectancy: Expectancy;
  realised: Realised;
  gates: GateVerdict[];
}

export interface GateParameters {
  round_trip_cost_bps: string;
  cost_venue: string;
  min_expectancy_bps: string;
  min_effective_n: number;
  max_assumed_share: string;
  max_concentration: string;
}

export interface EligibilityReport {
  as_of: string;
  notional: string;
  target_hit_rate: number;
  walk_forward_windows: number;
  min_per_window: number;
  venues_are_modelled: boolean;
  gate_parameters: GateParameters;
  methods: MethodEligibility[];
}

// The cost model prices a round trip in basis points, and gas is a fixed amount
// per transaction, so the endpoint refuses to report anything until a trade size
// is named -- `notional` is a required query parameter and a missing one is a
// 400, not a default. This is the size the panel asks about; the response echoes
// the one the server priced against and that echo is what is displayed, so the
// figure on screen is never this constant standing in for the server's.
export const DEFAULT_ELIGIBILITY_NOTIONAL = "1000";

export function eligibilityPath(notional: string): string {
  return `/trading/eligibility?notional=${encodeURIComponent(notional)}`;
}

export const getPortfolio = (): Promise<Portfolio> =>
  authedGetJson<Portfolio>("/trading/portfolio");

export const getReconciliation = (): Promise<ReconciliationReport> =>
  authedGetJson<ReconciliationReport>("/trading/reconciliation");

export const getEligibility = (
  notional: string = DEFAULT_ELIGIBILITY_NOTIONAL,
): Promise<EligibilityReport> =>
  authedGetJson<EligibilityReport>(eligibilityPath(notional));

// Money arrives as the string `str(Decimal)` produced, and it leaves as that
// same string. Parsing it into a JS number to format it would round-trip a
// decimal through binary64 -- the exact defect the string convention exists to
// prevent -- so nothing here touches Number().
export function formatDecimal(raw: string | null | undefined): string {
  if (raw === null || raw === undefined) return ABSENT;
  const s = raw.trim();
  return s === "" ? ABSENT : s;
}

const DECIMAL_RE = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE][+-]?\d+)?$/;

// The sign, read off the digits rather than off a float. Returns null when the
// string is not a decimal at all, because "unrecognised" and "zero" are
// different answers and a caller that conflates them renders a long position
// for a payload it could not parse.
export function decimalSign(raw: string | null | undefined): -1 | 0 | 1 | null {
  if (raw === null || raw === undefined) return null;
  const s = raw.trim();
  const m = DECIMAL_RE.exec(s);
  if (m === null) return null;
  const digits = `${m[2] ?? ""}${m[3] ?? ""}`;
  if (digits === "") return null;
  if (!/[1-9]/.test(digits)) return 0;
  return m[1] === "-" ? -1 : 1;
}

// Verbatim, sign included. A short rendered without its minus is a long, and
// the operator reading it has the direction of their risk exactly backwards.
// It has its own name rather than sharing the money formatter's so the quantity
// column has one seam, tested, that a later tidy-up of the number columns
// cannot strip the sign through.
export function formatQuantity(raw: string | null | undefined): string {
  return formatDecimal(raw);
}

export type PositionSide =
  | "long"
  | "short"
  | "flat"
  | "unknown"
  | "contradictory";

// The API states the side twice -- once in the sign of `quantity`, once in
// `is_short`. They agree or the row is wrong, and picking a winner silently is
// how a book comes to display the opposite of what it holds, so a disagreement
// is reported as one.
export function positionSide(p: {
  quantity: string | null;
  is_short?: boolean | null;
}): PositionSide {
  const sign = decimalSign(p.quantity);
  if (sign === null) return "unknown";
  const flagged = p.is_short ?? null;
  if (sign === 0) return flagged === true ? "contradictory" : "flat";
  const fromSign: PositionSide = sign < 0 ? "short" : "long";
  if (flagged === null) return fromSign;
  const fromFlag: PositionSide = flagged ? "short" : "long";
  return fromFlag === fromSign ? fromSign : "contradictory";
}

export function sideLabel(side: PositionSide): string {
  switch (side) {
    case "long":
      return "Long";
    case "short":
      return "Short";
    case "flat":
      return "Flat";
    case "contradictory":
      return "Side disputed";
    case "unknown":
      return "Side unknown";
  }
}

export type StatusTone = "clear" | "diverged" | "unresolved" | "unknown";

export interface StatusPresentation {
  status: string;
  tone: StatusTone;
  label: string;
  explanation: string;
}

// `never_run` and `stale` are their own tone on purpose. A venue nobody checked
// and a venue whose check is too old to stand are both unresolved, and folding
// either into `reconciled` reports a pass that never happened.
export function describeReconciliation(status: string): StatusPresentation {
  switch (status) {
    case "reconciled":
      return {
        status,
        tone: "clear",
        label: "Reconciled",
        explanation: "The local book matched the venue at the last check.",
      };
    case "diverged":
      return {
        status,
        tone: "diverged",
        label: "Diverged",
        explanation:
          "The venue and the local book disagree. Every difference found is listed below.",
      };
    case "never_run":
      return {
        status,
        tone: "unresolved",
        label: "Never checked",
        explanation:
          "No reconciliation has run against this venue. Nothing here has been verified, which is not the same as verified correct.",
      };
    case "stale":
      return {
        status,
        tone: "unresolved",
        label: "Stale",
        explanation:
          "The last check is too old to describe the book now. What it found then says nothing about what is there today.",
      };
    default:
      return {
        status,
        tone: "unknown",
        label: status,
        explanation:
          "The server reported a status this build does not recognise. It is not being read as a pass.",
      };
  }
}

export function readsAsHealthy(status: string): boolean {
  return describeReconciliation(status).tone === "clear";
}

const TONE_SEVERITY: Record<StatusTone, number> = {
  unknown: 0,
  diverged: 1,
  unresolved: 2,
  clear: 3,
};

export function sortVenuesBySeverity(
  venues: VenueReconciliation[],
): VenueReconciliation[] {
  return [...venues].sort(
    (a, b) =>
      TONE_SEVERITY[describeReconciliation(a.status).tone] -
      TONE_SEVERITY[describeReconciliation(b.status).tone],
  );
}

export function unresolvedVenues(
  venues: VenueReconciliation[],
): VenueReconciliation[] {
  return venues.filter((v) => !readsAsHealthy(v.status));
}

const DIVERGENCE_LABELS: Record<string, string> = {
  position_quantity: "Position quantity differs from the venue",
  position_missing_locally: "The venue holds a position the local book does not",
  position_missing_at_venue: "The local book holds a position the venue does not",
  cash_balance: "Cash balance differs from the venue",
  cash_locked: "Locked cash differs from the venue",
  unknown_symbol: "The venue reported a symbol this system does not know",
  venue_unavailable: "The venue could not be reached, so nothing was compared",
};

export function describeDivergenceKind(kind: string): string {
  return DIVERGENCE_LABELS[kind] ?? kind;
}

export function describeCheckedAt(checkedAt: string | null): string {
  if (checkedAt === null) return "never checked";
  return `checked ${checkedAt.slice(0, 19).replace("T", " ")}`;
}

export function formatTimestamp(raw: string | null | undefined): string {
  if (raw === null || raw === undefined || raw === "") return ABSENT;
  return raw.slice(0, 19).replace("T", " ");
}

const REFUSAL_LABELS: Record<string, string> = {
  no_confidence_bucket_has_enough_resolved_predictions:
    "No confidence bucket holds enough resolved predictions to calibrate a hit rate.",
  calibrated_hit_rate_is_below_the_target:
    "The calibrated hit rate is below the target it must clear.",
  too_few_resolved_predictions_for_this_method_and_kind:
    "Too few resolved predictions for this method and entity kind.",
  too_much_of_the_realised_pnl_was_assumed_rather_than_measured:
    "Too much of the realised P&L was assumed rather than measured.",
  the_realised_edge_is_carried_by_a_single_entity:
    "The realised edge is carried by a single entity.",
  net_expectancy_per_trade_is_below_the_required_minimum:
    "Net expectancy per trade, after costs, is below the required minimum.",
  no_walk_forward_validation_has_been_run:
    "No walk-forward validation has been run.",
  walk_forward_did_not_hold_out_of_sample:
    "The walk-forward did not hold out of sample.",
  too_few_live_resolved_predictions_backfill_does_not_count:
    "Too few live resolved predictions; backfilled ones do not count.",
  the_current_trading_phase_forbids_holding_capital:
    "The current trading phase forbids holding capital.",
};

// An unrecognised reason is passed through rather than replaced with a generic
// sentence. The reason code is the evidence; a friendly stand-in for one this
// build has not seen would hide a refusal the server thought worth naming.
export function refusalLabel(reason: string | null | undefined): string {
  if (reason === null || reason === undefined || reason.trim() === "") {
    return "Refused without a stated reason.";
  }
  return REFUSAL_LABELS[reason] ?? reason;
}

export function methodIsEligibleAnywhere(m: MethodEligibility): boolean {
  return Array.isArray(m.gates) && m.gates.some((g) => g.eligible);
}

export type EligibilityVerdict =
  | { kind: "refused"; headline: string; explanation: string; methods: MethodEligibility[] }
  | { kind: "permitted"; headline: string; explanation: string; methods: MethodEligibility[] }
  | { kind: "unreadable"; headline: string; explanation: string; methods: MethodEligibility[] };

// Nothing being eligible is the answer, not the absence of one. An empty method
// list means no prediction has resolved for this audience yet, which is a state
// the gate is supposed to produce -- rendering it as an error or as an empty
// screen would report the one working part of the system as broken.
// `unreadable` is reserved for a payload that is not the contract's shape at
// all, which is a different fact and the only one worth an error.
export function presentEligibility(report: EligibilityReport): EligibilityVerdict {
  const methods = report?.methods;
  if (!Array.isArray(methods)) {
    return {
      kind: "unreadable",
      headline: "The eligibility report could not be read",
      explanation:
        "The response carried no list of methods. Nothing is being shown rather than a verdict guessed from a payload this build does not understand.",
      methods: [],
    };
  }
  if (methods.length === 0) {
    return {
      kind: "refused",
      headline: "No method holds capital",
      explanation:
        "No prediction has resolved for this audience, so there is no record to calibrate against and no method can be permitted. This is the gate working: capital moves on evidence, and none has been produced yet.",
      methods,
    };
  }
  if (methods.some(methodIsEligibleAnywhere)) {
    return {
      kind: "permitted",
      headline: "A method has earned capital",
      explanation:
        "At least one method passed the gate in at least one phase. Each row below states every phase verdict and the record it was taken on.",
      methods,
    };
  }
  return {
    kind: "refused",
    headline: "No method holds capital",
    explanation:
      "Every method below was measured against the gate and refused in every phase. The reason for each refusal is stated on its row. This is the gate working, not a fault.",
    methods,
  };
}
