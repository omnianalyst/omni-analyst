import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import {
  briefingHeading,
  getBriefing,
  getScorecard,
  type BriefingFinding,
  type ScorecardRow,
} from "../lib/briefing";
import { ErrorState } from "./ErrorState";
import { FindingCard } from "./FindingCard";
import { Loading } from "./Loading";
import { MarketBanner } from "./MarketBanner";

type Async<T> =
  | { kind: "loading" }
  | { kind: "ok"; data: T }
  | { kind: "error"; message: string; detail?: string };

// The honest front door. A market canopy, then the picks the system's conviction
// gate earned the right to surface -- each a claim card carrying its confidence,
// track record, reasoning chain and invalidation. The default is deliberately
// short: only what cleared calibration. An empty list is the discipline visible,
// not a broken feed; a long feed is what the conviction gate exists to prevent.
//
// Trust is woven in, not bolted on: the scorecard strip sits alongside the picks
// so the user reads "here is what we said" and "here is how often we were right"
// in one glance. The deep scorecard + refusals live on /briefing for whoever
// wants the full receipts.
function overallHitRate(rows: ScorecardRow[]): { rate: number | null; resolved: number; surfaced: number } {
  let surfaced = 0;
  let resolved = 0;
  let hits = 0;
  for (const r of rows) {
    surfaced += r.surfaced || 0;
    resolved += r.resolved || 0;
    hits += r.hits || 0;
  }
  return { rate: resolved > 0 ? hits / resolved : null, resolved, surfaced };
}

export function BriefView() {
  const [findings, setFindings] = useState<Async<BriefingFinding[]>>({ kind: "loading" });
  const [scorecard, setScorecard] = useState<Async<ScorecardRow[]>>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    setFindings({ kind: "loading" });
    setScorecard({ kind: "loading" });

    void (async () => {
      try {
        const d = await getBriefing();
        if (!cancelled) setFindings({ kind: "ok", data: d });
      } catch (err) {
        if (!cancelled) {
          const { message, detail } = describeError(err);
          setFindings({ kind: "error", message, detail });
        }
      }
    })();

    void (async () => {
      try {
        const d = await getScorecard();
        if (!cancelled) setScorecard({ kind: "ok", data: d });
      } catch (err) {
        // Scorecard is operator-only; an anonymous viewer simply gets no strip.
        if (!cancelled) setScorecard({ kind: "ok", data: [] });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const picksOk = findings.kind === "ok" ? findings.data : [];
  const track = scorecard.kind === "ok" ? overallHitRate(scorecard.data) : null;

  return (
    <div class="brief-view">
      <MarketBanner />

      {track && (track.resolved > 0 || track.surfaced > 0) ? (
        <p class="track-strip">
          <strong>Track record:</strong>{" "}
          {track.rate !== null
            ? `right ~${Math.round(track.rate * 100)}% of the time across ${track.resolved} resolved call${track.resolved === 1 ? "" : "s"}`
            : `${track.surfaced} call${track.surfaced === 1 ? "" : "s"} surfaced, awaiting resolution to calibrate`}
          <span class="muted"> &middot; full receipts on the Briefing page</span>
        </p>
      ) : null}

      <section class="panel">
        <h2 class="panel-title">Today&apos;s calls</h2>
        <p class="panel-sub muted">
          Only what cleared the calibrated conviction bar. Direction is analysis, not advice.
        </p>
        {findings.kind === "loading" ? <Loading label="Loading calls\u2026" /> : null}
        {findings.kind === "error" ? (
          <ErrorState message={findings.message} detail={findings.detail} />
        ) : null}
        {findings.kind === "ok" && findings.data.length === 0 ? (
          <div class="empty">
            <p>
              <strong>{briefingHeading([])}</strong> &mdash; nothing was confident
              and calibrated enough to surface. That is the system working, not an
              empty feed.
            </p>
          </div>
        ) : null}
        {picksOk.length > 0 ? (
          <ul class="claims-list">
            {picksOk.map((f) => (
              <FindingCard key={f.id} finding={f} />
            ))}
          </ul>
        ) : null}
      </section>
    </div>
  );
}
