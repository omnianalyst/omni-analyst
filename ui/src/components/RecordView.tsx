import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import {
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
import { Hint } from "./Hint";
import { Loading } from "./Loading";
import { RefusalList } from "./RefusalList";
import { ScorecardTable } from "./ScorecardTable";

// The receipts. Today's page says what the system currently believes; this one
// says how often it has been right and what it declined to say -- the numerator
// and the denominator, which only mean something together. A product that
// stores only what it surfaced can claim any accuracy it likes.

type Async<T> =
  | { kind: "loading" }
  | { kind: "ok"; data: T }
  | { kind: "error"; message: string; detail?: string };

export function RecordView() {
  const [calls, setCalls] = useState<Async<BriefingFinding[]>>({ kind: "loading" });
  const [scorecard, setScorecard] = useState<Async<ScorecardRow[]>>({ kind: "loading" });
  const [refusals, setRefusals] = useState<Async<RefusalCounts>>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    const run = <T,>(set: (v: Async<T>) => void, fetcher: () => Promise<T>) => {
      fetcher()
        .then((d) => {
          if (!cancelled) set({ kind: "ok", data: d });
        })
        .catch((err) => {
          if (!cancelled) {
            const { message, detail } = describeError(err);
            set({ kind: "error", message, detail });
          }
        });
    };

    run(setCalls, getBriefing);
    run(setScorecard, getScorecard);
    run(setRefusals, getRefusals);

    return () => {
      cancelled = true;
    };
  }, []);

  const refusedTotal = refusals.kind === "ok" ? refusalTotal(refusals.data) : null;

  return (
    <div class="record-view">
      <header class="page-head">
        <h1>Track record</h1>
        <p class="muted">
          How often the system has been right, and what it chose not to say.
          Both halves are here on purpose: an accuracy figure without its{" "}
          <Hint term="refusal">refusals</Hint> is a number you cannot check.
        </p>
      </header>

      <section class="panel">
        <h2 class="panel-title">
          Accuracy by method
        </h2>
        <p class="panel-sub muted">
          A <Hint term="hit_rate">hit rate</Hint> appears only once at least ten
          calls of a class have played out. Fewer than that is noise wearing a
          percentage sign.
        </p>
        {scorecard.kind === "loading" ? <Loading label="Loading accuracy…" /> : null}
        {scorecard.kind === "error" ? (
          <ErrorState message={scorecard.message} detail={scorecard.detail} />
        ) : null}
        {scorecard.kind === "ok" ? <ScorecardTable rows={scorecard.data} /> : null}
      </section>

      <section class="panel">
        <h2 class="panel-title">Refusals — what it declined to say, and why</h2>
        {refusals.kind === "loading" ? <Loading label="Loading refusals…" /> : null}
        {refusals.kind === "error" ? (
          <ErrorState message={refusals.message} detail={refusals.detail} />
        ) : null}
        {refusals.kind === "ok" ? <RefusalList counts={refusals.data} /> : null}
      </section>

      <section class="panel">
        <h2 class="panel-title">
          Open calls
          <a class="panel-clear" href="/">today&apos;s read</a>
        </h2>
        <p class="panel-sub muted">
          Every call currently standing, with the evidence and the price that
          would prove each one wrong.
        </p>
        {calls.kind === "loading" ? <Loading label="Loading calls…" /> : null}
        {calls.kind === "error" ? (
          <ErrorState message={calls.message} detail={calls.detail} />
        ) : null}
        {calls.kind === "ok" && calls.data.length === 0 ? (
          <div class="empty">
            <p>
              <strong>No calls are standing</strong> — nothing has cleared the
              conviction gate.
            </p>
            {refusedTotal !== null && refusedTotal > 0 ? (
              <p class="mono">
                {refusedTotal} {refusedTotal === 1 ? "was" : "were"} considered
                and turned down — the reasons are above.
              </p>
            ) : null}
            {refusedTotal === 0 ? (
              <p class="mono">Nothing has been considered yet either.</p>
            ) : null}
          </div>
        ) : null}
        {calls.kind === "ok" && calls.data.length > 0 ? (
          <ul class="claims-list">
            {calls.data.map((f) => (
              <FindingCard key={f.id} finding={f} />
            ))}
          </ul>
        ) : null}
      </section>
    </div>
  );
}
