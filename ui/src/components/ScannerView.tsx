import { useEffect, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError, request } from "../lib/api";
import { CompaniesPanel } from "./CompaniesPanel";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

type AssetClass = "stocks" | "crypto" | "defensive";
type RiskTier = "low" | "medium" | "high" | "unrated";
type MarketBehavior = "risk_on" | "diversifier" | "counterweight" | "unrated";

interface AssetMetric {
  symbol: string;
  name: string;
  area: string;
  asset_class: AssetClass;
  risk_tier: RiskTier;
  market_behavior: MarketBehavior;
  correlation_to_spy?: number | null;
  returns?: { "365d"?: number | null };
  volatility?: number | null;
  max_drawdown?: number | null;
  cagr_5y?: number | null;
  cagr_10y?: number | null;
  median_annual_return?: number | null;
  history_years: number;
  complete_years: number;
  market_cap_rank?: number | null;
  scores: {
    balanced: number | null;
    durable_growth: number | null;
    consistency: number | null;
    stability: number | null;
    diversification: number | null;
  };
}

interface SectorLeader {
  symbol: string;
  name: string;
  return_window: number;
  as_of: string;
}

interface OverallLeader extends SectorLeader {
  sector: string;
  sector_symbol: string;
}

interface SectorLeaders {
  name: string;
  symbol: string;
  coverage: number;
  leaders: SectorLeader[];
}

interface ScannerData {
  category_rankings: Record<AssetClass, AssetMetric[]>;
  sectors: SectorLeaders[];
  overall_leaders: OverallLeader[];
  ranking_method: { balanced: string; history: string; scope: string };
  sector_coverage: { available: number; total: number; window_sessions: number };
  coverage: {
    policy_version: string;
    complete: boolean;
    crypto: {
      source: string;
      live: boolean;
      market_cap_limit: number;
      ranked: number;
      excluded: Array<{ rank: number; symbol: string; name: string; reason: string }>;
      unmapped: Array<{
        rank: number;
        symbol: string;
        name: string;
        coin_id: string;
        reason: string;
        measured: boolean;
      }>;
      insufficient_history: Array<{ symbol: string; observations: number; required: number }>;
    };
    broad_assets: { configured: number; ranked: number; unavailable: string[] };
    companies: { sectors_measured: number; sectors_required: number; complete: boolean };
    industries: { complete: boolean; reason: string };
  };
  as_of: string;
}

type State =
  | { kind: "loading" }
  | { kind: "ok"; data: ScannerData }
  | { kind: "error"; message: string; detail?: string };

const CATEGORY_DETAILS: Array<{
  key: AssetClass;
  title: string;
  description: string;
}> = [
  {
    key: "stocks",
    title: "Stocks & ETFs",
    description: "Broad markets, styles, international funds, and all 11 US sector ETFs.",
  },
  {
    key: "defensive",
    title: "Defensive & real assets",
    description: "Treasuries, bonds, gold, silver, and broad commodities.",
  },
  {
    key: "crypto",
    title: "Crypto",
    description: "Major digital assets ranked on the same measured framework.",
  },
];

const BEHAVIOR_LABELS: Record<MarketBehavior, string> = {
  risk_on: "Moves with stocks",
  diversifier: "Diversifier",
  counterweight: "Counterweight",
  unrated: "Not measured",
};

function percent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;
}

function tone(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return value > 0 ? "value-positive" : value < 0 ? "value-negative" : "";
}

function RankedCategory({
  title,
  description,
  assets,
  behaviorLens,
}: {
  title: string;
  description: string;
  assets: AssetMetric[];
  behaviorLens: boolean;
}) {
  return (
    <section class="rank-category">
      <div class="asset-group-heading">
        <div><h2>{title}</h2><p>{description}</p></div>
        <span>{assets.length}</span>
      </div>
      <div class="rank-table-wrap">
        <table class="rank-table">
          <thead>
            <tr>
              <th>Rank</th>
              <th>Asset</th>
              <th>Score</th>
              <th>1 year</th>
              <th>5y / year</th>
              <th>Median year</th>
              <th>Volatility</th>
              <th>{behaviorLens ? "Market role" : "Risk"}</th>
            </tr>
          </thead>
          <tbody>
            {assets.map((asset, index) => (
              <tr key={asset.symbol}>
                <td><span class="rank-number">{index + 1}</span></td>
                <td>
                  <span class="rank-asset">
                    <strong>{asset.symbol}</strong>
                    <small>
                      {asset.name} · {asset.area}
                      {asset.market_cap_rank ? ` · market cap #${asset.market_cap_rank}` : ""}
                    </small>
                  </span>
                </td>
                <td><strong class="canonical-score">{asset.scores.balanced?.toFixed(0) ?? "—"}</strong></td>
                <td class={tone(asset.returns?.["365d"])}>{percent(asset.returns?.["365d"])}</td>
                <td class={tone(asset.cagr_5y)}>{percent(asset.cagr_5y)}</td>
                <td class={tone(asset.median_annual_return)}>{percent(asset.median_annual_return)}</td>
                <td>{percent(asset.volatility)}</td>
                <td>
                  {behaviorLens ? (
                    <span class={`behavior-badge behavior-badge-${asset.market_behavior}`}>
                      {BEHAVIOR_LABELS[asset.market_behavior]}
                    </span>
                  ) : (
                    <span class={`risk-badge risk-badge-${asset.risk_tier}`}>
                      {asset.risk_tier === "unrated" ? "Unrated" : asset.risk_tier}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function SectorLeadership({ data }: { data: ScannerData }) {
  const companyCount = data.sectors.reduce((total, sector) => total + sector.coverage, 0);
  return (
    <section class="sector-leadership-block">
      <div class="section-heading-row section-heading-compact">
        <div>
          <p class="eyebrow">Individual companies</p>
          <h2>Ranked within each measured sector</h2>
          <p>
            These use the {data.sector_coverage.window_sessions}-session company history currently
            available. Sector ETFs above carry the longer-horizon comparison.
          </p>
        </div>
        <span class="coverage-note">
          {companyCount} companies · {data.sector_coverage.available} of {data.sector_coverage.total} sectors
        </span>
      </div>

      {data.sectors.length === 0 ? (
        <div class="quiet-state compact">
          <h3>Company rankings are still building</h3>
          <p>No company has enough visible history for a measured ranking yet.</p>
        </div>
      ) : (
        <>
          {data.overall_leaders.length > 0 ? <article class="overall-leaders-card">
            <header>
              <div><p class="eyebrow">Across measured companies</p><h3>Top overall</h3></div>
              <span>Top {data.overall_leaders.length} by {data.sector_coverage.window_sessions}-session return</span>
            </header>
            <ol class="overall-leader-grid">
              {data.overall_leaders.map((leader, index) => (
                <li key={leader.symbol}>
                  <span class="leader-rank">{index + 1}</span>
                  <span class="leader-company"><strong>{leader.symbol}</strong><small>{leader.sector}</small></span>
                  <strong class={tone(leader.return_window)}>{percent(leader.return_window)}</strong>
                </li>
              ))}
            </ol>
          </article> : (
            <div class="coverage-gate-note">
              <strong>Overall company ranking withheld</strong>
              <span>It will unlock when all 11 sectors have enough comparable company history.</span>
            </div>
          )}
          <div class="sector-leader-grid">
            {data.sectors.map((sector) => (
              <article class="sector-leader-card" key={sector.symbol}>
                <header>
                  <div><span>{sector.symbol}</span><h3>{sector.name}</h3></div>
                  <small>Top {sector.leaders.length} of {sector.coverage}</small>
                </header>
                <ol>
                  {sector.leaders.map((leader, index) => (
                    <li key={leader.symbol}>
                      <span class="leader-rank">{index + 1}</span>
                      <span class="leader-company"><strong>{leader.symbol}</strong><small>{leader.name}</small></span>
                      <strong class={tone(leader.return_window)}>{percent(leader.return_window)}</strong>
                    </li>
                  ))}
                </ol>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

export function ScannerView() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [behaviorLens, setBehaviorLens] = useState(false);

  useEffect(() => {
    let cancelled = false;
    request<ScannerData>("/scanner/market", authHeaderIfPresent())
      .then((data) => { if (!cancelled) setState({ kind: "ok", data }); })
      .catch((error) => {
        if (cancelled) return;
        const described = describeError(error);
        setState({ kind: "error", message: described.message, detail: described.detail });
      });
    return () => { cancelled = true; };
  }, []);

  if (state.kind === "loading") return <Loading label="Ranking the measured market…" />;
  if (state.kind === "error") return <ErrorState message={state.message} detail={state.detail} />;

  const assetCount = Object.values(state.data.category_rankings)
    .reduce((total, assets) => total + assets.length, 0);
  const companyCount = state.data.sectors.reduce((total, sector) => total + sector.coverage, 0);

  return (
    <div class="scanner-view product-page">
      <header class="discover-page-heading">
        <div><h1>Discover</h1><p>The measured list, ranked from strongest to weakest within each category.</p></div>
        <div class="discover-compact-meta">
          <span><strong>{assetCount}</strong> broad assets</span>
          <span><strong>{companyCount}</strong> companies</span>
          <time dateTime={state.data.as_of}>Updated {new Date(state.data.as_of).toLocaleString()}</time>
        </div>
      </header>

      <section class="ranking-definition">
        <div>
          <strong>One canonical score</strong>
          <p>{state.data.ranking_method.balanced} {state.data.ranking_method.scope}</p>
        </div>
        <button
          type="button"
          class={`behavior-toggle ${behaviorLens ? "active" : ""}`}
          aria-pressed={behaviorLens}
          onClick={() => setBehaviorLens((current) => !current)}
        >
          <span class="toggle-track" aria-hidden="true"><span /></span>
          Show market role
        </button>
      </section>

      <section class={`coverage-summary ${state.data.coverage.complete ? "coverage-complete" : "coverage-partial"}`}>
        <div class="coverage-summary-title">
          <span class="health-orb" aria-hidden="true" />
          <div>
            <strong>{state.data.coverage.complete ? "Universe coverage complete" : "Universe coverage is still closing"}</strong>
            <p>Policy {state.data.coverage.policy_version} · every omission is now classified.</p>
          </div>
        </div>
        <div class="coverage-summary-facts">
          <span><strong>{state.data.coverage.crypto.ranked}</strong> crypto ranked</span>
          <span><strong>{state.data.coverage.crypto.unmapped.length}</strong> need mapping</span>
          <span><strong>{state.data.coverage.companies.sectors_measured}/{state.data.coverage.companies.sectors_required}</strong> company sectors</span>
        </div>
        <details>
          <summary>View coverage audit</summary>
          <div class="coverage-audit-grid">
            <div>
              <strong>Explicitly excluded ({state.data.coverage.crypto.excluded.length})</strong>
              <p>{state.data.coverage.crypto.excluded.length
                ? state.data.coverage.crypto.excluded.map((item) => `${item.symbol}: ${item.reason}`).join(" · ")
                : "No live exclusions returned."}</p>
            </div>
            <div>
              <strong>Needs a verified mapping ({state.data.coverage.crypto.unmapped.length})</strong>
              {state.data.coverage.crypto.unmapped.length ? (
                <ul class="coverage-audit-list">
                  {state.data.coverage.crypto.unmapped.map((item) => (
                    <li key={item.coin_id}>
                      <span>#{item.rank} {item.symbol}</span> — {item.reason}
                    </li>
                  ))}
                </ul>
              ) : (
                <p>Every eligible census asset is mapped.</p>
              )}
            </div>
            <div>
              <strong>Insufficient price history ({state.data.coverage.crypto.insufficient_history.length})</strong>
              <p>{state.data.coverage.crypto.insufficient_history.length
                ? state.data.coverage.crypto.insufficient_history.map((item) => `${item.symbol}: ${item.observations}/${item.required} observations`).join(" · ")
                : "Every mapped asset meets the history floor."}</p>
            </div>
            <div>
              <strong>Industries</strong>
              <p>{state.data.coverage.industries.reason}</p>
            </div>
          </div>
        </details>
      </section>

      <div class="canonical-rankings">
        {CATEGORY_DETAILS.map((category) => (
          <RankedCategory
            key={category.key}
            title={category.title}
            description={category.description}
            assets={state.data.category_rankings[category.key] ?? []}
            behaviorLens={behaviorLens}
          />
        ))}
      </div>

      <details class="methodology-note canonical-methodology">
        <summary>Measurement details</summary>
        <p>{state.data.ranking_method.history}</p>
        <p>Volatility is annualized from daily returns. Market role uses correlation to SPY and is descriptive, not a guaranteed hedge.</p>
      </details>

      <SectorLeadership data={state.data} />

      <CompaniesPanel />
    </div>
  );
}
