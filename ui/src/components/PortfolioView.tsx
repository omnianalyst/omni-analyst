import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import {
  formatMoney,
  getCarryCycles,
  getNavHistory,
  groupPositions,
  navChange,
  portfolioHealth,
  recordedCarry,
  type CarryCycle,
  type NavPoint,
  type PositionAssetClass,
} from "../lib/portfolio";
import {
  formatTimestamp,
  getPortfolio,
  getReconciliation,
  type Portfolio,
  type ReconciliationReport,
} from "../lib/trading";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

type Resource<T> =
  | { kind: "ok"; data: T }
  | { kind: "error"; message: string };

type State =
  | { kind: "loading" }
  | { kind: "auth" }
  | { kind: "error"; message: string; detail?: string }
  | {
      kind: "ok";
      portfolio: Portfolio;
      cycles: Resource<CarryCycle[]>;
      history: Resource<NavPoint[]>;
      reconciliation: Resource<ReconciliationReport>;
    };

type PositionFilter = "all" | PositionAssetClass;
type ChartRange = "1m" | "3m" | "1y" | "all";

function NavChart({ points }: { points: NavPoint[] }) {
  const measured = points
    .map((point) => ({ time: new Date(point.taken_at).getTime(), value: Number(point.nav) }))
    .filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value))
    .sort((a, b) => a.time - b.time);
  if (measured.length < 2) return <div class="clean-empty"><strong>Chart is building</strong><span>Two recorded valuations are needed.</span></div>;
  const times = measured.map((point) => point.time);
  const values = measured.map((point) => point.value);
  const minTime = Math.min(...times);
  const maxTime = Math.max(...times);
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const x = (time: number) => 12 + ((time - minTime) / Math.max(maxTime - minTime, 1)) * 576;
  const y = (value: number) => 148 - ((value - minValue) / Math.max(maxValue - minValue, 1)) * 124;
  const gaps = measured.slice(1).map((point, index) => point.time - measured[index].time).sort((a, b) => a - b);
  const typicalGap = gaps.length > 1 ? gaps[Math.floor(gaps.length / 2)] : 86_400_000;
  const paths: string[] = [];
  let path = "";
  measured.forEach((point, index) => {
    const beginsSegment = index === 0 || point.time - measured[index - 1].time > typicalGap * 2.5;
    if (beginsSegment && path) paths.push(path);
    path = `${beginsSegment ? "M" : `${path} L`} ${x(point.time).toFixed(1)} ${y(point.value).toFixed(1)}`;
  });
  if (path) paths.push(path);
  return (
    <div class="nav-chart-wrap">
      <svg class="nav-chart" viewBox="0 0 600 170" role="img" aria-label="Recorded portfolio value over time">
        <line x1="12" y1="148" x2="588" y2="148" class="nav-chart-axis" />
        {paths.map((segment) => <path d={segment} class="nav-chart-line" fill="none" />)}
      </svg>
      <div class="nav-chart-scale"><span>{formatMoney(String(minValue))}</span><span>{formatMoney(String(maxValue))}</span></div>
    </div>
  );
}

function resource<T>(result: PromiseSettledResult<T>): Resource<T> {
  if (result.status === "fulfilled") return { kind: "ok", data: result.value };
  return { kind: "error", message: describeError(result.reason).message };
}

function signedPercent(value: number | null): string {
  if (value === null) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function signedMoney(value: number | null): string {
  if (value === null) return "—";
  const absolute = formatMoney(String(Math.abs(value)));
  return `${value > 0 ? "+" : value < 0 ? "−" : ""}${absolute}`;
}

export function PortfolioView() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [positionFilter, setPositionFilter] = useState<PositionFilter>("all");
  const [chartRange, setChartRange] = useState<ChartRange>("1y");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const [portfolio, cycles, history, reconciliation] = await Promise.allSettled([
        getPortfolio(),
        getCarryCycles(),
        getNavHistory(),
        getReconciliation(),
      ]);
      if (cancelled) return;
      if (portfolio.status === "rejected") {
        if (portfolio.reason instanceof AuthRequiredError) {
          setState({ kind: "auth" });
          return;
        }
        const error = describeError(portfolio.reason);
        setState({ kind: "error", message: error.message, detail: error.detail });
        return;
      }
      setState({
        kind: "ok",
        portfolio: portfolio.value,
        cycles:
          cycles.status === "fulfilled"
            ? { kind: "ok", data: cycles.value.cycles }
            : resource(cycles),
        history:
          history.status === "fulfilled"
            ? { kind: "ok", data: history.value.points }
            : resource(history),
        reconciliation: resource(reconciliation),
      });
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "loading") return <Loading label="Loading your portfolio…" />;
  if (state.kind === "auth") {
    return (
      <section class="quiet-state">
        <p class="eyebrow">Private portfolio</p>
        <h1>Sign in to see your portfolio</h1>
        <p>Your positions and performance are visible only to your account.</p>
        <a class="btn-primary" href="/login">Sign in</a>
      </section>
    );
  }
  if (state.kind === "error") {
    return <ErrorState message={state.message} detail={state.detail} />;
  }

  const { portfolio } = state;
  const cycles = state.cycles.kind === "ok" ? state.cycles.data : [];
  const history = state.history.kind === "ok" ? state.history.data : [];
  const reconciliation =
    state.reconciliation.kind === "ok" ? state.reconciliation.data : null;
  const latestCycle = cycles[0] ?? null;
  const health = portfolioHealth(portfolio.positions, latestCycle, reconciliation);
  const groups = groupPositions(portfolio.positions);
  const filteredGroups = positionFilter === "all" ? groups : groups.filter((group) => group.assetClass === positionFilter);
  const carry = state.cycles.kind === "ok" ? recordedCarry(cycles) : null;
  const change = state.history.kind === "ok" ? navChange(history) : null;
  const rangeDays = { "1m": 31, "3m": 93, "1y": 366, all: Infinity }[chartRange];
  const newestTime = history.length ? new Date(history[history.length - 1].taken_at).getTime() : 0;
  const chartHistory = history.filter((point) => newestTime - new Date(point.taken_at).getTime() <= rangeDays * 86_400_000);

  return (
    <div class="portfolio-view product-page">
      <header class={`compact-status-heading health-${health.tone}`}>
        <div class="portfolio-hero-copy">
          <div class="health-title-row">
            <span class="health-orb" aria-hidden="true" />
            <div>
              <h1>Portfolio</h1>
              <p>{health.headline} · {health.detail}</p>
            </div>
          </div>
        </div>
        <div class="portfolio-asof">
          <span>Last valued</span>
          <strong>{formatTimestamp(portfolio.as_of)}</strong>
        </div>
      </header>

      <section class="primary-metrics" aria-label="Portfolio summary">
        <article class="primary-metric primary-metric-featured">
          <span class="metric-kicker">Portfolio value</span>
          <strong>{formatMoney(portfolio.nav)}</strong>
          <span class="metric-context">{formatMoney(portfolio.cash)} available cash</span>
        </article>
        <article class="primary-metric">
          <span class="metric-kicker">NAV change</span>
          <strong class={change !== null && change < 0 ? "value-negative" : "value-positive"}>
            {signedPercent(change)}
          </strong>
          <span class="metric-context">
            {history.length >= 2 ? `across ${history.length} recorded valuations` : "waiting for another valuation"}
          </span>
        </article>
        <article class="primary-metric">
          <span class="metric-kicker">Recorded net carry</span>
          <strong class={carry !== null && carry < 0 ? "value-negative" : "value-positive"}>
            {signedMoney(carry)}
          </strong>
          <span class="metric-context">
            {cycles.length > 0 ? "funding less fees and modelled turnover" : "no completed cycles yet"}
          </span>
        </article>
      </section>

      <section class="surface-card portfolio-chart-card">
        <div class="section-heading-row section-heading-compact">
          <div><p class="eyebrow">Recorded value</p><h2>Portfolio over time</h2><p>Actual NAV snapshots only; missing intervals are left disconnected.</p></div>
          <div class="view-switch chart-range-switch">
            {(["1m", "3m", "1y", "all"] as ChartRange[]).map((range) => (
              <button type="button" class={chartRange === range ? "active" : ""} onClick={() => setChartRange(range)}>{range === "all" ? "All" : range.toUpperCase()}</button>
            ))}
          </div>
        </div>
        <NavChart points={chartHistory} />
      </section>

      <div class="portfolio-grid">
        <section class="surface-card holdings-card">
          <div class="section-heading">
            <div>
              <p class="eyebrow">Now</p>
              <h2>What the portfolio holds</h2>
            </div>
            <span class="count-badge">{filteredGroups.length} of {groups.length}</span>
          </div>
          <div class="position-filters" aria-label="Position type">
            {(["all", "stocks", "defensive", "crypto"] as PositionFilter[]).map((filter) => (
              <button type="button" class={positionFilter === filter ? "active" : ""} onClick={() => setPositionFilter(filter)}>
                {filter === "all" ? "All" : filter === "stocks" ? "Stocks & ETFs" : filter === "defensive" ? "Gold, bonds & defensive" : "Crypto"}
              </button>
            ))}
          </div>
          {filteredGroups.length === 0 ? (
            <div class="clean-empty">
              <strong>{groups.length ? "No positions in this group" : "Holding cash"}</strong>
              <span>{groups.length ? "Choose another filter to see current holdings." : "No market positions are open."}</span>
            </div>
          ) : (
            <div class="position-stack">
              {filteredGroups.map((group) => (
                <article class="position-card" key={`${group.venue}:${group.asset}`}>
                  <div class="position-identity">
                    <span class="asset-mark">{group.asset.slice(0, 2)}</span>
                    <div>
                      <strong>{group.asset}</strong>
                      <span>{group.venue}</span>
                    </div>
                  </div>
                  <div class="position-purpose">
                    <strong>{group.hasSpot && group.hasPerpetual ? "Paired carry" : "Unpaired exposure"}</strong>
                    <span>
                      {group.hasSpot && group.hasPerpetual
                        ? "Spot and perpetual legs are both present"
                        : "Only one market leg is present"}
                    </span>
                  </div>
                  <div class="position-leg-count">{group.notional === null ? `${group.legs.length} legs` : formatMoney(String(group.notional))}</div>
                </article>
              ))}
            </div>
          )}
        </section>

        <aside class="surface-card activity-card">
          <div class="section-heading">
            <div>
              <p class="eyebrow">Latest activity</p>
              <h2>{latestCycle ? (latestCycle.halted ? "Cycle halted" : "Cycle completed") : "No cycle recorded"}</h2>
            </div>
          </div>
          {latestCycle ? (
            <>
              <p class="activity-summary">
                {latestCycle.halted
                  ? latestCycle.halt_reason || "The cycle stopped before changing the book."
                  : latestCycle.abstention
                    ? latestCycle.abstention
                    : `${latestCycle.pairs_held} pair${latestCycle.pairs_held === 1 ? "" : "s"} held after the run.`}
              </p>
              <dl class="activity-facts">
                <div><dt>Venue</dt><dd>{latestCycle.venue}</dd></div>
                <div><dt>Funding</dt><dd>{formatMoney(latestCycle.funding_collected)}</dd></div>
                <div><dt>Fees</dt><dd>{formatMoney(latestCycle.fees_paid)}</dd></div>
                <div><dt>Run</dt><dd>{formatTimestamp(latestCycle.as_of)}</dd></div>
              </dl>
            </>
          ) : (
            <p class="activity-summary">Activity will appear after the first recorded carry cycle.</p>
          )}
          {state.cycles.kind === "error" ? (
            <p class="inline-warning">Cycle history unavailable: {state.cycles.message}</p>
          ) : null}
        </aside>
      </div>
    </div>
  );
}
