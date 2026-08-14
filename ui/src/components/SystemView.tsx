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
import { formatMoney, getCarryCycles, recordedCarry } from "../lib/portfolio";
import {
  describeRecord,
  formatT,
  getResearchRecord,
  isPass,
  shareOfBar,
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

export function SystemView() {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [reconciliation, setReconciliation] = useState<
    | { kind: "loading" }
    | { kind: "ok"; data: ReconciliationReport }
    | { kind: "error"; message: string }
  >({ kind: "loading" });
  const [automationOutcome, setAutomationOutcome] = useState<{ carry: number | null; cycles: number } | null>(null);
  const [research, setResearch] = useState<
    | { kind: "loading" }
    | { kind: "ok"; data: ResearchRecord }
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
    void refresh();
    void getReconciliation()
      .then((data) => setReconciliation({ kind: "ok", data }))
      .catch(() => setReconciliation({
        kind: "error",
        message: "Trading reconciliation is currently unavailable.",
      }));
    void getCarryCycles()
      .then(({ cycles }) => setAutomationOutcome({ carry: recordedCarry(cycles), cycles: cycles.length }))
      .catch(() => setAutomationOutcome(null));
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
          <span class="metric-kicker">Last 24 hours</span>
          <strong>{snapshot.production_24h.findings}</strong>
          <span class="metric-context">calls surfaced from {snapshot.production_24h.predictions} predictions</span>
        </article>
        <article class="primary-metric">
          <span class="metric-kicker">Automation outcome</span>
          <strong class={automationOutcome?.carry !== null && automationOutcome?.carry !== undefined && automationOutcome.carry < 0 ? "value-negative" : "value-positive"}>
            {automationOutcome?.carry === null || automationOutcome?.carry === undefined ? "—" : formatMoney(String(automationOutcome.carry))}
          </strong>
          <span class="metric-context">recorded net carry across {automationOutcome?.cycles ?? 0} completed cycles</span>
        </article>
      </section>

      <section class="surface-card attention-card">
        <div class="section-heading">
          <div><p class="eyebrow">Attention</p><h2>{unhealthy.length === 0 ? "Nothing needs you" : "Review these jobs"}</h2></div>
          <span class={`count-badge ${unhealthy.length > 0 ? "count-warning" : ""}`}>{unhealthy.length}</span>
        </div>
        {unhealthy.length === 0 ? (
          <div class="clean-empty success-empty">
            <strong>No active system issues</strong>
            <span>Quiet on-demand jobs are normal and are not treated as failures.</span>
          </div>
        ) : (
          <div class="issue-list">
            {unhealthy.map((issue) => (
              <article class="issue-row" key={issue.loop}>
                <span class={`status-dot-simple tone-${issue.flag === "failing" ? "dead" : "stale"}`} />
                <div><strong>{issue.loop}</strong><span>{issue.detail}</span></div>
              </article>
            ))}
          </div>
        )}
      </section>

      {research.kind === "loading" ? (
        <section class="surface-card research-record">
          <div class="section-heading">
            <div><p class="eyebrow">Strategy research</p><h2>What has been tested</h2></div>
          </div>
          <Loading label="Loading the research record…" />
        </section>
      ) : research.kind === "error" ? (
        <section class="surface-card research-record">
          <div class="section-heading">
            <div><p class="eyebrow">Strategy research</p><h2>Research record unavailable</h2></div>
            <button type="button" class="btn-secondary compact-button" onClick={loadResearch}>Try again</button>
          </div>
          <ErrorState message={research.message} detail={research.detail} />
        </section>
      ) : (
        <section class="surface-card research-record">
          <div class="section-heading">
            <div>
              <p class="eyebrow">Strategy research</p>
              <h2>What has been tested</h2>
            </div>
            <span class="count-badge">{research.data.summary.tests}</span>
          </div>

          <p class="settings-lead">{describeRecord(research.data.summary)}</p>

          {research.data.summary.tests === 0 ? (
            <div class="clean-empty">
              <strong>No hypothesis has been recorded here yet</strong>
              <span>
                Research runs append to the registry on the machine that runs them. Publish
                it with <code>ops/publish_research.py</code> to show the record here.
              </span>
            </div>
          ) : (
            <>
            <div class="research-bars">
              <div>
                <span class="metric-kicker">Significance bar</span>
                <strong>{formatT(research.data.summary.bar)}</strong>
                <span class="metric-context">
                  |t| a result must clear, from {research.data.summary.cells} statistics ever run
                </span>
              </div>
              <div>
                <span class="metric-kicker">Best result so far</span>
                <strong>{formatT(research.data.summary.best_t)}</strong>
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
                  {research.data.tests.map((entry) => {
                    const share = shareOfBar(entry, research.data.summary.bar);
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
                        <td>{formatT(entry.detail?.bar ?? research.data.summary.bar)}</td>
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
            </>
          )}
        </section>
      )}

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
