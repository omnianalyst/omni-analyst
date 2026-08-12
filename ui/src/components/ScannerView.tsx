import { useEffect, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError, request } from "../lib/api";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

type AssetClass = "stocks" | "crypto" | "defensive";
type RiskTier = "low" | "medium" | "high" | "unrated";
type MarketBehavior = "risk_on" | "diversifier" | "counterweight" | "unrated";
type ViewMode = "type" | "risk";
type RankMode = "balanced" | "durable_growth" | "consistency" | "stability" | "diversification";

interface AssetMetric {
  symbol: string;
  name?: string;
  area: string;
  asset_class: AssetClass;
  risk_tier: RiskTier;
  correlation_to_spy?: number | null;
  market_behavior: MarketBehavior;
  returns?: {
    "7d"?: number | null;
    "30d"?: number | null;
    "90d"?: number | null;
    "365d"?: number | null;
  };
  volatility?: number | null;
  max_drawdown?: number | null;
  cagr_5y?: number | null;
  cagr_10y?: number | null;
  median_annual_return?: number | null;
  positive_year_rate?: number | null;
  history_years: number;
  complete_years: number;
  scores: Record<RankMode, number | null>;
  funding_apr?: number | null;
  context?: string;
}

interface Bucket {
  name: string;
  role: string;
  assets: Omit<AssetMetric, "context">[];
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
  buckets: Bucket[];
  sectors: SectorLeaders[];
  overall_leaders: OverallLeader[];
  asset_rankings: Record<RankMode, AssetMetric[]>;
  ranking_method: {
    balanced: string;
    history: string;
    scope: string;
  };
  sector_coverage: {
    available: number;
    total: number;
    window_sessions: number;
  };
  as_of: string;
}

type State =
  | { kind: "loading" }
  | { kind: "ok"; data: ScannerData }
  | { kind: "error"; message: string; detail?: string };

const TYPE_GROUPS: Array<{
  key: AssetClass;
  label: string;
  description: string;
}> = [
  {
    key: "stocks",
    label: "Stocks",
    description: "Broad equity markets and measured company leadership.",
  },
  {
    key: "crypto",
    label: "Crypto",
    description: "Major digital assets, including current funding when available.",
  },
  {
    key: "defensive",
    label: "Defensive assets",
    description: "Treasuries and metals that may behave differently from stocks.",
  },
];

const RISK_GROUPS: Array<{
  key: RiskTier;
  label: string;
  description: string;
}> = [
  { key: "low", label: "Low measured risk", description: "Below 10% annualized volatility." },
  { key: "medium", label: "Medium measured risk", description: "10–30% annualized volatility." },
  { key: "high", label: "High measured risk", description: "Above 30% annualized volatility." },
  { key: "unrated", label: "Not yet rated", description: "Not enough data for a volatility tier." },
];

const BEHAVIOR_LABELS: Record<MarketBehavior, string> = {
  risk_on: "Moves with stocks",
  diversifier: "Diversifier",
  counterweight: "Counterweight",
  unrated: "Not measured",
};

const RANK_LABELS: Record<RankMode, string> = {
  balanced: "Balanced",
  durable_growth: "Long-term",
  consistency: "Consistency",
  stability: "Stability",
  diversification: "Diversification",
};

function percent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;
}

function tone(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return value > 0 ? "value-positive" : value < 0 ? "value-negative" : "";
}

function AssetCard({
  asset,
  behaviorLens,
}: {
  asset: AssetMetric;
  behaviorLens: boolean;
}) {
  return (
    <article
      class={`discover-asset-card ${behaviorLens ? `behavior-${asset.market_behavior}` : ""}`}
    >
      <header>
        <div>
          <strong>{asset.symbol}</strong>
          <span>{asset.name}</span>
        </div>
        {behaviorLens ? (
          <span class={`behavior-badge behavior-badge-${asset.market_behavior}`}>
            {BEHAVIOR_LABELS[asset.market_behavior]}
          </span>
        ) : (
          <span class={`risk-badge risk-badge-${asset.risk_tier}`}>
            {asset.risk_tier === "unrated" ? "Unrated" : `${asset.risk_tier} risk`}
          </span>
        )}
      </header>

      <div class="asset-card-measures asset-card-measures-four">
        <div>
          <span>1 year</span>
          <strong class={tone(asset.returns?.["365d"])}>
            {percent(asset.returns?.["365d"])}
          </strong>
        </div>
        <div>
          <span>5y / year</span>
          <strong class={tone(asset.cagr_5y)}>
            {percent(asset.cagr_5y)}
          </strong>
        </div>
        <div>
          <span>Median year</span>
          <strong class={tone(asset.median_annual_return)}>{percent(asset.median_annual_return)}</strong>
        </div>
        <div>
          <span>Volatility</span>
          <strong>{percent(asset.volatility)}</strong>
        </div>
      </div>

      <footer>
        <span>{asset.area} · {asset.history_years.toFixed(1)}y history</span>
        {asset.funding_apr !== null && asset.funding_apr !== undefined ? (
          <span>Funding {percent(asset.funding_apr)} APR</span>
        ) : behaviorLens && asset.correlation_to_spy !== null &&
          asset.correlation_to_spy !== undefined ? (
          <span>Stock correlation {asset.correlation_to_spy.toFixed(2)}</span>
        ) : null}
      </footer>
    </article>
  );
}

function SectorLeadership({ data }: { data: ScannerData }) {
  return (
    <div class="sector-leadership-block">
      <div class="section-heading-row section-heading-compact">
        <div>
          <p class="eyebrow">Stock leadership</p>
          <h3>Top companies within measured sectors</h3>
          <p>
            Ranked by trailing {data.sector_coverage.window_sessions}-session return from
            the company histories available to you.
          </p>
        </div>
        <span class="coverage-note">
          {data.sector_coverage.available} of {data.sector_coverage.total} sectors measured
        </span>
      </div>

      {data.sectors.length === 0 ? (
        <div class="quiet-state compact">
          <h3>Sector leadership is still building</h3>
          <p>No company has enough visible history for a measured ranking yet.</p>
        </div>
      ) : (
        <>
          <article class="overall-leaders-card">
            <header>
              <div>
                <p class="eyebrow">Fast funnel</p>
                <h3>Best overall</h3>
              </div>
              <span>Top {data.overall_leaders.length} across {data.sectors.reduce((total, sector) => total + sector.coverage, 0)}</span>
            </header>
            <ol class="overall-leader-grid">
              {data.overall_leaders.map((leader, index) => (
                <li key={leader.symbol}>
                  <span class="leader-rank">{index + 1}</span>
                  <span class="leader-company">
                    <strong>{leader.symbol}</strong>
                    <small>{leader.sector}</small>
                  </span>
                  <strong class={tone(leader.return_window)}>
                    {percent(leader.return_window)}
                  </strong>
                </li>
              ))}
            </ol>
          </article>

          <div class="sector-subheading">
            <h3>Best within each sector</h3>
            <span>Up to 15 companies per measured sector</span>
          </div>
          <div class="sector-leader-grid">
            {data.sectors.map((sector) => (
              <article class="sector-leader-card" key={sector.symbol}>
                <header>
                  <div>
                    <span>{sector.symbol}</span>
                    <h3>{sector.name}</h3>
                  </div>
                  <small>Top {sector.leaders.length} of {sector.coverage}</small>
                </header>
                <ol>
                  {sector.leaders.map((leader, index) => (
                    <li key={leader.symbol}>
                      <span class="leader-rank">{index + 1}</span>
                      <span class="leader-company">
                        <strong>{leader.symbol}</strong>
                        <small>{leader.name}</small>
                      </span>
                      <strong class={tone(leader.return_window)}>
                        {percent(leader.return_window)}
                      </strong>
                    </li>
                  ))}
                </ol>
              </article>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export function ScannerView() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [viewMode, setViewMode] = useState<ViewMode>("type");
  const [behaviorLens, setBehaviorLens] = useState(false);
  const [rankMode, setRankMode] = useState<RankMode>("balanced");

  useEffect(() => {
    let cancelled = false;
    request<ScannerData>("/scanner/market", authHeaderIfPresent())
      .then((data) => {
        if (!cancelled) setState({ kind: "ok", data });
      })
      .catch((error) => {
        if (cancelled) return;
        const described = describeError(error);
        setState({ kind: "error", message: described.message, detail: described.detail });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "loading") return <Loading label="Reading the market…" />;
  if (state.kind === "error") {
    return <ErrorState message={state.message} detail={state.detail} />;
  }

  const assets: AssetMetric[] = state.data.buckets.flatMap((bucket) =>
    bucket.assets.map((asset) => ({ ...asset, context: bucket.name })),
  );
  const companyCount = state.data.sectors.reduce(
    (total, sector) => total + sector.coverage,
    0,
  );
  const behaviorCounts = assets.reduce(
    (counts, asset) => ({
      ...counts,
      [asset.market_behavior]: counts[asset.market_behavior] + 1,
    }),
    { risk_on: 0, diversifier: 0, counterweight: 0, unrated: 0 },
  );

  const groups = viewMode === "type" ? TYPE_GROUPS : RISK_GROUPS;
  const rankedAssets = state.data.asset_rankings[rankMode] ?? [];

  return (
    <div class="scanner-view product-page">
      <header class="discover-page-heading">
        <div>
          <h1>Discover</h1>
          <p>See what is leading, what moves together, and what may offset it.</p>
        </div>
        <div class="discover-compact-meta">
          <span><strong>{companyCount}</strong> companies</span>
          <span><strong>{assets.length}</strong> broad assets</span>
          <time dateTime={state.data.as_of}>
            Updated {new Date(state.data.as_of).toLocaleString()}
          </time>
        </div>
      </header>

      <section class="surface-card ranked-assets-card">
        <div class="section-heading-row section-heading-compact">
          <div>
            <p class="eyebrow">Measured shortlist</p>
            <h2>Highest-ranked broad assets</h2>
            <p>Compare durable returns, repeatability, risk, and how differently each asset moved from stocks.</p>
          </div>
          <div class="rank-switch" aria-label="Ranking lens">
            {(Object.keys(RANK_LABELS) as RankMode[]).map((mode) => (
              <button type="button" class={rankMode === mode ? "active" : ""} onClick={() => setRankMode(mode)}>
                {RANK_LABELS[mode]}
              </button>
            ))}
          </div>
        </div>
        <ol class="ranked-assets-list">
          {rankedAssets.map((asset, index) => (
            <li key={asset.symbol}>
              <span class="leader-rank">{index + 1}</span>
              <span class="leader-company"><strong>{asset.symbol}</strong><small>{asset.area}</small></span>
              <span class="ranked-return"><small>5y / year</small><strong class={tone(asset.cagr_5y)}>{percent(asset.cagr_5y)}</strong></span>
              <span class="ranked-return"><small>Median year</small><strong class={tone(asset.median_annual_return)}>{percent(asset.median_annual_return)}</strong></span>
              <span class="rank-score"><small>{RANK_LABELS[rankMode]} score</small><strong>{asset.scores[rankMode]?.toFixed(0) ?? "—"}</strong></span>
            </li>
          ))}
        </ol>
        <details class="methodology-note">
          <summary>How this ranking works</summary>
          <p>{rankMode === "balanced" ? state.data.ranking_method.balanced : `The ${RANK_LABELS[rankMode].toLowerCase()} lens ranks only its named measurements.`}</p>
          <p>{state.data.ranking_method.history} {state.data.ranking_method.scope}</p>
        </details>
      </section>

      <section class="discover-controls" aria-label="Discover organization">
        <div>
          <span class="control-label">Organize by</span>
          <div class="view-switch">
            <button
              type="button"
              class={viewMode === "type" ? "active" : ""}
              aria-pressed={viewMode === "type"}
              onClick={() => setViewMode("type")}
            >
              Asset type
            </button>
            <button
              type="button"
              class={viewMode === "risk" ? "active" : ""}
              aria-pressed={viewMode === "risk"}
              onClick={() => setViewMode("risk")}
            >
              Risk level
            </button>
          </div>
        </div>
        <button
          type="button"
          class={`behavior-toggle ${behaviorLens ? "active" : ""}`}
          aria-pressed={behaviorLens}
          onClick={() => setBehaviorLens((current) => !current)}
        >
          <span class="toggle-track" aria-hidden="true"><span /></span>
          Show market behavior
        </button>
      </section>

      {behaviorLens ? (
        <aside class="behavior-explainer">
          <span class="behavior-key behavior-key-risk_on">
            {behaviorCounts.risk_on} move with stocks
          </span>
          <span class="behavior-key behavior-key-diversifier">
            {behaviorCounts.diversifier} diversifiers
          </span>
          <span class="behavior-key behavior-key-counterweight">
            {behaviorCounts.counterweight} counterweights
          </span>
          <p>
            Based on available daily history, up to ten years, versus SPY. A counterweight has moved
            differently in this window; it is not a guaranteed hedge.
          </p>
        </aside>
      ) : null}

      {assets.length === 0 && state.data.sectors.length === 0 ? (
        <section class="quiet-state compact">
          <h2>No market measurements are available</h2>
          <p>The scanner returned no assets, so there is nothing to organize.</p>
        </section>
      ) : (
        <div class="discover-groups">
          {groups.map((group) => {
            const groupedAssets = assets.filter((asset) =>
              viewMode === "type"
                ? asset.asset_class === group.key
                : asset.risk_tier === group.key,
            );
            if (groupedAssets.length === 0) return null;
            return (
              <section class="asset-group" key={group.key}>
                <div class="asset-group-heading">
                  <div>
                    <h2>{group.label}</h2>
                    <p>{group.description}</p>
                  </div>
                  <span>{groupedAssets.length}</span>
                </div>
                <div class="discover-asset-grid">
                  {groupedAssets.slice(0, 15).map((asset) => (
                    <AssetCard
                      asset={asset}
                      behaviorLens={behaviorLens}
                      key={asset.symbol}
                    />
                  ))}
                </div>
                {viewMode === "type" && group.key === "stocks" ? (
                  <SectorLeadership data={state.data} />
                ) : null}
              </section>
            );
          })}

          {viewMode === "risk" ? <SectorLeadership data={state.data} /> : null}
        </div>
      )}
    </div>
  );
}
