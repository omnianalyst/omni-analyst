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
    downside?: number | null;
    reliability?: number | null;
    quality?: number | null;
    evidence_complete?: boolean;
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
  decision_table?: Array<{
    tolerate: string;
    allocation: string;
    cagr_pct: number;
    worst_year_pct: number;
  }>;
  decision_table_as_of?: string;
  category_rankings: Record<AssetClass, AssetMetric[]>;
  reliability_rankings?: Record<AssetClass, AssetMetric[]>;
  quality_rankings?: Record<AssetClass, AssetMetric[]>;
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

function plainPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value.toFixed(1)}%`;
}

// The weakest fully-measured component under a headline score. A veteran
// reading "APP 59 / worst: stability 0.9" sees the whole argument. Shared by
// the short answer and the ranked tables so the two can never disagree.
function worstDimension(asset: AssetMetric): { name: string; value: number } | null {
  const measured: Array<[string, number | null | undefined]> = [
    ["growth", asset.scores.durable_growth],
    ["consistency", asset.scores.consistency],
    ["stability", asset.scores.stability],
    ["downside", asset.scores.downside],
    ["diversification", asset.scores.diversification],
  ];
  const present = measured.filter(
    ([, v]) => typeof v === "number",
  ) as Array<[string, number]>;
  if (present.length < 2) return null;
  present.sort((a, b) => a[1] - b[1]);
  return { name: present[0][0], value: present[0][1] };
}

function WeakestDimension({ asset }: { asset: AssetMetric }) {
  const worst = worstDimension(asset);
  if (worst === null) {
    return asset.scores.evidence_complete === false ? (
      <small class="rank-worst">short history</small>
    ) : null;
  }
  return (
    <small class={`rank-worst${worst.value < 25 ? " rank-worst-critical" : ""}`}>
      worst: {worst.name} {worst.value.toFixed(0)}
    </small>
  );
}

// The measured facts behind a row, on demand. Nothing here is derived in the
// browser -- every field is what the scanner measured for that asset.
const RANK_PREVIEW = 10;
const RANK_EXPANDED = 50;

function RankedCategory({
  title,
  description,
  assets,
  total,
  horizon = "long",
  rankBy = "balanced",
}: {
  title: string;
  description: string;
  assets: AssetMetric[];
  total?: number;
  horizon?: "short" | "long";
  rankBy?: "balanced" | "reliability" | "quality";
}) {
  // The caller passes the full ranked list (top slice no longer happens at
  // the call site); this component owns how much shows. 10 by default, 50
  // expanded, scrollable beyond -- enough depth to be a real ranking without
  // rendering 500 rows unprompted.
  const [expanded, setExpanded] = useState(false);
  // ONE rule: the table is ordered by exactly the number in its Score
  // column. Balanced -> balanced composite. Short term -> trailing 1-year
  // return (and the column shows that return, so the order is self-evident).
  // Reliability -> the median composite. A hidden second ordering under a
  // displayed score is how APP presented as "#1 with 59" (2026-08-23).
  const scoreOf = (a: AssetMetric): number => {
    if (rankBy === "quality") return a.scores.quality ?? -Infinity;
    if (rankBy === "reliability") return a.scores.reliability ?? -Infinity;
    if (horizon === "short") return a.returns?.["365d"] ?? -Infinity;
    return a.scores.balanced ?? -Infinity;
  };
  const rankedAll = [...assets].sort((a, b) => scoreOf(b) - scoreOf(a));
  const ranked = rankedAll.slice(0, expanded ? RANK_EXPANDED : RANK_PREVIEW);
  return (
    <section class="rank-category">
      <div class="asset-group-heading">
        <div><h2>{title}</h2><p>{description}</p></div>
        <span>
          {total && total > rankedAll.length
            ? `Top ${rankedAll.length} of ${total} tracked`
            : `${rankedAll.length} tracked`}
        </span>
      </div>
      {rankedAll.length > RANK_PREVIEW ? (
        <button
          type="button"
          class="rank-more"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded
            ? "Show fewer"
            : `Show more · next ${Math.min(
                RANK_EXPANDED - RANK_PREVIEW,
                rankedAll.length - RANK_PREVIEW,
              )}`}
        </button>
      ) : null}
      <div class="rank-table-wrap">
        <table class="rank-table">
          <thead>
            <tr>
              <th>Rank</th>
              <th>Asset</th>
              <th>
                {rankBy === "quality"
                  ? "Quality"
                  : rankBy === "reliability"
                  ? "Reliability"
                  : horizon === "short"
                    ? "1-year return"
                    : "Balanced score"}
              </th>
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
                <td>
                  <strong class="canonical-score">
                    {rankBy === "quality"
                      ? asset.scores.quality?.toFixed(0) ?? "—"
                      : rankBy === "reliability"
                      ? asset.scores.reliability?.toFixed(0) ?? "—"
                      : horizon === "short"
                        ? `${(asset.returns?.["365d"] ?? 0).toFixed(1)}%`
                        : asset.scores.balanced?.toFixed(0) ?? "—"}
                  </strong>
                  <WeakestDimension asset={asset} />
                </td>
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

// Section 0: the first-glance answer. One pick per temperament -- the
// highest balanced score within each volatility tier, across every measured
// asset class -- then, for the person who would rather not pick, the
// measured mixes. Everything below this section is the evidence for it.
function ShortAnswer({ data }: { data: ScannerData }) {
  const universe = Object.values(data.category_rankings)
    .flat()
    .filter(
      (a) =>
        a.scores.evidence_complete !== false &&
        typeof a.scores.balanced === "number" &&
        a.risk_tier !== "unrated",
    );
  const tiers = TIER_COLUMNS.map(({ tier, title, hint, accent }) => {
    const inTier = universe
      .filter((a) => a.risk_tier === tier)
      .sort((a, b) => (b.scores.balanced ?? -Infinity) - (a.scores.balanced ?? -Infinity));
    return { tier, title, hint, accent, pick: inTier[0] ?? null, measured: inTier.length };
  });
  return (
    <section class="d-answer" aria-label="The short answer">
      <div class="d-answer-head">
        <h2>The short answer</h2>
        <p>
          One pick per temperament -- the highest balanced score in each volatility tier,
          across every measured asset class. The weakest dimension is stated, not hidden.
          Everything below is the evidence.
        </p>
      </div>
      <div class="d-answer-grid">
        {tiers.map(({ tier, title, hint, accent, pick, measured }) => (
          <article class="d-answer-card" key={tier} style={`border-top-color:${accent}`}>
            <header>
              <span class="d-answer-tier" style={`color:${accent}`}>{title}</span>
              <small>{hint}</small>
            </header>
            {pick ? (
              <>
                <strong class="d-answer-symbol">{pick.symbol}</strong>
                <small class="d-answer-name">{pick.name} · {pick.area}</small>
                <dl class="d-answer-facts">
                  <div>
                    <dt>Median year</dt>
                    <dd class={tone(pick.median_annual_return)}>{percent(pick.median_annual_return)}</dd>
                  </div>
                  <div>
                    <dt>Volatility</dt>
                    <dd>{plainPercent(pick.volatility)}</dd>
                  </div>
                  <div>
                    <dt>Weakest</dt>
                    <dd>
                      {(() => {
                        const worst = worstDimension(pick);
                        return worst ? `${worst.name} ${worst.value.toFixed(0)}` : "—";
                      })()}
                    </dd>
                  </div>
                </dl>
                <small class="d-answer-count">
                  {measured > 1
                    ? `${measured} assets measured in this tier`
                    : measured === 1
                      ? "the only asset measured in this tier"
                      : "nothing measured in this tier yet"}
                </small>
              </>
            ) : (
              <p class="d-answer-empty">No asset has finished measuring in this tier.</p>
            )}
          </article>
        ))}
      </div>
      {data.decision_table && data.decision_table.length > 0 ? (
        <div class="d-answer-mixes">
          <div class="d-answer-head">
            <h2>If you would rather hold a mix</h2>
            <p>
              Equal-weight mixes and what they measured: the return per year, and the worst
              calendar year each one cost. Pick the worst year you would have sat through.
              History, not a promise.
            </p>
          </div>
          <div class="decision-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Worst year to accept</th>
                  <th>The mix</th>
                  <th>Returned per year</th>
                  <th>Its worst year</th>
                </tr>
              </thead>
              <tbody>
                {data.decision_table.map((row) => (
                  <tr key={row.tolerate}>
                    <td><strong>{row.tolerate}</strong></td>
                    <td>{row.allocation}</td>
                    <td class="mono">{row.cagr_pct.toFixed(1)}%</td>
                    <td class="mono">{row.worst_year_pct.toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p class="d-answer-as-of">
            Measured through {data.decision_table_as_of ?? "the latest complete window"}.
          </p>
        </div>
      ) : null}
    </section>
  );
}

// Section 1: the best of each category, at a glance. Short = the trailing
// year; long = the median calendar year over everything measured (3-year
// floor). Top 8 per category; the full tables live behind the disclosure.
function BestMeasured({ data }: { data: ScannerData }) {
  const [horizon, setHorizon] = useState<"short" | "long">("long");
  const [rankBy, setRankBy] = useState<"balanced" | "reliability" | "quality">(
    "balanced",
  );
  const reliability = data.reliability_rankings;
  const quality = data.quality_rankings;
  return (
    <section class="best-measured" aria-label="Best measured by category">
      <div class="section-heading-row section-heading-compact">
        <div>
          <h2>The best measured, by category</h2>
          <p>
            {horizon === "short"
              ? "Ranked by the trailing year -- who is winning right now. A single year's winner is often an extreme event; ZEC's +1223% was real, and it says nothing about the next year."
              : "Ordered by the score shown. Balanced weighs growth, consistency, stability and diversification; every row states its weakest dimension. Short term orders by trailing 1-year return."}
          </p>
        </div>
        <div class="view-switch" role="tablist" aria-label="Rank by">
          <button
            type="button"
            class={rankBy === "balanced" ? "active" : ""}
            onClick={() => setRankBy("balanced")}
            title="Weighted average of components; incomplete records reweighted"
          >
            Balanced
          </button>
          <button
            type="button"
            class={rankBy === "quality" ? "active" : ""}
            onClick={() => setRankBy("quality")}
            title="Median of the three asset dimensions -- growth, consistency, downside -- every one measured, none in the bottom quartile. Market correlation never gates: it is a portfolio fact, not the stock's fault. The candidate list."
          >
            Quality
          </button>
          <button
            type="button"
            class={rankBy === "reliability" ? "active" : ""}
            onClick={() => setRankBy("reliability")}
            title="Median of four components including diversification -- portfolio building blocks that do not just duplicate the index. A failing dimension disqualifies."
          >
            Reliability
          </button>
        </div>
        {rankBy === "balanced" ? (
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
        ) : null}
      </div>
      {CATEGORY_DETAILS.map((category) => {
        const universe = data.category_rankings[category.key] ?? [];
        const eligible =
          (rankBy === "quality"
            ? quality?.[category.key]
            : reliability?.[category.key]) ?? [];
        // The default (balanced) list gets the same evidence floor as
        // reliability: a name missing long-term components has not been
        // measured, and outscoring fully-measured names on its remaining
        // half is the EA failure (stability alone carried it to #2,
        // 2026-08-22). Incomplete records sort to the bottom, marked.
        const completeRecords = universe.filter(
          (a) => a.scores.evidence_complete !== false,
        );
        const incompleteRecords = universe.filter(
          (a) => a.scores.evidence_complete === false,
        );
        const balancedUniverse = [...completeRecords, ...incompleteRecords];
        return (
          <RankedCategory
            key={category.key}
            title={category.title}
            description={
              rankBy === "reliability" && universe.length > 0
                ? `${category.description} ${eligible.length} of ${universe.length} qualify for best overall -- the rest fail a dimension or lack the history to measure one.`
                : category.description
            }
            assets={rankBy === "reliability" ? eligible : balancedUniverse}
            total={universe.length}
            horizon={horizon}
            rankBy={rankBy}
          />
        );
      })}
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

      <ShortAnswer data={state.data} />

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
