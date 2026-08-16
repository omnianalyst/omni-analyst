import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import {
  aggregateScorecard,
  regimeBadges,
  regimeMetrics,
  topReason,
  topSectors,
} from "../lib/console";
import {
  briefingHeading,
  explainRefusal,
  formatHitRate,
  getBriefing,
  getRefusals,
  getScorecard,
  refusalTotal,
  unprovenCount,
  type BriefingFinding,
  type RefusalCounts,
  type ScorecardRow,
} from "../lib/briefing";
import { getRegime, getSectors, type RegimeResponse, type SectorEntry } from "../lib/autonomous";
import { ErrorState } from "./ErrorState";
import { FindingCard } from "./FindingCard";
import { Hint } from "./Hint";
import { Loading } from "./Loading";
import { MarketBanner } from "./MarketBanner";

// The front door: what the system says right now, framed by the macro backdrop
// it deduced it from, with its own track record beside it.
//
// This page was three -- "/" showed the calls, "/console" showed the same calls
// under a different heading with a sidebar, "/briefing" showed them a third time
// with the scorecard. One dataset, three names for it, and a reader who could
// not tell what distinguished them. Now there are two pages with genuinely
// different jobs: this one says what the system currently believes, and the
// track record page shows how often that belief has been right and what it
// declined to say. Silence in the feed is the conviction gate working.

type Async<T> =
  | { kind: "loading" }
  | { kind: "ok"; data: T }
  | { kind: "error"; message: string; detail?: string };

const SECTOR_PREVIEW = 3;

function useTodayData() {
  const [calls, setCalls] = useState<Async<BriefingFinding[]>>({ kind: "loading" });
  const [scorecard, setScorecard] = useState<Async<ScorecardRow[]>>({ kind: "loading" });
  const [refusals, setRefusals] = useState<Async<RefusalCounts>>({ kind: "loading" });
  const [regime, setRegime] = useState<Async<RegimeResponse>>({ kind: "loading" });
  const [sectors, setSectors] = useState<Async<SectorEntry[]>>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    const run = <T,>(set: (v: Async<T>) => void, fetcher: () => Promise<T>) => {
      fetcher()
        .then((d) => {
          if (!cancelled) set({ kind: "ok", data: d });
        })
        .catch((err) => {
          // A failed fetch is an error state, never an empty one. Rendering a
          // 500 as a calm "nothing to report" is the single most dishonest
          // thing this UI could do, in the one product whose whole claim is
          // that silence means something.
          if (!cancelled) {
            const { message, detail } = describeError(err);
            set({ kind: "error", message, detail });
          }
        });
    };

    run(setCalls, getBriefing);
    run(setScorecard, getScorecard);
    run(setRefusals, getRefusals);
    run(setRegime, getRegime);
    run(setSectors, getSectors);

    return () => {
      cancelled = true;
    };
  }, []);

  return { calls, scorecard, refusals, regime, sectors };
}

function RegimeStrip({ regime }: { regime: Async<RegimeResponse> }) {
  if (regime.kind === "loading") return <Loading label="Loading regime…" />;
  if (regime.kind === "error") {
    return <ErrorState message={regime.message} detail={regime.detail} />;
  }
  const r = regime.data;
  if (!r.value || !r.value.cycle_phase) {
    return (
      <p class="faint">
        No regime read yet — the system waits for enough macro data before
        calling one.
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

function TrackRecordPanel({ scorecard }: { scorecard: Async<ScorecardRow[]> }) {
  if (scorecard.kind === "loading") return <Loading label="Loading track record…" />;
  if (scorecard.kind === "error") {
    return <ErrorState message={scorecard.message} detail={scorecard.detail} />;
  }
  const summary = aggregateScorecard(scorecard.data);
  return (
    <div class="gauge">
      <span class="gauge-rate">{formatHitRate(summary.rate)}</span>
      <span class="metric-sub">
        {summary.resolved > 0
          ? `of ${summary.resolved} call${summary.resolved === 1 ? "" : "s"} that have played out`
          : "no calls have played out yet"}
      </span>
      <a class="bar-detail" href="/briefing">full record</a>
    </div>
  );
}

function RefusalsPanel({ refusals }: { refusals: Async<RefusalCounts> }) {
  if (refusals.kind === "loading") return <Loading label="Loading refusals…" />;
  if (refusals.kind === "error") {
    return <ErrorState message={refusals.message} detail={refusals.detail} />;
  }
  const total = refusalTotal(refusals.data);
  const top = topReason(refusals.data);
  const unproven = unprovenCount(refusals.data);
  if (total === 0 && unproven === null) {
    return <p class="empty">Nothing considered and turned down yet.</p>;
  }
  if (total === 0) {
    return (
      <div class="refusals-block">
        <span class="gauge-rate">{unproven}</span>
        <span class="metric-sub">calls still out, unjudged — the scorecard only counts what has come home</span>
      </div>
    );
  }
  return (
    <div class="refusals-block">
      <span class="gauge-rate">{total}</span>
      <span class="metric-sub">
        {top ? `mostly: ${explainRefusal(top.reason)}` : "stayed quiet"}
        {unproven !== null && unproven > 0 ? ` · ${unproven} still out there` : ""}
      </span>
    </div>
  );
}

function SectorLeaders({ sectors }: { sectors: Async<SectorEntry[]> }) {
  if (sectors.kind === "loading") return <Loading label="Loading sectors…" />;
  if (sectors.kind === "error") {
    return <ErrorState message={sectors.message} detail={sectors.detail} />;
  }
  if (sectors.data.length === 0) {
    return (
      <p class="empty">
        No sector scores yet — scoring needs sector ETF prices, which arrive once
        a provider key is configured.
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

export function TodayView() {
  const { calls, scorecard, refusals, regime, sectors } = useTodayData();

  return (
    <div class="today">
      <MarketBanner />

      <RegimeStrip regime={regime} />

      <div class="console-grid">
        <section class="panel console-main">
          <h2 class="panel-title">
            Today&apos;s calls
            <a class="panel-clear" href="/briefing">track record</a>
          </h2>
          <p class="panel-sub muted">
            Only what cleared the{" "}
            <Hint term="conviction_gate">conviction gate</Hint>. Direction is
            analysis, not advice.
          </p>
          {calls.kind === "loading" ? <Loading label="Loading calls…" /> : null}
          {calls.kind === "error" ? (
            <ErrorState message={calls.message} detail={calls.detail} />
          ) : null}
          {calls.kind === "ok" && calls.data.length === 0 ? (
            <div class="empty">
              <p>
                <strong>{briefingHeading([])}</strong> — nothing was confident and
                calibrated enough to surface. That is the system working, not an
                empty feed.
              </p>
            </div>
          ) : null}
          {calls.kind === "ok" && calls.data.length > 0 ? (
            <>
              <p class="gap-meta" style={{ padding: "12px 18px 0" }}>
                {briefingHeading(calls.data)}
              </p>
              <ul class="claims-list">
                {calls.data.map((f) => (
                  <FindingCard key={f.id} finding={f} />
                ))}
              </ul>
            </>
          ) : null}
        </section>

        <aside class="console-side">
          <section class="panel">
            <h2 class="panel-title">
              <Hint term="hit_rate">Track record</Hint>
            </h2>
            <TrackRecordPanel scorecard={scorecard} />
          </section>
          <section class="panel">
            <h2 class="panel-title">
              <Hint term="refusal">Refusals</Hint>
            </h2>
            <RefusalsPanel refusals={refusals} />
          </section>
          <section class="panel">
            <h2 class="panel-title">
              Sector leaders
              <a class="panel-clear" href="/sectors">scan</a>
            </h2>
            <SectorLeaders sectors={sectors} />
          </section>
        </aside>
      </div>
    </div>
  );
}
