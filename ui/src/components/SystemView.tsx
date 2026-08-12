import { useEffect, useState } from "preact/hooks";
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

  useEffect(() => {
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
  const scheduled = snapshot.loops.filter((loop) => loopCadence(loop.loop) === "scheduled");
  const healthyScheduled = scheduled.filter(
    (loop) => ["fresh", "recent"].includes(scheduledLoopTier(loop.age_seconds, loop.never_run)),
  ).length;
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
          <strong>{healthyScheduled}/{scheduled.length}</strong>
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
