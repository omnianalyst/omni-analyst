import { useEffect, useState } from "preact/hooks";
import { request, authHeaderIfPresent, describeError } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import { Loading } from "./Loading";
import { ErrorState } from "./ErrorState";

interface Position {
  symbol: string;
  market_type: string;
  quantity: string;
  average_entry: string;
  is_short: boolean;
}
interface Portfolio {
  nav: string;
  cash: string;
  gross_exposure: string;
  net_exposure: string;
  positions: Position[];
  cash_positions: { asset: string; free: string; locked: string }[];
}
interface Cycle {
  as_of: string;
  halted: boolean;
  halt_reason: string | null;
  funding_collected: string;
  fees_paid: string;
  pairs_opened: number;
  pairs_held: number;
}
interface RiskData {
  verdict: string;
  net_ratio?: number;
  pc1_label?: string;
}

type State =
  | { kind: "loading" }
  | { kind: "auth" }
  | { kind: "ok"; portfolio: Portfolio; cycles: Cycle[]; risk: RiskData | null }
  | { kind: "error"; message: string };

const VERDICT_COLORS: Record<string, string> = {
  delta_neutral: "var(--green, #4ade80)",
  slight_drift: "var(--yellow, #fbbf24)",
  factor_exposed: "var(--red, #f87171)",
  flat: "var(--muted)",
};
const VERDICT_LABELS: Record<string, string> = {
  delta_neutral: "Delta-neutral",
  slight_drift: "Slight drift",
  factor_exposed: "Factor exposed",
  flat: "Flat",
  insufficient_data: "Warming up",
  no_portfolio: "No portfolio",
};

export function PortfolioView() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    const headers = authHeaderIfPresent();
    if (!headers.authorization) {
      setState({ kind: "auth" });
      return;
    }
    (async () => {
      try {
        const [portfolio, cyclesResp, risk] = await Promise.all([
          request<Portfolio>("/trading/portfolio", headers),
          request<{ cycles: Cycle[] }>("/trading/cycles", headers).catch(() => ({ cycles: [] })),
          request<RiskData>("/scanner/risk", headers).catch(() => null),
        ]);
        if (!cancelled) setState({ kind: "ok", portfolio, cycles: cyclesResp.cycles, risk });
      } catch (err) {
        if (cancelled) return;
        if (err instanceof AuthRequiredError) { setState({ kind: "auth" }); return; }
        const { message } = describeError(err);
        setState({ kind: "error", message });
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (state.kind === "loading") return <Loading label="Loading portfolio..." />;
  if (state.kind === "auth") {
    return (
      <div style={{ padding: "40px" }}>
        <p class="muted">Sign in to view your portfolio.</p>
      </div>
    );
  }
  if (state.kind === "error") return <ErrorState message={state.message} />;

  const { portfolio, cycles, risk } = state;
  const spotPositions = portfolio.positions.filter((p) => p.market_type === "spot" && parseFloat(p.quantity) > 0);
  const pairs = spotPositions.map((p) => p.symbol.split("/")[0]);
  const lastCycle = cycles[0];
  const totalFunding = cycles.reduce((s, c) => s + (parseFloat(c.funding_collected) || 0), 0);
  const totalFees = cycles.reduce((s, c) => s + (parseFloat(c.fees_paid) || 0), 0);
  const verdict = risk?.verdict ?? "unknown";
  const vColor = VERDICT_COLORS[verdict] ?? "var(--muted)";
  const vLabel = VERDICT_LABELS[verdict] ?? verdict;

  return (
    <div class="portfolio-view">
      <header class="page-head">
        <h1>Portfolio</h1>
        <p class="muted">
          Your carry book. Live, delta-neutral, collecting funding.
        </p>
      </header>

      <div class="portfolio-stats">
        <div class="stat-block">
          <div class="stat-label">NAV</div>
          <div class="stat-value">${parseFloat(portfolio.nav).toFixed(2)}</div>
        </div>
        <div class="stat-block">
          <div class="stat-label">Cash free</div>
          <div class="stat-value">${parseFloat(portfolio.cash).toFixed(2)}</div>
        </div>
        <div class="stat-block">
          <div class="stat-label">Gross exposure</div>
          <div class="stat-value">${parseFloat(portfolio.gross_exposure).toFixed(2)}</div>
        </div>
        <div class="stat-block">
          <div class="stat-label">Net exposure</div>
          <div class="stat-value" style={{ color: "var(--green, #4ade80)" }}>
            ${parseFloat(portfolio.net_exposure).toFixed(2)}
          </div>
        </div>
      </div>

      <section class="panel">
        <div class="portfolio-status-bar">
          <h2 class="panel-title">Carry Book</h2>
          <span class="status-badge" style={{ background: vColor }}>
            <span class="status-dot" style={{ background: "var(--bg)" }} />
            {vLabel}
          </span>
        </div>

        <table class="data-table">
          <thead>
            <tr>
              <th>Pair</th>
              <th>Spot qty</th>
              <th>Perp qty</th>
              <th>Entry</th>
            </tr>
          </thead>
          <tbody>
            {spotPositions.map((spot) => {
              const perp = portfolio.positions.find(
                (p) => p.symbol.includes(spot.symbol.split("/")[0]) && p.market_type === "perpetual",
              );
              return (
                <tr key={spot.symbol}>
                  <td class="mono" style={{ fontWeight: 600 }}>{spot.symbol.split("/")[0]}</td>
                  <td class="mono">{parseFloat(spot.quantity).toFixed(4)}</td>
                  <td class="mono">{perp ? parseFloat(perp.quantity).toFixed(4) : "--"}</td>
                  <td class="mono">${parseFloat(spot.average_entry).toFixed(1)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>

        {risk?.pc1_label && (
          <p class="muted" style={{ fontSize: "12px", margin: "12px 16px 16px" }}>{risk.pc1_label}</p>
        )}
      </section>

      <section class="panel">
        <h2 class="panel-title">Cycle History</h2>
        {cycles.length === 0 ? (
          <p class="muted">No cycles recorded yet.</p>
        ) : (
          <table class="data-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Result</th>
                <th class="num">Pairs held</th>
                <th class="num">Funding</th>
                <th class="num">Fees</th>
              </tr>
            </thead>
            <tbody>
              {cycles.slice(0, 10).map((c, i) => (
                <tr key={i}>
                  <td class="mono">{new Date(c.as_of).toLocaleString()}</td>
                  <td style={{
                    color: c.halted ? "var(--red, #f87171)" : "var(--green, #4ade80)",
                    fontWeight: 600,
                  }}>
                    {c.halted ? "Halted" : c.pairs_held > 0 ? `${c.pairs_held} held` : "Flat"}
                  </td>
                  <td class="num mono">{c.pairs_held}</td>
                  <td class="num mono" style={{ color: "var(--green, #4ade80)" }}>
                    {parseFloat(c.funding_collected).toFixed(4)}
                  </td>
                  <td class="num muted">{parseFloat(c.fees_paid).toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {totalFunding > 0 && (
          <div class="portfolio-totals">
            <span>Total funding collected: <strong style={{ color: "var(--green, #4ade80)" }}>${totalFunding.toFixed(4)}</strong></span>
            <span class="muted">Total fees paid: ${totalFees.toFixed(4)}</span>
          </div>
        )}
      </section>

      {portfolio.cash_positions.length > 0 && (
        <section class="panel">
          <h2 class="panel-title">Cash Balances</h2>
          <table class="data-table">
            <thead>
              <tr>
                <th>Venue</th>
                <th>Asset</th>
                <th class="num">Free</th>
                <th class="num">Locked</th>
              </tr>
            </thead>
            <tbody>
              {portfolio.cash_positions.map((c, i) => (
                <tr key={i}>
                  <td class="mono">hyperliquid</td>
                  <td class="mono">{c.asset}</td>
                  <td class="num mono">${parseFloat(c.free).toFixed(2)}</td>
                  <td class="num mono">${parseFloat(c.locked).toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
