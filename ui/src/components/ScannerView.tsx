import { useEffect, useMemo, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError, request, sendJson } from "../lib/api";
import { equalWeightAverage, riskShares } from "../lib/blend";
import { getRegime, type RegimeResponse } from "../lib/autonomous";
import { CompaniesPanel } from "./CompaniesPanel";
import { CustomCompare } from "./CustomCompare";
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
    description: "Major digital assets ranked on the same measured framework.",
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
function AssetInfo({ asset }: { asset: AssetMetric }) {
  return (
    <div class="builder-info" role="note">
      <p class="builder-info-kind">
        {classLabel(asset)} · {asset.area}
        {asset.market_cap_rank ? ` · market cap #${asset.market_cap_rank}` : ""}
        {` · ${asset.history_years}y measured, ${asset.complete_years} complete years`}
      </p>
      <dl>
        <div><dt>Volatility</dt><dd>{percent(asset.volatility)}</dd></div>
        <div><dt>Max drawdown</dt><dd>{percent(asset.max_drawdown)}</dd></div>
        <div><dt>1 year</dt><dd class={tone(asset.returns?.["365d"])}>{percent(asset.returns?.["365d"])}</dd></div>
        <div><dt>5y / year</dt><dd class={tone(asset.cagr_5y)}>{percent(asset.cagr_5y)}</dd></div>
        <div><dt>10y / year</dt><dd class={tone(asset.cagr_10y)}>{percent(asset.cagr_10y)}</dd></div>
        <div><dt>Median year</dt><dd class={tone(asset.median_annual_return)}>{percent(asset.median_annual_return)}</dd></div>
        <div><dt>Up years</dt><dd>{asset.positive_year_rate != null ? `${asset.positive_year_rate.toFixed(1)}%` : "—"}</dd></div>
        <div><dt>Income yield</dt><dd>{asset.income_yield != null ? `${asset.income_yield.toFixed(1)}%/yr` : "none"}</dd></div>
        <div><dt>Fund fee</dt><dd>{asset.expense_ratio != null ? `${asset.expense_ratio.toFixed(2)}%/yr` : "—"}</dd></div>
        <div><dt>Correlation to stocks</dt><dd>{asset.correlation_to_spy?.toFixed(2) ?? "—"}</dd></div>
        <div><dt>Sharpe</dt><dd>{asset.sharpe?.toFixed(2) ?? "withheld below 5% vol"}</dd></div>
        <div><dt>Balanced score</dt><dd>{asset.scores.balanced?.toFixed(0) ?? "—"}</dd></div>
      </dl>
    </div>
  );
}

function BuilderRow({
  asset,
  rank,
  infoOpen,
  onToggleInfo,
}: {
  asset: AssetMetric;
  rank: number;
  infoOpen: boolean;
  onToggleInfo: (symbol: string) => void;
}) {
  return (
    <>
      <li class={infoOpen ? "builder-row-open" : undefined}>
        <span class="leader-rank">{rank}</span>
        <span class="top-pick-asset">
          <strong>{asset.symbol}</strong>
          <small>{asset.name}</small>
        </span>
        <span class={`behavior-badge behavior-badge-${asset.market_behavior}`}>
          {BEHAVIOR_LABELS[asset.market_behavior]}
        </span>
        <strong class={`top-pick-return ${tone(asset.median_annual_return)}`}>
          {percent(asset.median_annual_return)}
        </strong>
        <button
          type="button"
          class={`info-dot ${infoOpen ? "info-dot-open" : ""}`}
          aria-label={`${asset.symbol} details`}
          aria-expanded={infoOpen}
          onClick={() => onToggleInfo(asset.symbol)}
        >
          i
        </button>
      </li>
      {infoOpen ? <li class="builder-info-row"><AssetInfo asset={asset} /></li> : null}
    </>
  );
}

// One header over every list so the number column says what it is: the median
// of complete calendar years, per year.
function BuilderListHeader() {
  return (
    <div class="builder-col-head" aria-hidden="true">
      <span>Asset</span>
      <span>Median / yr</span>
    </div>
  );
}

function BuilderList({
  assets,
  openInfo,
  onToggleInfo,
  ranked,
}: {
  assets: AssetMetric[];
  openInfo: string | null;
  onToggleInfo: (symbol: string) => void;
  ranked?: boolean;
}) {
  if (assets.length === 0) return <p class="top-pick-empty">Nothing measured here yet.</p>;
  return (
    <>
      <BuilderListHeader />
      <ol>
        {assets.map((asset, index) => (
          <BuilderRow
            key={asset.symbol}
            asset={asset}
            rank={ranked ? index + 1 : 0}
            infoOpen={openInfo === asset.symbol}
            onToggleInfo={onToggleInfo}
          />
        ))}
      </ol>
    </>
  );
}

// The four macro regimes every market moment falls into. The server names the
// buckets and owns the mapping of assets to regimes; this is display naming
// only, keyed by the server's bucket name so a regime added server-side
// renders under its own name instead of vanishing.
const REGIME_LABELS: Record<string, string> = {
  Growth: "Growth",
  Debasement: "Inflation & stagflation",
  Deflation: "Deflation",
  Safety: "Recession & crisis",
};
const REGIME_ORDER = ["Growth", "Debasement", "Deflation", "Safety"];
const REGIME_COLORS: Record<string, string> = {
  Growth: "var(--accent)",
  Debasement: "var(--tier-aging)",
  Deflation: "var(--tier-fresh)",
  Safety: "var(--border-strong)",
};

function PortfolioHistoryBlock({ history }: { history: PortfolioHistory }) {
  return (
    <div class="history-block">
      <p class="metric-kicker">
        The mix, measured · {history.window_start} to {history.window_end} ·{" "}
        {history.complete_years} complete years, rebalanced every January
      </p>
      <dl class="history-facts">
        <div>
          <dt><Hint term="median_year">Median year</Hint></dt>
          <dd class={history.median_year >= 0 ? "value-positive" : "value-negative"}>
            {history.median_year > 0 ? "+" : ""}{history.median_year.toFixed(1)}%
          </dd>
        </div>
        <div>
          <dt><Hint term="volatility">Volatility</Hint></dt>
          <dd>{history.volatility.toFixed(1)}%</dd>
        </div>
        <div>
          <dt><Hint term="max_drawdown">Worst fall</Hint></dt>
          <dd class="value-negative">{history.worst_drawdown.toFixed(1)}%</dd>
        </div>
        <div>
          <dt>Worst year</dt>
          <dd class="value-negative">
            {history.worst_year.return.toFixed(1)}% <small class="unit-note">in {history.worst_year.year}</small>
          </dd>
        </div>
        <div>
          <dt>Best year</dt>
          <dd class="value-positive">
            +{history.best_year.return.toFixed(1)}% <small class="unit-note">in {history.best_year.year}</small>
          </dd>
        </div>
        <div>
          <dt><Hint term="positive_year_rate">Up years</Hint></dt>
          <dd>{history.up_years.toFixed(0)}%</dd>
        </div>
      </dl>
      <p class="risk-note">
        What holding these four at equal weight actually did over the window where all four
        have prices — history, not a forecast, and the window is short (it starts when the
        youngest holding began trading).
      </p>
    </div>
  );
}

// The regime a pick protects, as a phrase a first-time investor can read.
function regimePhrase(bucketName: string): string {
  switch (bucketName) {
    case "Growth":
      return "grows when the economy grows";
    case "Debasement":
      return "holds value when inflation or stagflation hits";
    case "Deflation":
      return "gains when rates and prices fall";
    case "Safety":
      return "keeps cash safe through a recession";
    default:
      return bucketName.toLowerCase();
  }
}

// The regime pick for THE portfolio: the bucket's designated sleeve when it
// survived ranking (policy -- the regime's definition, not its recent
// winner), else the best measured pick under steady/balanced risk. A
// high-volatility asset can still top a bucket's score and shows in that
// regime's alternatives; it just cannot become the safe answer.
function regimePick(bucket: ScenarioBucket): AssetMetric | null {
  const designated = bucket.representative?.symbol;
  if (designated) {
    const asset = bucket.assets.find((entry) => entry.symbol === designated);
    if (asset) return asset;
  }
  return (
    bucket.assets.find(
      (asset) =>
        rankableByMedian(asset) &&
        asset.risk_tier !== "high" &&
        asset.risk_tier !== "unrated",
    ) ??
    bucket.assets.find(rankableByMedian) ??
    null
  );
}

function BlendLegendRow({
  pick,
  bucketName,
  riskShare,
}: {
  pick: AssetMetric;
  bucketName: string;
  riskShare: number;
}) {
  const weight = 100 / 4;
  return (
    <article class="holding-card" style={{ borderTopColor: REGIME_COLORS[bucketName] }}>
      <header>
        <strong>{pick.symbol}</strong>
        <span class="mono">{weight.toFixed(0)}%</span>
      </header>
      <p class="holding-role">{REGIME_LABELS[bucketName] ?? bucketName}</p>
      <p class="holding-phrase muted">{regimePhrase(bucketName)}</p>
      <dl>
        <div>
          <dt><Hint term="median_year">Median yr</Hint></dt>
          <dd class={tone(pick.median_annual_return)}>{percent(pick.median_annual_return)}</dd>
        </div>
        <div>
          <dt><Hint term="volatility">Vol</Hint></dt>
          <dd>{pick.volatility != null ? `${pick.volatility.toFixed(1)}%` : "—"}</dd>
        </div>
        <div>
          <dt><Hint term="max_drawdown">Worst fall</Hint></dt>
          <dd>{pick.max_drawdown != null ? `${pick.max_drawdown.toFixed(0)}%` : "—"}</dd>
        </div>
        <div>
          <dt><Hint term="risk_share">Of risk</Hint></dt>
          <dd>{riskShare > 0 ? `${(riskShare * 100).toFixed(0)}%` : "—"}</dd>
        </div>
        <div>
          <dt><Hint term="positive_year_rate">Up years</Hint></dt>
          <dd>{pick.positive_year_rate != null ? `${Math.round(pick.positive_year_rate)}%` : "—"}</dd>
        </div>
      </dl>
    </article>
  );
}

// THE answer, always visible: one holding per macro regime at equal weight.
// Four positions cover growth, inflation, deflation, and recession -- whatever
// the market does next, one of them is built for it. The arithmetic on
// measured medians is labelled for what it is; it is not a backtest.
interface LocalCompareResponse {
  custom: PortfolioHistory;
}

function ThePortfolio({ data }: { data: ScannerData }) {
  const buckets = REGIME_ORDER
    .map((name) => data.buckets.find((bucket) => bucket.name === name))
    .filter((bucket): bucket is ScenarioBucket => bucket !== undefined);
  const picks = buckets
    .map((bucket) => ({ bucket, pick: regimePick(bucket) }))
    .filter((entry): entry is { bucket: ScenarioBucket; pick: AssetMetric } => entry.pick !== null);

  if (picks.length === 0) {
    return <p class="quiet-line">The portfolio cannot be assembled yet — no regime has a measured pick.</p>;
  }

  const weight = 1 / picks.length;
  // median_annual_return is already in percent units; the equal-weight blend
  // is their plain average via the tested helper -- no further scaling.
  const total = equalWeightAverage(picks.map((entry) => entry.pick.median_annual_return ?? null));
  const positiveTotal = equalWeightAverage(
    picks.map((entry) => Math.max(entry.pick.median_annual_return ?? 0, 0)),
  );
  // Equal capital does not mean equal risk: with a quarter of the money in
  // each, a sleeve's share of portfolio risk is its volatility over the sum
  // of the four. Shown rather than hidden -- the growth sleeve dominates and
  // the reader deserves to see by how much.
  const shares = riskShares(picks.map((entry) => entry.pick.volatility ?? null));
  // Equal-weight income and cost from the sponsors' published figures. Gold
  // pays nothing; a missing figure for any sleeve means the line stays hidden
  // rather than guessing a yield.
  const yields = picks.map((entry) => entry.pick.income_yield ?? null);
  const costs = picks.map((entry) => entry.pick.expense_ratio ?? null);
  const income =
    yields.every((value) => value != null) && costs.every((value) => value != null)
      ? {
          yield: equalWeightAverage(yields),
          cost: equalWeightAverage(costs),
        }
      : null;

  const h = data.portfolio_history;
  const startAmount = 10000;
  const [split, setSplit] = useState<Record<string, number>>({ VTI: 25, GLD: 25, TLT: 25, SGOV: 25 });
  const [splitResult, setSplitResult] = useState<PortfolioHistory | null>(null);
  const [splitBusy, setSplitBusy] = useState(false);
  const isDefault =
    split.VTI === 25 && split.GLD === 25 && split.TLT === 25 && split.SGOV === 25;

  // Debounced compare: drag sliders, the ghost line follows once you pause.
  // Same endpoint, same window-pinning as the mix comparator -- the ghost is
  // computed against exactly the hero's dates.
  useEffect(() => {
    if (isDefault) {
      setSplitResult(null);
      return;
    }
    const t = window.setTimeout(() => {
      setSplitBusy(true);
      void sendJson<LocalCompareResponse>(
        "POST",
        "/scanner/custom-portfolio",
        {
          positions: Object.entries(split)
            .filter(([, w]) => w > 0)
            .map(([symbol, weight]) => ({ symbol, weight })),
        },
        authHeaderIfPresent(),
      )
        .then((r) => setSplitResult(r.custom))
        .catch(() => setSplitResult(null))
        .finally(() => setSplitBusy(false));
    }, 350);
    return () => window.clearTimeout(t);
  }, [split, isDefault]);

  return (
    <section class="the-portfolio" aria-label="The portfolio">
      <div class="d-hero">
        <div class="d-kicker">
          The portfolio · one holding per future · 25% each · rebalanced yearly
        </div>
        {h && (h.path?.length ?? 0) > 0 ? (
          <>
            <div class="d-money">
              {((h.path![h.path!.length - 1][1] / 100) * startAmount).toLocaleString("en-US", {
                style: "currency",
                currency: "USD",
                maximumFractionDigits: 0,
              })}
            </div>
            <div class="d-sub">
              what <b>{startAmount.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })}</b>{" "}
              in the four-way became, {h.window_start} → {h.window_end} — median year{" "}
              <b>+{h.median_year.toFixed(1)}%</b>, up {Math.round(h.up_years)}% of years
            </div>
            <div class="d-worst">
              worst year {h.worst_year.return.toFixed(1)}% ({h.worst_year.year}) · worst fall {h.worst_drawdown.toFixed(1)}%
            </div>
          </>
        ) : (
          <div class="d-money">{"\u2014"}</div>
        )}
      </div>

      {h && (h.path?.length ?? 0) > 1 ? (
        <HeroChart history={h} ghost={splitResult} ghostLabel="your split" />
      ) : null}

      <SplitTuner
        split={split}
        setSplit={setSplit}
        result={splitResult}
        busy={splitBusy}
        isDefault={isDefault}
        startAmount={startAmount}
        defaultMedian={h?.median_year ?? null}
        defaultWorst={h?.worst_year.return ?? null}
      />

      <div class="top-picks-heading">
        <h2>What you hold</h2>
        <p>
          Each sleeve is the definition of its future — the whole market for growth, gold for
          inflation, long Treasuries for deflation, T-bills for crisis.{" "}
          <Hint term="rebalance">Rebalance</Hint> back to a quarter about once a year.
        </p>
      </div>
      <div class="d-holdings">
        {picks.map(({ bucket, pick }, index) => (
          <div class="d-holding" key={pick.symbol}>
            <span class="pct">25%</span>
            <div class="sym">{pick.symbol}</div>
            <div class={`role role-${bucket.name.toLowerCase().replace("/", "")}`}>
              {REGIME_LABELS[bucket.name] ?? bucket.name}
            </div>
            <dl>
              <div><dt>median yr</dt><dd>{percent(pick.median_annual_return)}</dd></div>
              <div><dt>worst fall</dt><dd>{pick.max_drawdown != null ? `${pick.max_drawdown.toFixed(0)}%` : "—"}</dd></div>
              <div><dt>of risk</dt><dd>{shares[index] > 0 ? `${Math.round((shares[index] ?? 0) * 100)}%` : "0%"}</dd></div>
            </dl>
          </div>
        ))}
      </div>

      {data.decision_table && data.decision_table.length > 0 ? (
        <DecisionTable rows={data.decision_table} asOf={data.decision_table_as_of} />
      ) : null}

      {income != null ? (
        <p class="risk-note income-line">
          Income about {income.yield.toFixed(1)}%/yr, costing {income.cost.toFixed(2)}%/yr in
          fund fees (equal weight, sponsor figures as of {data.income_as_of ?? "last audit"}).
        </p>
      ) : null}
    </section>
  );
}

// The sliders from the local lab, on production math: drag the four sleeves,
// your dashed line rides the hero chart, and the delta strip says what the
// drag bought and cost against the default. Reset is one click back to 25x4.
function SplitTuner({
  split,
  setSplit,
  result,
  busy,
  isDefault,
  startAmount,
  defaultMedian,
  defaultWorst,
}: {
  split: Record<string, number>;
  setSplit: (next: Record<string, number>) => void;
  result: PortfolioHistory | null;
  busy: boolean;
  isDefault: boolean;
  startAmount: number;
  defaultMedian: number | null;
  defaultWorst: number | null;
}) {
  const total = Object.values(split).reduce((a, b) => a + b, 0) || 1;
  const sleeves: Array<[string, string, string]> = [
    ["VTI", "Stocks", "#34d399"],
    ["GLD", "Gold", "#fbbf24"],
    ["TLT", "Bonds", "#a5b4fc"],
    ["SGOV", "Cash", "#f87171"],
  ];
  const money = (growth: number) =>
    ((growth / 100) * startAmount).toLocaleString("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 0,
    });
  return (
    <div class="d-tuner">
      <p class="metric-kicker">
        Try your own split {busy ? "· measuring…" : "· the dashed line is yours"}
      </p>
      <div class="d-sliders">
        {sleeves.map(([sym, label, color]) => (
          <div class="d-sl" key={sym}>
            <label>
              <span style={{ color }}>{sym}</span>
              <span class="mono">{Math.round((split[sym] / total) * 100)}%</span>
            </label>
            <input
              type="range"
              min="0"
              max="100"
              step="5"
              value={split[sym]}
              aria-label={`${label} weight`}
              onInput={(e) => setSplit({ ...split, [sym]: Number(e.currentTarget.value) })}
            />
            <small>{label}</small>
          </div>
        ))}
      </div>
      {!isDefault && result ? (
        <div class="d-tuner-delta">
          <span>
            yours: <b class="mono">{money(result.path?.[result.path.length - 1]?.[1] ?? 100)}</b>
          </span>
          <span>
            median yr <b class="mono">{result.median_year >= 0 ? "+" : ""}{result.median_year.toFixed(1)}%</b>
            {defaultMedian != null ? (
              <small> ({result.median_year >= defaultMedian ? "+" : ""}{(result.median_year - defaultMedian).toFixed(1)} vs default)</small>
            ) : null}
          </span>
          <span>
            worst yr <b class="mono value-negative">{result.worst_year.return.toFixed(1)}%</b>
            {defaultWorst != null ? (
              <small> ({defaultWorst !== 0 ? ((result.worst_year.return - defaultWorst)).toFixed(1) : ""} vs default)</small>
            ) : null}
          </span>
          <button
            type="button"
            class="d-reset"
            onClick={() => setSplit({ VTI: 25, GLD: 25, TLT: 25, SGOV: 25 })}
          >
            back to 25×4
          </button>
        </div>
      ) : null}
      {!isDefault && !result && !busy ? (
        <p class="risk-note">This split could not be measured (a sleeve at 0 leaves the window too short).</p>
      ) : null}
    </div>
  );
}

// The journey: monthly path of the mix, growth of $100, log scale, with the
// worst-stretch trough ringed. Pure SVG -- no chart library, no canvas. An
// optional ghost path (the caller's split) rides behind the hero line on the
// same axes -- same window, same math, so the comparison is honest by
// construction.
function HeroChart({
  history,
  ghost,
  ghostLabel,
}: {
  history: PortfolioHistory;
  ghost?: PortfolioHistory | null;
  ghostLabel?: string;
}) {
  const pts = history.path ?? [];
  if (pts.length < 2) return null;
  const W = 1000, H = 340;
  const PAD_L = 0, PAD_R = 0, PAD_T = 14, PAD_B = 26;
  const ghostPts = ghost?.path ?? [];
  const maxV = Math.max(...pts.map(([, v]) => v), ...(ghostPts.length ? ghostPts.map(([, v]) => v) : [0]));
  const logSpan = Math.log(maxV / 100);
  const x = (i: number) => PAD_L + (i / (pts.length - 1)) * (W - PAD_L - PAD_R);
  const y = (v: number) => PAD_T + (1 - Math.max(0, Math.min(1.04, Math.log(v / 100) / logSpan))) * (H - PAD_T - PAD_B);

  // worst drawdown trough
  let peak = pts[0][1], troughI = 0, worstDD = 0;
  pts.forEach(([i, v]) => {
    if (v > peak) peak = v;
    const dd = v / peak - 1;
    if (dd < worstDD) { worstDD = dd; troughI = i; }
  });
  const [ti, tv] = pts[troughI];

  // area fill
  const line = pts.map(([i, v], k) => `${k ? "L" : "M"} ${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L ${x(pts.length - 1).toFixed(1)} ${H - PAD_B} L ${x(0).toFixed(1)} ${H - PAD_B} Z`;

  // the caller's mix, dashed behind the hero line; the ghost shares the
  // window (server-pinned) so index i means the same month on both paths
  const ghostLine = ghostPts.length > 1
    ? ghostPts.map(([i, v], k) => `${k ? "L" : "M"} ${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ")
    : null;
  const ghostLast = ghostPts.length ? ghostPts[ghostPts.length - 1][1] : null;

  // year ticks
  const ticks: number[] = [];
  const perYear = 12;
  for (let i = 0; i < pts.length; i += perYear) ticks.push(i);

  return (
    <div class="d-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="The portfolio's measured monthly path">
        <defs>
          <linearGradient id="dArea" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="rgba(52,211,153,0.20)" />
            <stop offset="100%" stop-color="rgba(52,211,153,0)" />
          </linearGradient>
        </defs>
        {ticks.map((i) => (
          <text class="d-axis" x={x(i)} y={H - 8} text-anchor="start">
            {history.window_start.slice(0, 4) ? String(Number(history.window_start.slice(0, 4)) + Math.floor(i / 12)) : ""}
          </text>
        ))}
        {ghostLine ? (
          <path d={ghostLine} fill="none" stroke="#eef4fb" stroke-width="1.4" stroke-dasharray="5 5" opacity="0.7" />
        ) : null}
        <path d={area} fill="url(#dArea)" />
        <path d={line} fill="none" stroke="#34d399" stroke-width="2.6" stroke-linejoin="round" />
        {ghostLast !== null ? (
          <text class="d-axis" x={W - 4} y={y(ghostLast) - 6} text-anchor="end" fill="#eef4fb" opacity="0.85">
            {ghostLabel ?? "your split"} {((ghostLast / 100) * 10000).toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })}
          </text>
        ) : null}
        <circle cx={x(ti)} cy={y(tv)} r="5.5" fill="none" stroke="#f87171" stroke-width="1.6" stroke-dasharray="3 3" />
        <text class="d-axis" x={Math.min(x(ti) + 8, W - 120)} y={y(tv) + 22} fill="#f87171">
          {history.worst_year.year}: {history.worst_year.return.toFixed(1)}%
        </text>
      </svg>
    </div>
  );
}

// Constraint in, allocation out: the only honest form of "best". Each row is
// the highest-returning mix (1971-2023, annually rebalanced, our own ingested
// series) for a given worst-tolerable-year -- and the whole spectrum costs
// just 2.6%/yr, which is the strongest evidence for the 25x4 default there is.
function DecisionTable({
  rows,
  asOf,
}: {
  rows: NonNullable<ScannerData["decision_table"]>;
  asOf?: string;
}) {
  return (
    <div class="decision-table-wrap">
      <p class="metric-kicker">
        Pick your row · the return you keep for the crash you can sit through
      </p>
      <div class="responsive-table">
        <table class="data-table">
          <thead>
            <tr>
              <th>Worst year you can tolerate</th>
              <th>The highest-returning mix</th>
              <th>Paid</th>
              <th>Worst year</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.tolerate} class={row.tolerate === "-6%" ? "row-default" : undefined}>
                <td><strong>{row.tolerate}</strong>{row.tolerate === "-6%" ? <small class="unit-note"> the default</small> : null}</td>
                <td>{row.allocation}</td>
                <td class="mono value-positive">+{row.cagr_pct.toFixed(1)}%/yr</td>
                <td class="mono value-negative">{row.worst_year_pct.toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p class="risk-note">
        Measured 1971&ndash;2023, annual rebalancing, from the system&apos;s own ingested series
        (Shiller S&amp;P, World Bank gold, FRED rates){asOf ? ` · table as of ${asOf}` : ""}.
        History, not a forecast. The full risk spectrum costs only ~2.6%/yr: the safe rows keep
        most of the money. Tested the other way &mdash; chasing the trailing decade&apos;s winner
        &mdash; won 4 of 8 periods and returned less than holding the split.
      </p>
    </div>
  );
}
// Where the macro readings sit right now -- the same measured indicators the
// Today page reports, beside the four regimes they describe. Descriptive, not
// a forecast: it says what the gauges read, never which regime to bet on.
function CurrentReadings() {
  const [regime, setRegime] = useState<
    | { kind: "loading" }
    | { kind: "ok"; data: RegimeResponse }
    | { kind: "error"; message: string }
  >({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    getRegime()
      .then((data) => { if (!cancelled) setRegime({ kind: "ok", data }); })
      .catch((error) => {
        if (!cancelled) setRegime({ kind: "error", message: describeError(error).message });
      });
    return () => { cancelled = true; };
  }, []);

  if (regime.kind === "loading") return null;
  if (regime.kind === "error") {
    return <p class="quiet-line">Current readings unavailable: {regime.message}</p>;
  }
  const v = regime.data.value;
  if (!v || !v.cycle_phase) {
    return (
      <p class="quiet-line">
        Current readings — the system waits for enough macro data before calling one.
      </p>
    );
  }

  const readings: Array<[string, string, string | null]> = [
    ["Cycle phase", v.cycle_phase, null],
    ["Risk regime", v.risk_regime.replace("_", " "), null],
    ["Inflation", v.inflation_regime, `CPI ${v.inflation_yoy.toFixed(1)}% YoY`],
    [
      "Yield curve",
      v.yield_curve_spread != null ? `${v.yield_curve_spread.toFixed(2)}%` : "—",
      v.yield_curve_inverted ? "inverted" : "normal",
    ],
    ["Recession prob", `${(v.recession_probability * 100).toFixed(0)}%`, v.recession_assessment],
    ["Policy", v.policy_stance, null],
  ];

  return (
    <section class="readings-strip" aria-label="Current macro readings">
      <p class="metric-kicker">
        Current readings · what the gauges say now · descriptive, not a forecast
      </p>
      <div class="readings-row">
        {readings.map(([label, value, sub]) => (
          <span class="reading" key={label}>
            <span class="reading-label">{label}</span>
            <span class="reading-value">{value}</span>
            {sub ? <span class="reading-sub">{sub}</span> : null}
          </span>
        ))}
      </div>
    </section>
  );
}

// The reassurance: four regimes, each with what it protects against, the pick
// already in the portfolio, and the strongest alternatives measured in that
// regime. This is the answer to "am I covered for stagflation?" — visibly.
function ScenarioCards({ data }: { data: ScannerData }) {
  const buckets = REGIME_ORDER
    .map((name) => data.buckets.find((bucket) => bucket.name === name))
    .filter((bucket): bucket is ScenarioBucket => bucket !== undefined);
  return (
    <section class="scenario-grid" aria-label="Covered scenarios">
      {buckets.map((bucket) => {
        const pick = regimePick(bucket);
        const alternatives = bucket.assets
          .filter((asset) => rankableByMedian(asset) && asset.symbol !== pick?.symbol)
          .slice(0, 2);
        return (
          <article class="scenario-card" key={bucket.name}>
            <header>
              <span class="scenario-dot" style={{ background: REGIME_COLORS[bucket.name] }} />
              <div>
                <h3>{REGIME_LABELS[bucket.name] ?? bucket.name}</h3>
                <small>{bucket.role}</small>
              </div>
            </header>
            {pick ? (
              <div class="scenario-pick">
                <span class="eyebrow">In the portfolio</span>
                <strong>{pick.symbol}</strong>
                <small>
                  {bucket.representative?.symbol === pick.symbol
                    ? bucket.representative.reason
                    : pick.name}
                </small>
                <strong class={`top-pick-return ${tone(pick.median_annual_return)}`}>
                  {percent(pick.median_annual_return)} <small class="unit-note">median yr</small>
                </strong>
              </div>
            ) : (
              <p class="top-pick-empty">No measured pick in this regime yet.</p>
            )}
            {alternatives.length > 0 ? (
              <ul class="scenario-alternatives">
                {alternatives.map((asset) => (
                  <li key={asset.symbol}>
                    <span class="top-pick-asset">
                      <strong>{asset.symbol}</strong>
                      <small>{asset.name}</small>
                    </span>
                    <strong class={`top-pick-return ${tone(asset.median_annual_return)}`}>
                      {percent(asset.median_annual_return)} <small class="unit-note">median yr</small>
                    </strong>
                  </li>
                ))}
              </ul>
            ) : null}
          </article>
        );
      })}
    </section>
  );
}

// The risk ladder: the same measured universe, re-cut by how much an asset
// swings. The portfolio above already balances risk across regimes; these
// columns are for choosing a core with more or less of it.
//
// The rank is the median of each asset's complete calendar years over its
// whole measured history -- not a fixed 10-year window, which most of the
// universe does not have. Only assets with at least MIN_COMPLETE_YEARS
// measured years compete, so a young listing cannot win on one lucky year.
function RiskLadder({ data }: { data: ScannerData }) {
  const [openInfo, setOpenInfo] = useState<string | null>(null);

  const universe = (Object.values(data.category_rankings) as AssetMetric[][]).flat();
  const rankable = universe.filter(rankableByMedian);
  const tierTops = TIER_COLUMNS.map((column) => ({
    ...column,
    assets: rankable
      .filter((asset) => asset.risk_tier === column.tier)
      .sort(byMedian)
      .slice(0, TOP_PER_TIER),
  }));

  function toggleInfo(symbol: string) {
    setOpenInfo((current) => (current === symbol ? null : symbol));
  }

  return (
    <section class="builder" aria-label="Risk ladder">
      <div class="top-picks-heading">
        <h2>More risk, or less</h2>
        <p>
          The same universe re-cut by how much an asset swings, ranked by median year
          (3-year minimum). The portfolio above is balanced; a core from a lower column
          means steadier nights, a higher one means bigger swings both ways.
        </p>
      </div>
      <div class="top-picks-grid">
        {tierTops.map((column) => (
          <article class="top-picks-column" key={column.tier} style={{ borderTopColor: column.accent }}>
            <h3>{column.title}</h3>
            <small class="builder-hint">{column.hint}</small>
            <BuilderList
              assets={column.assets}
              openInfo={openInfo}
              onToggleInfo={toggleInfo}
              ranked
            />
          </article>
        ))}
      </div>
    </section>
  );
}

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
            8,
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

  const universe = useMemo(
    () =>
      (Object.values(state.data.category_rankings) as AssetMetric[][]).flat(),
    [state.data],
  );

  return (
    <div class="scanner-view product-page">
      <header class="discover-page-heading">
        <div><h1>Discover</h1><p>The best of everything measured -- what to hold, and why.</p></div>
        <div class="discover-compact-meta">
          <LearnWhy />
          <time dateTime={state.data.as_of}>Updated {new Date(state.data.as_of).toLocaleString()}</time>
        </div>
      </header>

      <ThePortfolio data={state.data} />

      <CustomCompare universe={universe} companies={state.data.comparator_universe ?? []} />

      <BestMeasured data={state.data} />

      <ScenarioCards data={state.data} />

      <CurrentReadings />

      <RiskLadder data={state.data} />

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
            <summary>Method</summary>
            <div class="foot-panel">
              <p>{state.data.ranking_method.balanced} {state.data.ranking_method.scope}</p>
              <p>{state.data.ranking_method.history}</p>
              <p>{state.data.ranking_method.risk_tier}</p>
              <p>Volatility is annualized from daily returns. Market role uses correlation to SPY and is descriptive, not a guaranteed hedge.</p>
            </div>
          </details>
          <details class="foot-details">
            <summary>Coverage · {coverage.complete ? "complete" : "closing"}</summary>
            <div class="foot-panel">
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
