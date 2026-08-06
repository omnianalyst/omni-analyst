import { useEffect, useState } from "preact/hooks";
import {
  aggregateScorecard,
  regimeBadges,
  regimeMetrics,
  topReason,
  topSectors,
} from "../lib/console";
import { describeError } from "../lib/api";
import {
  briefingHeading,
  explainRefusal,
  formatHitRate,
  getBriefing,
  getRefusals,
  getScorecard,
  refusalTotal,
  type BriefingFinding,
  type RefusalCounts,
  type ScorecardRow,
} from "../lib/briefing";
import { getRegime, getSectors, type RegimeResponse, type SectorEntry } from "../lib/autonomous";
import { ErrorState } from "./ErrorState";
import { FindingCard } from "./FindingCard";
import { Loading } from "./Loading";

type Async<T> =
  | { kind: "loading" }
  | { kind: "ok"; data: T }
  | { kind: "error"; message: string; detail?: string };

const FINDING_PREVIEW = 5;
const SECTOR_PREVIEW = 3;

function useConsoleData() {
  const [findings, setFindings] = useState<Async<BriefingFinding[]>>({ kind: "loading" });
  const [scorecard, setScorecard] = useState<Async<ScorecardRow[]>>({ kind: "loading" });
  const [refusals, setRefusals] = useState<Async<RefusalCounts>>({ kind: "loading" });
  const [regime, setRegime] = useState<Async<RegimeResponse>>({ kind: "loading" });
  const [sectors, setSectors] = useState<Async<SectorEntry[]>>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    const run = <T,>(
      set: (v: Async<T>) => void,
      fetcher: () => Promise<T>,
    ) => {
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

    run(setFindings, getBriefing);
    run(setScorecard, getScorecard);
    run(setRefusals, getRefusals);
    run(setRegime, getRegime);
    run(setSectors, getSectors);

    return () => {
      cancelled = true;
    };
  }, []);

  return { findings, scorecard, refusals, regime, sectors };
}

function RegimeStrip({ regime }: { regime: Async<RegimeResponse> }) {
  if (regime.kind === "loading") return <Loading label="Loading regime\u2026" />;
  if (regime.kind === "error") {
    return <ErrorState message={regime.message} detail={regime.detail} />;
  }
  const r = regime.data;
  if (!r.value || !r.value.cycle_phase) {
    return (
      <p class="faint">
        No regime assessment yet. The macro loop abstains until FRED data is available.
      </p>
    );
  }
  const badges = regimeBadges(r.value);
  const metrics = regimeMetrics(r.value);
  return (
    <div class="regime-strip">
      <div class="regime-strip-badges">
        {badges.map((b) => (
          <span key={b.label} class={`badge ${b.tone ? "badge-" + b.tone : ""}`}>
            <span class="badge-label">{b.label}</span> {b.value}
          </span>
        ))}
      </div>
      <div class="regime-strip-metrics">
        {metrics.map((m) => (
          <span key={m.label} class="strip-metric">
            <span class="metric-label">{m.label}</span>
            <span class="metric-value">{m.value}</span>
            {m.sub ? <span class="metric-sub">{m.sub}</span> : null}
          </span>
        ))}
        <a class="bar-detail" href="/regime">full regime</a>
      </div>
    </div>
  );
}

function CalibrationGauge({ scorecard }: { scorecard: Async<ScorecardRow[]> }) {
  if (scorecard.kind === "loading") return <Loading label="Loading calibration\u2026" />;
  if (scorecard.kind === "error") {
    return <ErrorState message={scorecard.message} detail={scorecard.detail} />;
  }
  const summary = aggregateScorecard(scorecard.data);
  return (
    <div class="gauge">
      <span class="gauge-rate">{formatHitRate(summary.rate)}</span>
      <span class="metric-sub">
        {summary.resolved > 0
          ? `${summary.hits} of ${summary.resolved} resolved predictions`
          : "no resolved predictions yet"}
      </span>
      <a class="bar-detail" href="/briefing">scorecard</a>
    </div>
  );
}

function RefusalsBlock({ refusals }: { refusals: Async<RefusalCounts> }) {
  if (refusals.kind === "loading") return <Loading label="Loading refusals\u2026" />;
  if (refusals.kind === "error") {
    return <ErrorState message={refusals.message} detail={refusals.detail} />;
  }
  const total = refusalTotal(refusals.data);
  const top = topReason(refusals.data);
  if (total === 0) {
    return <p class="empty">Nothing considered and refused yet.</p>;
  }
  return (
    <div class="refusals-block">
      <span class="gauge-rate">{total}</span>
      <span class="metric-sub">
        {top ? `mostly: ${explainRefusal(top.reason)}` : "stayed quiet"}
      </span>
    </div>
  );
}

function SectorTeaser({ sectors }: { sectors: Async<SectorEntry[]> }) {
  if (sectors.kind === "loading") return <Loading label="Loading sectors\u2026" />;
  if (sectors.kind === "error") {
    return <ErrorState message={sectors.message} detail={sectors.detail} />;
  }
  if (sectors.data.length === 0) {
    return (
      <p class="empty">
        No sector scores. The scanner needs ETF prices (Polygon); sign in as the
        operator to view byo_only scores.
      </p>
    );
  }
  const leaders = topSectors(sectors.data, SECTOR_PREVIEW);
  return (
    <ul class="sector-teaser">
      {leaders.map((s) => (
        <li key={s.symbol} class="sector-teaser-row">
          <strong>{s.symbol}</strong>
          <div class="rs-bar-track">
            <div
              class="rs-bar-fill"
              style={{ width: `${Math.max(Math.min(s.score.rs_percentile * 100, 100), 0)}%` }}
            />
          </div>
          <span class="mono">{(s.score.rs_percentile * 100).toFixed(0)}</span>
        </li>
      ))}
    </ul>
  );
}

export function ConsoleView() {
  const { findings, scorecard, refusals, regime, sectors } = useConsoleData();

  return (
    <div class="console">
      <header class="page-head">
        <h1>Console</h1>
        <p class="muted">
          What the system is saying right now, against the macro backdrop. Silence
          in the feed is the conviction gate working, not an error.
        </p>
      </header>

      <RegimeStrip regime={regime} />

      <div class="console-grid">
        <section class="panel console-main">
          <h2 class="panel-title">
            Findings
            <a class="panel-clear" href="/briefing">full briefing</a>
          </h2>
          {findings.kind === "loading" ? <Loading label="Loading briefing\u2026" /> : null}
          {findings.kind === "error" ? (
            <ErrorState message={findings.message} detail={findings.detail} />
          ) : null}
          {findings.kind === "ok" && findings.data.length === 0 ? (
            <div class="empty">
              <p>
                <strong>{briefingHeading([])}</strong> &mdash; nothing was confident and
                calibrated enough to interrupt you with.
              </p>
            </div>
          ) : null}
          {findings.kind === "ok" && findings.data.length > 0 ? (
            <>
              <p class="gap-meta" style={{ padding: "12px 18px 0" }}>
                {briefingHeading(findings.data)}
              </p>
              <ul class="gaps" style={{ marginTop: "8px" }}>
                {findings.data.slice(0, FINDING_PREVIEW).map((f) => (
                  <FindingCard key={f.id} finding={f} />
                ))}
              </ul>
              {findings.data.length > FINDING_PREVIEW ? (
                <a class="panel-clear" href="/briefing" style={{ display: "inline-block", padding: "12px 18px" }}>
                  {findings.data.length - FINDING_PREVIEW} more in the full briefing
                </a>
              ) : null}
            </>
          ) : null}
        </section>

        <aside class="console-side">
          <section class="panel">
            <h2 class="panel-title">Calibration</h2>
            <CalibrationGauge scorecard={scorecard} />
          </section>
          <section class="panel">
            <h2 class="panel-title">Refusals</h2>
            <RefusalsBlock refusals={refusals} />
          </section>
          <section class="panel">
            <h2 class="panel-title">
              Sector leaders
              <a class="panel-clear" href="/sectors">scan</a>
            </h2>
            <SectorTeaser sectors={sectors} />
          </section>
        </aside>
      </div>
    </div>
  );
}
