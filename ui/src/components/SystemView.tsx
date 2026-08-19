import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import { formatAge } from "../lib/age";
import {
  engineStatusWord,
  fillOutcomeClass,
  loopAgeLabel,
  loopCadence,
  scheduledLoopTier,
  unhealthyLoops,
  worstScheduledTier,
  type LoopStatus,
} from "../lib/system";
import { errorMessage, lastOkAt, refresh, state, status } from "../lib/systemStore";
import {
  describeEdgeMonitor,
  describeRecord,
  formatExcessBps,
  formatP,
  formatT,
  getEdgeMonitor,
  getResearchRecord,
  isPass,
  shareOfBar,
  type EdgeMonitor,
  type ResearchRecord,
} from "../lib/research";
import {
  describeReconciliation,
  formatTimestamp,
  getReconciliation,
  type ReconciliationReport,
} from "../lib/trading";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

function timestamp(iso: string | null): string {
  if (!iso) return "—";
  return iso.slice(0, 19).replace("T", " ");
}

function interval(seconds: number | null): string {
  if (seconds === null) return "Not recorded";
  if (seconds < 60) return `Every ${seconds} seconds`;
  if (seconds < 3600) return `Every ${seconds / 60} minutes`;
  if (seconds < 86400) return `Every ${seconds / 3600} hours`;
  if (seconds === 86400) return "Every day";
  return `Every ${seconds / 86400} days`;
}

function LoopRow({ loop }: { loop: LoopStatus }) {
  const cadence = loopCadence(loop.loop);
  const tier = scheduledLoopTier(loop.age_seconds, loop.never_run);
  return (
    <tr>
      <td><strong>{loop.loop}</strong></td>
      <td><span class={`status-dot-simple tone-${cadence === "scheduled" ? tier : "quiet"}`} />{cadence === "scheduled" ? "Scheduled" : "On demand"}</td>
      <td>{timestamp(loop.last_activity)}</td>
      <td>{loopAgeLabel(loop)}</td>
    </tr>
  );
}

// The strategy research record, compressed to the one line an operator needs
// unless they ask for more: how much has been tested and how much of it held
// up. The rising bar and the permanent table are behind the disclosure.
function ResearchSection({
  research,
  onRetry,
}: {
  research:
    | { kind: "loading" }
    | { kind: "ok"; data: ResearchRecord }
    | { kind: "error"; message: string; detail?: string };
  onRetry: () => void;
}) {
  const [open, setOpen] = useState(false);

  if (research.kind === "loading") {
    return (
      <section class="surface-card research-record">
        <Loading label="Loading the research record…" />
      </section>
    );
  }
  if (research.kind === "error") {
    return (
      <section class="surface-card research-record">
        <div class="section-heading">
          <div><p class="eyebrow">Strategy research</p><h2>Research record unavailable</h2></div>
          <button type="button" class="btn-secondary compact-button" onClick={onRetry}>Try again</button>
        </div>
        <ErrorState message={research.message} detail={research.detail} />
      </section>
    );
  }

  const { summary, tests } = research.data;
  if (summary.tests === 0) {
    return (
      <section class="surface-card research-record">
        <p class="research-one-line">
          Strategy research · no hypothesis recorded here yet. Runs append to the registry on the
          machine that runs them; publish with <code>ops/publish_research.py</code>.
        </p>
      </section>
    );
  }

  const cleared = tests.filter(isPass).length;
  return (
    <section class="surface-card research-record">
      <button
        type="button"
        class="disclosure-button research-disclosure"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span>
          What has been tested · {summary.tests} hypotheses · {cleared} cleared the bar · best |t| {formatT(summary.best_t)}
        </span>
        <span aria-hidden="true">{open ? "−" : "+"}</span>
      </button>
      {open ? (
        <div class="detail-drawer">
          <div class="detail-block">
            <p class="settings-lead">{describeRecord(summary)}</p>
            <div class="research-bars">
              <div>
                <span class="metric-kicker">Significance bar</span>
                <strong>{formatT(summary.bar)}</strong>
                <span class="metric-context">
                  |t| a result must clear, from {summary.cells} statistics ever run
                </span>
              </div>
              <div>
                <span class="metric-kicker">Best result so far</span>
                <strong>{formatT(summary.best_t)}</strong>
                <span class="metric-context">highest |t| on the most recent third</span>
              </div>
            </div>

            <div class="responsive-table">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>Hypothesis</th>
                    <th>Data</th>
                    <th>Statistics</th>
                    <th>Best |t|</th>
                    <th>Bar</th>
                    <th>Verdict</th>
                  </tr>
                </thead>
                <tbody>
                  {tests.map((entry) => {
                    const share = shareOfBar(entry, summary.bar);
                    const passed = isPass(entry);
                    return (
                      <tr key={`${entry.name}-${entry.recorded_at}`}>
                        <td><strong>{entry.name}</strong></td>
                        <td><small>{entry.source}</small></td>
                        <td>{entry.cells}</td>
                        <td>
                          {formatT(entry.detail?.best_recent_third_t)}
                          {share === null ? null : (
                            <span
                              class="research-meter"
                              style={{ "--share": String(share) }}
                              aria-hidden="true"
                            />
                          )}
                        </td>
                        <td>{formatT(entry.detail?.bar ?? summary.bar)}</td>
                        <td>
                          <span class={`fill fill-${passed ? "good" : "blocked"}`}>
                            {passed ? "cleared" : "did not clear"}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <p class="research-note">
              The bar is <code>sqrt(2 ln N)</code> over every statistic this system has ever
              computed, never below 2.5. It rises as the search widens, so a result found
              after a long search must be stronger than the same result found early. A test
              recorded here is permanent — retiring a failed hypothesis from the record would
              make every later result look more significant than it is.
            </p>
          </div>
        </div>
      ) : null}
    </section>
  );
}

// The edge decay monitor: the forward verdict on every promoted rule, one
// row per shadow book, refreshed nightly after the scoring pass. It sits next
// to the research record because the two answer one question from opposite
// ends -- what justified the promotion, and whether it still holds.
function EdgeSection({
  monitor,
}: {
  monitor:
    | { kind: "loading" }
    | { kind: "ok"; data: EdgeMonitor }
    | { kind: "error"; message: string; detail?: string };
}) {
  if (monitor.kind === "loading") {
    return (
      <section class="surface-card research-record">
        <Loading label="Loading the edge decay monitor…" />
      </section>
    );
  }
  if (monitor.kind === "error") {
    return (
      <section class="surface-card research-record">
        <div class="section-heading">
          <div><p class="eyebrow">Edge decay</p><h2>Edge monitor unavailable</h2></div>
        </div>
        <ErrorState message={monitor.message} detail={monitor.detail} />
      </section>
    );
  }
  const { books, alerts } = monitor.data;
  if (books.length === 0) {
    return (
      <section class="surface-card research-record">
        <p class="research-one-line">
          Edge decay · no shadow book has a recorded state yet. The monitor writes its
          first row the night after the scoring pass runs.
        </p>
      </section>
    );
  }

  const stateTone: Record<string, string> = {
    holding: "fresh",
    unconfirmed: "stale",
    decayed: "dead",
    insufficient: "quiet",
  };
  return (
    <section class={`surface-card research-record${alerts.length > 0 ? " attention-card" : ""}`}>
      <div class="section-heading">
        <div>
          <p class="eyebrow">Edge decay</p>
          <h2>{alerts.length > 0 ? "A promoted edge has decayed" : "Promoted edges"}</h2>
        </div>
        {alerts.length > 0 ? (
          <span class="count-badge count-warning">{alerts.length}</span>
        ) : null}
      </div>
      <p class="research-one-line">{describeEdgeMonitor(monitor.data)}</p>
      <div class="responsive-table">
        <table class="data-table">
          <thead>
            <tr>
              <th>Book</th>
              <th>Role</th>
              <th>State</th>
              <th>Mean session excess</th>
              <th>Decay p</th>
              <th>Recent window</th>
            </tr>
          </thead>
          <tbody>
            {books.map((book) => (
              <tr key={book.book}>
                <td><strong>{book.book}</strong></td>
                <td>{book.promoted ? "promoted edge" : "control"}</td>
                <td>
                  <span class={`status-dot-simple tone-${stateTone[book.state] ?? "quiet"}`} />
                  {book.state === "insufficient" ? "insufficient history" : book.state}
                </td>
                <td>
                  {book.state === "insufficient"
                    ? book.reason ?? "—"
                    : formatExcessBps(book.mean_session_excess)}
                </td>
                <td>{formatP(book.decay_p)}</td>
                <td>
                  {book.window_start === null || book.window_end === null
                    ? "—"
                    : `${book.window_start} to ${book.window_end}`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p class="research-note">
        Each night the scoring pass measures every shadow book against its benchmark; this
        monitor judges the most recent third of that forward record. Significantly negative
        excess is decayed — the promoted claim reversed. Non-positive but inconclusive is
        unconfirmed, not dead: a quiet edge is not a dead edge. History cannot be backfilled,
        so an insufficient row is the monitor confirming it looked.
      </p>
    </section>
  );
}

// The live process board: every scheduled unit with a dot and a last-heard
// age. This is the "what is running right now" view -- deliberately compact,
// one row per process, no tables. The full result/error detail stays in the
// technical drawer below; a board that showed everything would be a log, not
// a board.
function ProcessBoard({
  loops,
}: {
  loops: import("../lib/system").LoopHealthEntry[];
}) {
  const tone: Record<string, string> = {
    ok: "fresh",
    stale: "stale",
    failing: "dead",
    never_run: "quiet",
  };
  const word: Record<string, string> = {
    ok: "healthy",
    stale: "late",
    failing: "failing",
    never_run: "never run",
  };
  return (
    <section class="surface-card process-board" aria-label="Background processes">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Background processes</p>
          <h2>Currently monitored</h2>
        </div>
        <span class="count-badge">{loops.length}</span>
      </div>
      <div class="process-grid">
        {loops.map((loop) => (
          <div class={`process-chip tone-${tone[loop.state] ?? "quiet"}`} key={loop.loop}>
            <span class={`status-dot-simple tone-${tone[loop.state] ?? "quiet"}`} aria-hidden="true" />
            <div>
              <strong>{loop.loop}</strong>
              <small>{word[loop.state] ?? loop.state}</small>
            </div>
            <span class="process-age">
              {loop.last_success_at ?? loop.last_failure_at
                ? formatAge(
                    (Date.now() -
                      Date.parse(
                        (loop.last_success_at ?? loop.last_failure_at) as string,
                      )) / 1000,
                  )
                : "—"}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

export function SystemView() {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [reconciliation, setReconciliation] = useState<
    | { kind: "loading" }
    | { kind: "ok"; data: ReconciliationReport }
    | { kind: "error"; message: string }
  >({ kind: "loading" });
  const [research, setResearch] = useState<
    | { kind: "loading" }
    | { kind: "ok"; data: ResearchRecord }
    | { kind: "error"; message: string; detail?: string }
  >({ kind: "loading" });
  const [edges, setEdges] = useState<
    | { kind: "loading" }
    | { kind: "ok"; data: EdgeMonitor }
    | { kind: "error"; message: string; detail?: string }
  >({ kind: "loading" });

  function loadResearch() {
    setResearch({ kind: "loading" });
    void getResearchRecord()
      .then((data) => setResearch({ kind: "ok", data }))
      .catch((error) => {
        const { message, detail } = describeError(error);
        setResearch({ kind: "error", message, detail });
      });
  }

  useEffect(() => {
    loadResearch();
    void getEdgeMonitor()
      .then((data) => setEdges({ kind: "ok", data }))
      .catch((error) => {
        const { message, detail } = describeError(error);
        setEdges({ kind: "error", message, detail });
      });
    void refresh();
    void getReconciliation()
      .then((data) => setReconciliation({ kind: "ok", data }))
      .catch(() => setReconciliation({
        kind: "error",
        message: "Trading reconciliation is currently unavailable.",
      }));
  }, []);

  const snapshot = status.value;
  const storeState = state.value;

  if (snapshot === null) {
    if (storeState === "error") {
      return <ErrorState message={errorMessage.value ?? "System status is unreachable."} />;
    }
    return <Loading label="Checking Omni…" />;
  }

  const worst = worstScheduledTier(snapshot.loops);
  const word = engineStatusWord(worst);
  const unhealthy = unhealthyLoops(snapshot.health);
  const healthyScheduled = snapshot.health.loops.filter((loop) => loop.state === "ok").length;
  const fillEntries = Object.entries(snapshot.fill_last_hour).sort((a, b) => b[1] - a[1]);
  const disconnected = storeState === "error";
  const critical = word === "inactive" || word === "stalled";
  const attention = disconnected || critical || unhealthy.length > 0 || word === "degraded";
  const headline = attention ? "Omni needs attention" : "Everything is running";
  const detail = disconnected
    ? "The live status connection was lost. This page is showing the last real snapshot."
    : unhealthy.length > 0
      ? `${unhealthy.length} background ${unhealthy.length === 1 ? "job needs" : "jobs need"} attention.`
      : critical
        ? "A scheduled background job has stopped reporting on time."
        : "Research, coverage, and monitoring are operating on schedule.";

  return (
    <div class="system-view product-page">
      <header class={`compact-status-heading ${attention ? "health-attention" : "health-healthy"}`}>
        <div>
          <div class="health-title-row">
            <span class="health-orb" aria-hidden="true" />
            <div>
              <h1>System</h1>
              <p>{headline} · {detail}</p>
            </div>
          </div>
        </div>
        <button type="button" class="btn-secondary compact-button" onClick={() => void refresh()}>
          Check again
        </button>
      </header>

      {disconnected ? (
        <p class="inline-warning system-warning">
          Last live update {lastOkAt.value ? formatAge((Date.now() - lastOkAt.value) / 1000) : "unknown"}.
        </p>
      ) : null}

      <section class="primary-metrics system-metrics" aria-label="System summary">
        <article class="primary-metric">
          <span class="metric-kicker">Automation</span>
          <strong>{healthyScheduled}/{snapshot.health.loops.length}</strong>
          <span class="metric-context">scheduled jobs reporting normally</span>
        </article>
        <article class="primary-metric">
          <span class="metric-kicker">Claim store</span>
          <strong>{snapshot.claims.total.toLocaleString()}</strong>
          <span class="metric-context">
            +{snapshot.claims.last_24h.toLocaleString()} observations in the last day
          </span>
        </article>
        <article class="primary-metric">
          <span class="metric-kicker">Demand</span>
          <strong>{snapshot.demand.active}</strong>
          <span class="metric-context">active coverage requests being worked</span>
        </article>
        <article class="primary-metric">
          <span class="metric-kicker">Last 24 hours</span>
          <strong>{snapshot.production_24h.findings}</strong>
          <span class="metric-context">calls surfaced from {snapshot.production_24h.predictions} predictions</span>
        </article>
      </section>

      {unhealthy.length === 0 ? (
        <p class="quiet-line">No active system issues. Quiet on-demand jobs are normal.</p>
      ) : (
        <section class="surface-card attention-card">
          <div class="section-heading">
            <div><p class="eyebrow">Attention</p><h2>Review these jobs</h2></div>
            <span class="count-badge count-warning">{unhealthy.length}</span>
          </div>
          <div class="issue-list">
            {unhealthy.map((issue) => (
              <article class="issue-row" key={issue.loop}>
                <span class={`status-dot-simple tone-${issue.flag === "failing" ? "dead" : "stale"}`} />
                <div><strong>{issue.loop}</strong><span>{issue.detail}</span></div>
              </article>
            ))}
          </div>
        </section>
      )}

      <ProcessBoard loops={snapshot.health.loops} />
      <EdgeSection monitor={edges} />
      <ResearchSection research={research} onRetry={loadResearch} />

      <button
        type="button"
        class="disclosure-button"
        aria-expanded={detailsOpen}
        onClick={() => setDetailsOpen((open) => !open)}
      >
        <span>{detailsOpen ? "Hide technical details" : "View technical details"}</span>
        <span aria-hidden="true">{detailsOpen ? "−" : "+"}</span>
      </button>

      {detailsOpen ? (
        <div class="detail-drawer">
          <section class="detail-block">
            <div class="section-heading">
              <div><p class="eyebrow">Trading infrastructure</p><h2>Venue reconciliation</h2></div>
            </div>
            {reconciliation.kind === "loading" ? (
              <Loading label="Checking trading venues…" />
            ) : reconciliation.kind === "error" ? (
              <p class="inline-warning">{reconciliation.message}</p>
            ) : reconciliation.data.venues.length === 0 ? (
              <p class="clean-empty">No venue checks have been recorded.</p>
            ) : (
              <div class="verification-list">
                {reconciliation.data.venues.map((venue) => {
                  const presentation = describeReconciliation(venue.status);
                  return (
                    <div class={`verification-row tone-${presentation.tone}`} key={venue.venue}>
                      <span class="health-orb" aria-hidden="true" />
                      <strong>{venue.venue}</strong>
                      <span>{presentation.label}</span>
                      <small>{formatTimestamp(venue.checked_at)}</small>
                    </div>
                  );
                })}
              </div>
            )}
          </section>
          <section class="detail-block">
            <div class="section-heading"><div><p class="eyebrow">Scheduled units</p><h2>Last execution result</h2></div><small>Assessed {timestamp(snapshot.now)}</small></div>
            <div class="responsive-table">
              <table class="data-table">
                <thead><tr><th>Job</th><th>Result</th><th>Expected</th><th>Last success</th><th>Last error</th></tr></thead>
                <tbody>
                  {snapshot.health.loops.map((loop) => (
                    <tr key={loop.loop}>
                      <td><strong>{loop.loop}</strong></td>
                      <td>
                        <span class={`fill fill-${loop.state === "ok" ? "good" : loop.state === "never_run" ? "neutral" : "failed"}`}>
                          {loop.last_status ?? "never run"}
                        </span>
                        {loop.last_result ? <small>{loop.last_result}</small> : null}
                      </td>
                      <td>{interval(loop.expected_interval_seconds)}</td>
                      <td>{timestamp(loop.last_success_at)}</td>
                      <td>
                        {loop.last_error ?? "None recorded"}
                        {loop.last_failure_at ? <small>{timestamp(loop.last_failure_at)}</small> : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section class="detail-block">
            <div class="section-heading"><div><p class="eyebrow">Background jobs</p><h2>Activity by loop</h2></div><small>Assessed {timestamp(snapshot.now)}</small></div>
            <div class="responsive-table">
              <table class="data-table">
                <thead><tr><th>Job</th><th>Mode</th><th>Last activity</th><th>Age</th></tr></thead>
                <tbody>{snapshot.loops.map((loop) => <LoopRow key={loop.loop} loop={loop} />)}</tbody>
              </table>
            </div>
          </section>
          <section class="detail-block">
            <div class="section-heading"><div><p class="eyebrow">Coverage</p><h2>Fill outcomes</h2></div><small>Last hour</small></div>
            {fillEntries.length === 0 ? (
              <p class="clean-empty">No coverage fills were attempted in the last hour.</p>
            ) : (
              <div class="outcome-grid">
                {fillEntries.map(([outcome, count]) => (
                  <div class="outcome-card" key={outcome}>
                    <span class={`fill fill-${fillOutcomeClass(outcome)}`}>{outcome}</span>
                    <strong>{count}</strong>
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      ) : null}
    </div>
  );
}
