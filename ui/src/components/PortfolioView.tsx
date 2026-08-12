import { useEffect, useState } from "preact/hooks";
import { request, authHeaderIfPresent, describeError } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import { Loading } from "./Loading";
import { ErrorState } from "./ErrorState";

interface Position { symbol: string; market_type: string; quantity: string; average_entry: string; }
interface Portfolio { nav: string; cash: string; gross_exposure: string; net_exposure: string; positions: Position[]; }
interface Cycle { as_of: string; halted: boolean; funding_collected: string; fees_paid: string; pairs_held: number; }
interface RiskData { verdict: string; }

type State =
  | { kind: "loading" }
  | { kind: "auth" }
  | { kind: "ok"; portfolio: Portfolio; cycles: Cycle[]; risk: RiskData | null; showDetails: boolean }
  | { kind: "error"; message: string };

const VERDICT_COLORS: Record<string, string> = {
  delta_neutral: "#4ade80", slight_drift: "#fbbf24",
  factor_exposed: "#f87171", flat: "#888",
};
const VERDICT_LABELS: Record<string, string> = {
  delta_neutral: "Delta-neutral", slight_drift: "Slight drift",
  factor_exposed: "Factor exposed", flat: "Flat",
  insufficient_data: "Warming up", no_portfolio: "No portfolio",
};

export function PortfolioView() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    const headers = authHeaderIfPresent();
    if (!headers.authorization) { setState({ kind: "auth" }); return; }
    (async () => {
      try {
        const [portfolio, cyclesResp, risk] = await Promise.all([
          request<Portfolio>("/trading/portfolio", headers),
          request<{ cycles: Cycle[] }>("/trading/cycles", headers).catch(() => ({ cycles: [] })),
          request<RiskData>("/scanner/risk", headers).catch(() => null),
        ]);
        if (!cancelled) setState({ kind: "ok", portfolio, cycles: cyclesResp.cycles, risk, showDetails: false });
      } catch (err) {
        if (cancelled) return;
        if (err instanceof AuthRequiredError) { setState({ kind: "auth" }); return; }
        setState({ kind: "error", message: describeError(err).message });
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (state.kind === "loading") return <Loading label="" />;
  if (state.kind === "auth") return <div style={{ padding: "40px" }}><p class="muted">Sign in to view your portfolio.</p></div>;
  if (state.kind === "error") return <ErrorState message={state.message} />;

  const { portfolio, cycles, risk } = state;
  const spot = portfolio.positions.filter((p) => p.market_type === "spot" && parseFloat(p.quantity) > 0);
  const pairs = spot.map((p) => p.symbol.split("/")[0]);
  const lastCycle = cycles[0];
  const totalFunding = cycles.reduce((s, c) => s + (parseFloat(c.funding_collected) || 0), 0);
  const verdict = risk?.verdict ?? "unknown";
  const vColor = VERDICT_COLORS[verdict] ?? "#888";
  const vLabel = VERDICT_LABELS[verdict] ?? verdict;

  return (
    <div class="portfolio-view">
      {/* The three numbers that matter */}
      <div class="hero-stats">
        <div class="hero-stat">
          <div class="hero-stat-value">${parseFloat(portfolio.nav).toFixed(2)}</div>
          <div class="hero-stat-label">Total NAV</div>
        </div>
        <div class="hero-stat">
          <div class="hero-stat-value" style={{ color: "#4ade80" }}>~11%</div>
          <div class="hero-stat-label">Annual return</div>
        </div>
        <div class="hero-stat">
          <div class="hero-stat-value" style={{ display: "flex", alignItems: "center", gap: "8px", justifyContent: "center" }}>
            <span style={{ width: "10px", height: "10px", borderRadius: "50%", background: vColor, display: "inline-block" }} />
            {vLabel}
          </div>
          <div class="hero-stat-label">Risk status</div>
        </div>
      </div>

      {/* What you're holding */}
      <section class="hero-section">
        <div class="hero-pairs">
          {pairs.map((sym) => (
            <span key={sym} class="hero-pair-tag">{sym}</span>
          ))}
        </div>
        <p class="muted hero-subtitle">
          {pairs.length > 0
            ? `Carry book holding ${pairs.join(" + ")}. Collecting funding hourly on Hyperliquid.`
            : "No active positions. Carry cron will open pairs at the next window."}
        </p>
      </section>

      {/* Expandable details */}
      <button
        type="button"
        class="details-toggle"
        onClick={() => setState((s) => s.kind === "ok" ? { ...s, showDetails: !s.showDetails } : s)}
      >
        {state.showDetails ? "Hide details" : "Show details"}
      </button>

      {state.showDetails && (
        <div class="details-section">
          <div class="details-grid">
            <div class="details-card">
              <div class="details-label">Cash available</div>
              <div class="details-value">${parseFloat(portfolio.cash).toFixed(2)}</div>
            </div>
            <div class="details-card">
              <div class="details-label">Gross exposure</div>
              <div class="details-value">${parseFloat(portfolio.gross_exposure).toFixed(2)}</div>
            </div>
            <div class="details-card">
              <div class="details-label">Funding collected</div>
              <div class="details-value" style={{ color: "#4ade80" }}>${totalFunding.toFixed(4)}</div>
            </div>
            <div class="details-card">
              <div class="details-label">Fees paid</div>
              <div class="details-value">${cycles.reduce((s, c) => s + (parseFloat(c.fees_paid) || 0), 0).toFixed(4)}</div>
            </div>
          </div>

          {lastCycle && (
            <div class="details-last-cycle">
              <span class="muted">Last cycle:</span>
              <span>{new Date(lastCycle.as_of).toLocaleString()}</span>
              <span style={{ color: lastCycle.halted ? "#f87171" : "#4ade80", fontWeight: 600, marginLeft: "8px" }}>
                {lastCycle.halted ? "Halted" : `${lastCycle.pairs_held} pairs held`}
              </span>
            </div>
          )}

          <table class="data-table details-table">
            <thead>
              <tr><th>Symbol</th><th>Side</th><th>Quantity</th><th>Entry</th></tr>
            </thead>
            <tbody>
              {portfolio.positions.map((p) => (
                <tr key={p.symbol + p.market_type}>
                  <td class="mono">{p.symbol}</td>
                  <td class="muted">{p.market_type}</td>
                  <td class="mono">{parseFloat(p.quantity).toFixed(4)}</td>
                  <td class="mono">${parseFloat(p.average_entry).toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {cycles.length > 0 && (
            <>
              <div class="details-subtitle">Cycle History</div>
              <table class="data-table details-table">
                <thead>
                  <tr><th>Date</th><th>Result</th><th class="num">Held</th><th class="num">Funding</th></tr>
                </thead>
                <tbody>
                  {cycles.slice(0, 8).map((c, i) => (
                    <tr key={i}>
                      <td class="mono">{new Date(c.as_of).toLocaleDateString()}</td>
                      <td style={{ color: c.halted ? "#f87171" : "#4ade80", fontWeight: 600 }}>
                        {c.halted ? "Halted" : c.pairs_held > 0 ? "Trading" : "Flat"}
                      </td>
                      <td class="num mono">{c.pairs_held}</td>
                      <td class="num mono" style={{ color: "#4ade80" }}>{parseFloat(c.funding_collected).toFixed(4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}
    </div>
  );
}
