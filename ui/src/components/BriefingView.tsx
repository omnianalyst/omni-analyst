import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import {
  briefingHeading,
  getBriefing,
  getRefusals,
  getScorecard,
  refusalTotal,
  type BriefingFinding,
  type RefusalCounts,
  type ScorecardRow,
} from "../lib/briefing";
import { ErrorState } from "./ErrorState";
import { FindingCard } from "./FindingCard";
import { Loading } from "./Loading";
import { RefusalList } from "./RefusalList";
import { ScorecardTable } from "./ScorecardTable";

type Async<T> =
  | { kind: "loading" }
  | { kind: "ok"; data: T }
  | { kind: "error"; message: string; detail?: string };

export function BriefingView() {
  const [findings, setFindings] = useState<Async<BriefingFinding[]>>({
    kind: "loading",
  });
  const [scorecard, setScorecard] = useState<Async<ScorecardRow[]>>({
    kind: "loading",
  });
  const [refusals, setRefusals] = useState<Async<RefusalCounts>>({
    kind: "loading",
  });

  useEffect(() => {
    let cancelled = false;
    setFindings({ kind: "loading" });
    setScorecard({ kind: "loading" });
    setRefusals({ kind: "loading" });

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
        if (!cancelled) {
          const { message, detail } = describeError(err);
          setScorecard({ kind: "error", message, detail });
        }
      }
    })();

    void (async () => {
      try {
        const d = await getRefusals();
        if (!cancelled) setRefusals({ kind: "ok", data: d });
      } catch (err) {
        if (!cancelled) {
          const { message, detail } = describeError(err);
          setRefusals({ kind: "error", message, detail });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const showEmptyHonest =
    findings.kind === "ok" && findings.data.length === 0;
  const refusedTotal =
    refusals.kind === "ok" ? refusalTotal(refusals.data) : null;

  return (
    <div class="briefing-view">
      <header class="page-head">
        <h1>Briefing</h1>
        <p class="muted">
          What the system chose to say unprompted. Silence is a real answer here
          &mdash; the refusals below are the system working, not an error state.
        </p>
      </header>

      <section class="panel">
        <h2 class="panel-title">Calls</h2>
        {findings.kind === "loading" ? (
          <Loading label="Loading briefing\u2026" />
        ) : null}
        {findings.kind === "error" ? (
          <ErrorState message={findings.message} detail={findings.detail} />
        ) : null}
        {showEmptyHonest ? (
          <div class="empty">
            <p>
              <strong>{briefingHeading([])}</strong> &mdash; nothing was
              confident and calibrated enough to interrupt you with.
            </p>
            {refusedTotal !== null && refusedTotal > 0 ? (
              <p class="mono">
                {refusedTotal} call{refusedTotal === 1 ? "" : "s"} were
                considered and stayed quiet &mdash; see the refusals below for
                why.
              </p>
            ) : null}
            {refusedTotal === 0 ? (
              <p class="mono">Nothing has been considered yet either.</p>
            ) : null}
          </div>
        ) : null}
        {findings.kind === "ok" && findings.data.length > 0 ? (
          <>
            <p class="gap-meta" style={{ padding: "12px 18px 0" }}>
              {briefingHeading(findings.data)}
            </p>
            <ul class="gaps" style={{ marginTop: "8px" }}>
              {findings.data.map((f) => (
                <FindingCard key={f.id} finding={f} />
              ))}
            </ul>
          </>
        ) : null}
      </section>

      <section class="panel">
        <h2 class="panel-title">Scorecard &mdash; how often it has been right</h2>
        {scorecard.kind === "loading" ? (
          <Loading label="Loading scorecard\u2026" />
        ) : null}
        {scorecard.kind === "error" ? (
          <ErrorState message={scorecard.message} detail={scorecard.detail} />
        ) : null}
        {scorecard.kind === "ok" ? <ScorecardTable rows={scorecard.data} /> : null}
      </section>

      <section class="panel">
        <h2 class="panel-title">Refusals &mdash; how often it stayed quiet, and why</h2>
        {refusals.kind === "loading" ? (
          <Loading label="Loading refusals\u2026" />
        ) : null}
        {refusals.kind === "error" ? (
          <ErrorState message={refusals.message} detail={refusals.detail} />
        ) : null}
        {refusals.kind === "ok" ? <RefusalList counts={refusals.data} /> : null}
      </section>
    </div>
  );
}
