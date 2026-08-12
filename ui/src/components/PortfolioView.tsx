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
} from "../lib/portfolio";
import {
  describeReconciliation,
  formatQuantity,
  formatTimestamp,
  getPortfolio,
  getReconciliation,
  sideLabel,
  positionSide,
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
  const [detailsOpen, setDetailsOpen] = useState(false);

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
  const carry = state.cycles.kind === "ok" ? recordedCarry(cycles) : null;
  const change = state.history.kind === "ok" ? navChange(history) : null;

  return (
    <div class="portfolio-view product-page">
      <header class={`portfolio-hero health-${health.tone}`}>
        <div class="portfolio-hero-copy">
          <p class="eyebrow">Your portfolio</p>
          <div class="health-title-row">
            <span class="health-orb" aria-hidden="true" />
            <h1>{health.headline}</h1>
          </div>
          <p>{health.detail}</p>
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

      <div class="portfolio-grid">
        <section class="surface-card holdings-card">
          <div class="section-heading">
            <div>
              <p class="eyebrow">Now</p>
              <h2>What the portfolio holds</h2>
            </div>
            <span class="count-badge">{groups.length} {groups.length === 1 ? "position" : "positions"}</span>
          </div>
          {groups.length === 0 ? (
            <div class="clean-empty">
              <strong>Holding cash</strong>
              <span>No market positions are open.</span>
            </div>
          ) : (
            <div class="position-stack">
              {groups.map((group) => (
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
                  <div class="position-leg-count">{group.legs.length} legs</div>
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

      <button
        type="button"
        class="disclosure-button"
        aria-expanded={detailsOpen}
        onClick={() => setDetailsOpen((open) => !open)}
      >
        <span>{detailsOpen ? "Hide portfolio details" : "View portfolio details"}</span>
        <span aria-hidden="true">{detailsOpen ? "−" : "+"}</span>
      </button>

      {detailsOpen ? (
        <div class="detail-drawer">
          <section class="detail-block">
            <div class="section-heading">
              <div><p class="eyebrow">Verification</p><h2>Venue checks</h2></div>
            </div>
            {state.reconciliation.kind === "error" ? (
              <p class="inline-warning">Reconciliation unavailable: {state.reconciliation.message}</p>
            ) : reconciliation && reconciliation.venues.length > 0 ? (
              <div class="verification-list">
                {reconciliation.venues.map((venue) => {
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
            ) : (
              <p class="clean-empty">No venue checks have been recorded.</p>
            )}
          </section>

          <section class="detail-block">
            <div class="section-heading">
              <div><p class="eyebrow">Positions</p><h2>Recorded legs</h2></div>
            </div>
            {portfolio.positions.length === 0 ? (
              <p class="clean-empty">No position legs are recorded.</p>
            ) : (
              <div class="responsive-table">
                <table class="data-table">
                  <thead><tr><th>Asset</th><th>Venue</th><th>Market</th><th>Side</th><th>Quantity</th><th>Entry</th></tr></thead>
                  <tbody>
                    {portfolio.positions.map((position) => {
                      const side = positionSide(position);
                      return (
                        <tr key={`${position.venue}:${position.symbol}:${position.market_type}`}>
                          <td><strong>{position.symbol}</strong></td>
                          <td>{position.venue}</td>
                          <td>{position.market_type}</td>
                          <td>{sideLabel(side)}</td>
                          <td class="mono">{formatQuantity(position.quantity)}</td>
                          <td class="mono">{formatMoney(position.average_entry)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section class="detail-block">
            <div class="section-heading">
              <div><p class="eyebrow">History</p><h2>Recorded valuations</h2></div>
              <span class="count-badge">{history.length}</span>
            </div>
            {state.history.kind === "error" ? (
              <p class="inline-warning">Valuation history unavailable: {state.history.message}</p>
            ) : history.length === 0 ? (
              <p class="clean-empty">No historical valuations have been recorded yet.</p>
            ) : (
              <div class="valuation-strip">
                {history.slice(-12).map((point) => (
                  <div class="valuation-point" key={point.taken_at}>
                    <strong>{formatMoney(point.nav)}</strong>
                    <span>{point.taken_at.slice(0, 10)}</span>
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
