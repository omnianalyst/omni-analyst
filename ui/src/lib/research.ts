import { authedGetJson } from "./auth";

// The em-dash printed where the API returned null. Absent is not zero, and a
// zero t-statistic is a measurement — printing one for an unmeasured test would
// assert a check nobody ran.
export const ABSENT = "—";

export interface HypothesisTest {
  name: string;
  source: string;
  cells: number;
  verdict: string;
  recorded_at: string;
  detail: { bar?: number; best_recent_third_t?: number };
  mirrored_at: string;
}

export interface ResearchSummary {
  tests: number;
  cells: number;
  passed: number;
  failed: number;
  bar: number;
  fdr_bar: number;
  best_t: number | null;
  sources: string[];
  last_recorded_at: string | null;
  last_mirrored_at: string | null;
}

export interface ResearchRecord {
  summary: ResearchSummary;
  tests: HypothesisTest[];
}

export const getResearchRecord = (): Promise<ResearchRecord> =>
  authedGetJson<ResearchRecord>("/research/hypotheses");

export type EdgeMonitorState = "insufficient" | "holding" | "unconfirmed" | "decayed";

export interface EdgeBookState {
  book: string;
  promoted: boolean;
  state: EdgeMonitorState;
  scored_sessions: number;
  recent_sessions: number;
  mean_session_excess: number | null;
  decay_p: number | null;
  window_start: string | null;
  window_end: string | null;
  reason: string | null;
  as_of: string;
  evaluated_at: string | null;
}

export interface EdgeMonitor {
  books: EdgeBookState[];
  alerts: string[];
}

export const getEdgeMonitor = (): Promise<EdgeMonitor> =>
  authedGetJson<EdgeMonitor>("/research/edge-state");

/** Mean session excess in basis points; absent stays absent, never zero. */
export function formatExcessBps(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT;
  return `${(value * 10_000).toFixed(1)} bps`;
}

export function formatP(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT;
  return value.toFixed(4);
}

/** One line stating what the monitor means, without overclaiming direction. */
export function describeEdgeMonitor(monitor: EdgeMonitor): string {
  const promoted = monitor.books.filter((b) => b.promoted);
  const watched = promoted.length;
  const decayed = monitor.alerts.length;
  const insufficient = promoted.filter((b) => b.state === "insufficient").length;
  if (watched === 0) {
    return "No promoted edge is being watched yet.";
  }
  const names = decayed === 0 ? "none decayed" : `decayed: ${monitor.alerts.join(", ")}`;
  const history =
    insufficient === watched
      ? "forward history still accumulating"
      : insufficient > 0
        ? `${insufficient} of ${watched} still accumulating history`
        : "forward history measuring";
  return `${watched} promoted ${watched === 1 ? "edge" : "edges"} watched nightly · ${names} · ${history}`;
}

export function isPass(test: HypothesisTest): boolean {
  return test.verdict.toLowerCase() === "pass";
}

export function formatT(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT;
  return value.toFixed(2);
}

/** How close a test came, as a share of the bar it had to clear.
 *
 * Capped at 1 so the meter cannot imply a result exceeded a bar it failed, and
 * null when either side is unmeasured rather than defaulting to zero — a zero
 * would read as "measured, and nowhere near", which is a different claim from
 * "not measured".
 */
export function shareOfBar(test: HypothesisTest, fallbackBar: number): number | null {
  const t = test.detail?.best_recent_third_t;
  const bar = test.detail?.bar ?? fallbackBar;
  if (t === null || t === undefined || !Number.isFinite(t)) return null;
  if (!Number.isFinite(bar) || bar <= 0) return null;
  return Math.min(1, Math.abs(t) / bar);
}

/** One line stating what the record means, without overclaiming either way. */
export function describeRecord(summary: ResearchSummary): string {
  if (summary.tests === 0) {
    return "No hypothesis has been recorded yet.";
  }
  const tested = `${summary.tests} ${summary.tests === 1 ? "hypothesis" : "hypotheses"}`;
  const cells = `${summary.cells} ${summary.cells === 1 ? "statistic" : "statistics"}`;
  if (summary.passed === 0) {
    return `${tested} tested across ${cells}. None cleared the bar.`;
  }
  return `${tested} tested across ${cells}. ${summary.passed} cleared the bar.`;
}
