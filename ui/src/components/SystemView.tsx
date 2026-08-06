import { useEffect } from "preact/hooks";
import { formatAge } from "../lib/age";
import {
  engineStatusWord,
  fillOutcomeClass,
  loopAgeLabel,
  loopCadence,
  scheduledLoopTier,
  unhealthyLoops,
  worstScheduledTier,
  type EngineStatusWord,
  type LoopStatus,
} from "../lib/system";
import {
  errorMessage,
  lastOkAt,
  refresh,
  state,
  status,
} from "../lib/systemStore";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

const WORD_TIER: Record<EngineStatusWord, string> = {
  nominal: "fresh",
  degraded: "aging",
  stalled: "stale",
  down: "dead",
  standby: "unknown",
};

function timestamp(iso: string | null): string {
  if (!iso) return "\u2014";
  return iso.slice(0, 19).replace("T", " ");
}

function LoopRow({ loop }: { loop: LoopStatus }) {
  const cadence = loopCadence(loop.loop);
  const tier = scheduledLoopTier(loop.age_seconds, loop.never_run);
  // On-demand loops staying quiet is the conviction gate working, not a stall;
  // spell that out so the row never reads as an unexplained silence.
  const note =
    cadence === "on_demand"
      ? loop.never_run
        ? "on demand, nothing to say yet"
        : "on demand, quiet is normal"
      : null;

  return (
    <tr class={`row ${cadence === "scheduled" ? "tier-" + tier : ""}`}>
      <td class="claim-type">
        {(cadence === "scheduled" || loop.never_run) && (
          <span class={`engine-dot tier-${loop.never_run ? "unknown" : tier}`} aria-hidden="true" />
        )}
        {loop.loop}
      </td>
      <td>
        <span class="gap-class">{cadence === "scheduled" ? "scheduled" : "on demand"}</span>
      </td>
      <td class="mono">{timestamp(loop.last_activity)}</td>
      <td class="num mono">{loopAgeLabel(loop)}</td>
      <td class="mono">{note}</td>
    </tr>
  );
}

function FillRow({ outcome, n }: { outcome: string; n: number }) {
  return (
    <tr>
      <td class="claim-type">
        <span class={`fill fill-${fillOutcomeClass(outcome)}`}>{outcome}</span>
      </td>
      <td class="num">{n}</td>
    </tr>
  );
}

export function SystemView() {
  useEffect(() => {
    void refresh();
  }, []);

  const s = status.value;
  const st = state.value;

  if (s === null) {
    if (st === "error") {
      return <ErrorState message={errorMessage.value ?? "status unreachable"} />;
    }
    return <Loading label="Loading system status\u2026" />;
  }

  const worst = worstScheduledTier(s.loops);
  const word = engineStatusWord(worst);
  const tierClass = WORD_TIER[word];
  const fillEntries = Object.entries(s.fill_last_hour).sort((a, b) => b[1] - a[1]);
  const unhealthy = unhealthyLoops(s.health);
  const sortedLoops = [...s.loops].sort((a, b) => {
    const order = (l: LoopStatus) => (loopCadence(l.loop) === "scheduled" ? 0 : 1);
    if (order(a) !== order(b)) return order(a) - order(b);
    return a.loop.localeCompare(b.loop);
  });

  return (
    <div>
      <header class="page-head">
        <h1>System status</h1>
        <p class="muted">
          Loop health read from the data itself: a loop that is alive writes rows,
          one that is dead stops. Scheduled loops are graded on cadence; on-demand
          loops are quiet by design.
        </p>
      </header>

      {st === "error" ? (
        <p class="error-line">
          connection lost &mdash; showing the last real snapshot
          {lastOkAt.value ? ` (${formatAge((Date.now() - lastOkAt.value) / 1000)})` : ""}
        </p>
      ) : null}

      <section class="panel">
        <h2 class="panel-title">
          Engine
          <button type="button" class="panel-clear" onClick={() => void refresh()}>
            refresh
          </button>
        </h2>
        <div class="regime-panel">
          <div class="regime-header">
            <span class={`badge badge-${tierClass === "fresh" ? "pos" : tierClass === "dead" || tierClass === "stale" ? "neg" : ""}`}>
              <span class={`engine-dot tier-${tierClass}`} aria-hidden="true" /> {word}
            </span>
          </div>
          <div class="metric-grid">
            <div class="metric">
              <span class="metric-label">Demand active</span>
              <span class="metric-value">{s.demand.active}</span>
              <span class="metric-sub">{s.demand.total} total</span>
            </div>
            <div class="metric">
              <span class="metric-label">Predictions (24h)</span>
              <span class="metric-value">{s.production_24h.predictions}</span>
            </div>
            <div class="metric">
              <span class="metric-label">Findings (24h)</span>
              <span class="metric-value">{s.production_24h.findings}</span>
              <span class="metric-sub">quiet days are healthy</span>
            </div>
            <div class="metric">
              <span class="metric-label">Assessed</span>
              <span class="metric-value mono" style={{ fontSize: "14px" }}>
                {timestamp(s.now)}
              </span>
            </div>
          </div>
        </div>
      </section>

      <section class="panel">
        <h2 class="panel-title">Loop health</h2>
        {unhealthy.length === 0 ? (
          <p class="empty">
            {s.health.overall === null
              ? "No loop has run yet -- health appears after the first iteration."
              : "All loops healthy. A failing or stale loop surfaces here with its reason."}
          </p>
        ) : (
          <ul class="health-list">
            {unhealthy.map((u) => (
              <li class={`health-item tier-${u.flag === "failing" ? "dead" : "stale"}`}>
                <span class={`engine-dot tier-${u.flag === "failing" ? "dead" : "stale"}`} aria-hidden="true" />
                <span class="claim-type">{u.loop}</span>
                <span class="mono">{u.detail}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section class="panel">
        <h2 class="panel-title">Loops</h2>
        <table class="coverage">
          <thead>
            <tr>
              <th>Loop</th>
              <th>Cadence</th>
              <th>Last activity</th>
              <th class="num">Age</th>
              <th>Note</th>
            </tr>
          </thead>
          <tbody>
            {sortedLoops.map((l) => (
              <LoopRow key={l.loop} loop={l} />
            ))}
          </tbody>
        </table>
      </section>

      <section class="panel">
        <h2 class="panel-title">Fill outcomes (last hour)</h2>
        {fillEntries.length === 0 ? (
          <p class="empty">No fill attempts in the last hour. The engine is idle on this path.</p>
        ) : (
          <table class="coverage">
            <thead>
              <tr>
                <th>Outcome</th>
                <th class="num">Count</th>
              </tr>
            </thead>
            <tbody>
              {fillEntries.map(([outcome, n]) => (
                <FillRow key={outcome} outcome={outcome} n={n} />
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
