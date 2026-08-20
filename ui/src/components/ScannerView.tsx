import { useEffect, useMemo, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError, request, sendJson } from "../lib/api";
import { equalWeightAverage, riskShares } from "../lib/blend";
import { getRegime, type RegimeResponse } from "../lib/autonomous";
import { CompaniesPanel } from "./CompaniesPanel";
import { ErrorState } from "./ErrorState";
import { Hint } from "./Hint";
import { LearnWhy } from "./LearnWhy";
import { Loading } from "./Loading";

type AssetClass = "stocks" | "crypto" | "defensive";
type RiskTier = "low" | "medium" | "high" | "unrated";
type MarketBehavior = "risk_on" | "diversifier" | "counterweight" | "unrated";

export interface AssetMetric {
  symbol: string;
  name: string;
  area: string;
  asset_class: AssetClass;
  risk_tier: RiskTier;
  market_behavior: MarketBehavior;
  correlation_to_spy?: number | null;
  sharpe?: number | null;
  returns?: { "365d"?: number | null };
  volatility?: number | null;
  max_drawdown?: number | null;
  cagr_5y?: number | null;
  cagr_10y?: number | null;
  median_annual_return?: number | null;
  positive_year_rate?: number | null;
  income_yield?: number | null;
  expense_ratio?: number | null;
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

interface ScenarioBucket {
  name: string;
  role: string;
  representative?: { symbol: string; reason: string } | null;
  assets: AssetMetric[];
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

export interface PortfolioHistory {
  window_start: string;
  window_end: string;
  volatility: number;
  median_year: number;
  worst_year: { year: string; return: number };
  best_year: { year: string; return: number };
  worst_drawdown: number;
  up_years: number;
  complete_years: number;
  path?: Array<[number, number]>;
}

interface ScannerData {
  buckets: ScenarioBucket[];
  portfolio_history: PortfolioHistory | null;
  income_as_of?: string;
  decision_table?: Array<{
    tolerate: string;
    allocation: string;
    cagr_pct: number;
    worst_year_pct: number;
  }>;
  decision_table_as_of?: string;
  comparator_universe?: Array<{ symbol: string; name: string; kind?: string }>;
  category_rankings: Record<AssetClass, AssetMetric[]>;
  sectors: SectorLeaders[];
  overall_leaders: OverallLeader[];
  ranking_method: { balanced: string; history: string; scope: string; risk_tier: string };
  sector_coverage: { available: number; total: number; window_sessions: number };
  coverage: {
    policy_version: string;
    complete: boolean;
    feed_defects?: Array<{
      symbol: string;
      reasons: string[];
      last_close: number;
      census_price?: number;
    }>;
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
    description: "The seven-name ladder -- one name per role, ranked on the same measured framework.",
  },
];

const BEHAVIOR_LABELS: Record<MarketBehavior, string> = {
  risk_on: "Moves with stocks",
  diversifier: "Diversifier",
  counterweight: "Counterweight",
  unrated: "Not measured",
};

const TOP_PER_TIER = 10;

const TIER_COLUMNS: Array<{
  tier: Exclude<RiskTier, "unrated">;
  title: string;
  hint: string;
  accent: string;
}> = [
  { tier: "low", title: "Steady", hint: "under 10% volatility", accent: "var(--tier-fresh)" },
  { tier: "medium", title: "Balanced", hint: "10\u201330% volatility", accent: "var(--accent)" },
  { tier: "high", title: "Aggressive", hint: "30%+ volatility", accent: "var(--tier-aging)" },
];

function percent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;
}

function tone(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return value > 0 ? "value-positive" : value < 0 ? "value-negative" : "";
}

// The same floor the server applies before letting the median feed a rank
// (scanner.py: has_long_enough_record). A median of one or two yearly returns
// is noise wearing a median's clothes, and an asset below the floor stays out
// of the builder -- it remains in the full table, where the number is labelled
// for what it is.
const MIN_COMPLETE_YEARS = 3;

function classLabel(asset: AssetMetric): string {
  const entry = CATEGORY_DETAILS.find((category) => category.key === asset.asset_class);
  return entry ? entry.title : asset.asset_class;
}

// Ranked by median annual return -- the steady-centre metric, not the trailing
// year. An asset without a measured median cannot be ranked by it and stays
// out rather than being seated by a stand-in number.
function byMedian(a: AssetMetric, b: AssetMetric): number {
  const left = a.median_annual_return;
  const right = b.median_annual_return;
  if (left === null || left === undefined) return 1;
  if (right === null || right === undefined) return -1;
  return right - left;
}

function rankableByMedian(asset: AssetMetric): boolean {
  return (
    asset.median_annual_return != null && asset.complete_years >= MIN_COMPLETE_YEARS
  );
}

// The measured facts behind a row, on demand. Nothing here is derived in the
// browser -- every field is what the scanner measured for that asset.
function RankedCategory({
  title,
  description,
  assets,
  horizon = "long",
}: {
  title: string;
  description: string;
  assets: AssetMetric[];
  horizon?: "short" | "long";
}) {
  const ranked = [...assets].sort((a, b) => {
    if (horizon === "short") {
      return (b.returns?.["365d"] ?? -Infinity) - (a.returns?.["365d"] ?? -Infinity);
    }
    const left = a.median_annual_return;
    const right = b.median_annual_return;
    if (left == null) return 1;
    if (right == null) return -1;
    return right - left;
  });
  return (
    <section class="rank-category">
      <div class="asset-group-heading">
        <div><h2>{title}</h2><p>{description}</p></div>
        <span>{ranked.length}</span>
      </div>
      <div class="rank-table-wrap">
        <table class="rank-table">
          <thead>
            <tr>
              <th>Rank</th>
              <th>Asset</th>
              <th>Score</th>
              <th class={horizon === "short" ? "col-active" : ""}>1 year</th>
              <th class={horizon === "long" ? "col-active" : ""}>Median year, all measured</th>
              <th>Volatility</th>
              <th>Market role</th>
            </tr>
          </thead>
          <tbody>
            {ranked.map((asset, index) => (
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
                <td class={`${tone(asset.returns?.["365d"])} ${horizon === "short" ? "col-active" : ""}`}>
                  {percent(asset.returns?.["365d"])}
                </td>
                <td class={`${tone(asset.median_annual_return)} ${horizon === "long" ? "col-active" : ""}`}>
                  {percent(asset.median_annual_return)}
                </td>
                <td>{percent(asset.volatility)}</td>
                <td>
                  <span class={`behavior-badge behavior-badge-${asset.market_behavior}`}>
                    {BEHAVIOR_LABELS[asset.market_behavior]}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// Section 1: the best of each category, at a glance. Short = the trailing
// year; long = the median calendar year over everything measured (3-year
// floor). Top 8 per category; the full tables live behind the disclosure.
function BestMeasured({ data }: { data: ScannerData }) {
  const [horizon, setHorizon] = useState<"short" | "long">("long");
  return (
    <section class="best-measured" aria-label="Best measured by category">
      <div class="section-heading-row section-heading-compact">
        <div>
          <h2>The best measured, by category</h2>
          <p>
            {horizon === "short"
              ? "Ranked by the trailing year -- who is winning right now. A single year's winner is often an extreme event; ZEC's +1223% was real, and it says nothing about the next year."
              : "Ranked by median calendar year over everything measured (3-year minimum) -- who wins over time."}
          </p>
        </div>
        <div class="view-switch" role="tablist" aria-label="Ranking horizon">
          <button
            type="button"
            class={horizon === "long" ? "active" : ""}
            onClick={() => setHorizon("long")}
          >
            Long term
          </button>
          <button
            type="button"
            class={horizon === "short" ? "active" : ""}
            onClick={() => setHorizon("short")}
          >
            Short term
          </button>
        </div>
      </div>
      {CATEGORY_DETAILS.map((category) => (
        <RankedCategory
          key={category.key}
          title={category.title}
          description={category.description}
          assets={(data.category_rankings[category.key] ?? []).slice(
            0,
            5,
          )}
          horizon={horizon}
        />
      ))}
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
  const [rankingsOpen, setRankingsOpen] = useState(false);
  const [companiesOpen, setCompaniesOpen] = useState(false);

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
  const { coverage } = state.data;


  return (
    <div class="scanner-view product-page">
      <header class="discover-page-heading">
        <div><h1>Discover</h1><p>What's worth tracking -- ranked on everything measured.</p></div>
        <div class="discover-compact-meta">
          <LearnWhy />
          <time dateTime={state.data.as_of}>Updated {new Date(state.data.as_of).toLocaleString()}</time>
        </div>
      </header>

      <BestMeasured data={state.data} />

      <button
        type="button"
        class="disclosure-button"
        aria-expanded={rankingsOpen}
        onClick={() => setRankingsOpen((open) => !open)}
      >
        <span>{rankingsOpen ? "Hide every measured asset" : `Every measured asset · ${assetCount} ranked`}</span>
        <span aria-hidden="true">{rankingsOpen ? "−" : "+"}</span>
      </button>
      {rankingsOpen ? (
        <div class="detail-drawer">
          {CATEGORY_DETAILS.map((category) => (
            <RankedCategory
              key={category.key}
              title={category.title}
              description={category.description}
              assets={state.data.category_rankings[category.key] ?? []}
            />
          ))}
        </div>
      ) : null}

      <button
        type="button"
        class="disclosure-button"
        aria-expanded={companiesOpen}
        onClick={() => setCompaniesOpen((open) => !open)}
      >
        <span>{companiesOpen ? "Hide individual companies" : `Individual companies · ${companyCount} ranked`}</span>
        <span aria-hidden="true">{companiesOpen ? "−" : "+"}</span>
      </button>
      {companiesOpen ? (
        <div class="detail-drawer">
          <div class="detail-block">
            <SectorLeadership data={state.data} />
            <CompaniesPanel />
          </div>
        </div>
      ) : null}

      <footer class="scanner-foot">
        <div class="scanner-foot-row">
          <details class="foot-details">
            <summary>Method &amp; coverage · {coverage.complete ? "complete" : "closing"}</summary>
            <div class="foot-panel">
              <p>{state.data.ranking_method.balanced} {state.data.ranking_method.scope}</p>
              <p>{state.data.ranking_method.history}</p>
              <p>{state.data.ranking_method.risk_tier}</p>
              <p>Volatility is annualized from daily returns. Market role uses correlation to SPY and is descriptive, not a guaranteed hedge.</p>
              <p>
                Policy {coverage.policy_version} · {coverage.crypto.ranked} crypto ranked ·{" "}
                {coverage.crypto.unmapped.length} need mapping ·{" "}
                {coverage.companies.sectors_measured}/{coverage.companies.sectors_required} company sectors
                {(coverage.feed_defects?.length ?? 0) > 0
                  ? ` · ${coverage.feed_defects!.length} refused for a broken price feed`
                  : ""}
              </p>
              <div class="coverage-audit-grid">
                {(coverage.feed_defects?.length ?? 0) > 0 ? (
                  <div>
                    <strong>Broken price feeds ({coverage.feed_defects!.length})</strong>
                    <ul class="coverage-audit-list">
                      {coverage.feed_defects!.map((defect) => (
                        <li key={defect.symbol}>
                          <span>{defect.symbol}</span> — refused, not ranked: {defect.reasons.join("; ")}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                <div>
                  <strong>Explicitly excluded ({coverage.crypto.excluded.length})</strong>
                  <p>{coverage.crypto.excluded.length
                    ? coverage.crypto.excluded.map((item) => `${item.symbol}: ${item.reason}`).join(" · ")
                    : "No live exclusions returned."}</p>
                </div>
                <div>
                  <strong>Needs a verified mapping ({coverage.crypto.unmapped.length})</strong>
                  {coverage.crypto.unmapped.length ? (
                    <ul class="coverage-audit-list">
                      {coverage.crypto.unmapped.map((item) => (
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
                  <strong>Insufficient price history ({coverage.crypto.insufficient_history.length})</strong>
                  <p>{coverage.crypto.insufficient_history.length
                    ? coverage.crypto.insufficient_history.map((item) => `${item.symbol}: ${item.observations}/${item.required} observations`).join(" · ")
                    : "Every mapped asset meets the history floor."}</p>
                </div>
                <div>
                  <strong>Industries</strong>
                  <p>{coverage.industries.reason}</p>
                </div>
              </div>
            </div>
          </details>
        </div>
      </footer>
    </div>
  );
}
