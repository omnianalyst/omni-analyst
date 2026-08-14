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

// The market angles a balanced portfolio holds alongside growth: assets whose
// returns run against stocks, and assets whose returns ignore them. They are
// how the page answers "what protects this when the market turns".
const HEDGE_ROLES: Array<{
  behavior: "counterweight" | "diversifier";
  title: string;
  hint: string;
}> = [
  { behavior: "counterweight", title: "Counterweights", hint: "correlation to stocks \u2264 \u22120.15" },
  { behavior: "diversifier", title: "Diversifiers", hint: "correlation to stocks < 0.35" },
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
        <div><dt>Correlation to stocks</dt><dd>{asset.correlation_to_spy?.toFixed(2) ?? "—"}</dd></div>
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

// The blend: equal weight across the leader of each tier plus the two market
// angles -- five positions, 20% each. Rendered as one stacked bar where each
// segment is that pick's contribution to the blend's historical median. The
// segments describe history; they are not a forecast or advice.
function BlendVisual({ picks }: { picks: Array<{ asset: AssetMetric; label: string; color: string }> }) {
  const weight = 1 / picks.length;
  const contributions = picks.map((pick) => ({
    ...pick,
    contribution: (pick.asset.median_annual_return ?? 0) * weight * 100,
  }));
  const total = contributions.reduce((sum, entry) => sum + entry.contribution, 0);
  const positiveTotal = contributions.reduce(
    (sum, entry) => sum + Math.max(entry.contribution, 0),
    0,
  );
  return (
    <div class="blend">
      <div class="blend-bar" role="img" aria-label={`Equal-weight blend of ${picks.map((p) => p.asset.symbol).join(", ")}`}>
        {contributions.map((entry) => (
          entry.contribution > 0 ? (
            <span
              key={entry.asset.symbol}
              class="blend-segment"
              style={{
                width: `${Math.max((entry.contribution / positiveTotal) * 100, 3)}%`,
                background: entry.color,
              }}
              title={`${entry.asset.symbol} · ${entry.label} · ${(weight * 100).toFixed(0)}% weight`}
            />
          ) : null
        ))}
      </div>
      <div class="blend-total">
        <span class="metric-kicker">Blend median year</span>
        <strong class={total > 0 ? "value-positive" : "value-negative"}>
          {total > 0 ? "+" : ""}{total.toFixed(1)}%
        </strong>
        <span class="metric-context">
          equal weight ({(weight * 100).toFixed(0)}% each) of each pick&apos;s historical median
        </span>
      </div>
      <ol class="blend-legend">
        {contributions.map((entry) => (
          <li key={entry.asset.symbol}>
            <span class="blend-swatch" style={{ background: entry.color }} />
            <strong>{entry.asset.symbol}</strong>
            <span class="muted">{entry.label}</span>
            <span class={`mono ${tone(entry.contribution)}`}>
              {entry.contribution > 0 ? "+" : ""}{entry.contribution.toFixed(1)}%
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

// The front door of Discover: a portfolio in one glance. One pick from each
// volatility column is a core; the angles below are the balance against
// market conditions. Everything after this section is the supporting depth.
//
// The rank is the median of each asset's complete calendar years over its
// whole measured history -- not a fixed 10-year window, which most of the
// universe does not have. Only assets with at least MIN_COMPLETE_YEARS
// measured years compete, so a young listing cannot win on one lucky year.
function PortfolioBuilder({ data }: { data: ScannerData }) {
  const [openInfo, setOpenInfo] = useState<string | null>(null);
  const [blendOpen, setBlendOpen] = useState(false);

  const universe = (Object.values(data.category_rankings) as AssetMetric[][]).flat();
  const rankable = universe.filter(rankableByMedian);
  const tierTops = TIER_COLUMNS.map((column) => ({
    ...column,
    assets: rankable
      .filter((asset) => asset.risk_tier === column.tier)
      .sort(byMedian)
      .slice(0, TOP_PER_TIER),
  }));
  const hedges = HEDGE_ROLES.map((role) => ({
    ...role,
    assets: universe
      .filter((asset) => asset.market_behavior === role.behavior && rankableByMedian(asset))
      .sort(byMedian)
      .slice(0, 2),
  }));

  function toggleInfo(symbol: string) {
    setOpenInfo((current) => (current === symbol ? null : symbol));
  }

  // The blend takes the leader of each tier and the leader of each angle --
  // every role a balanced portfolio wants, at equal weight.
  const blendPicks = [
    ...tierTops.map((tier) =>
      tier.assets.length > 0
        ? { asset: tier.assets[0], label: tier.title, color: tier.accent }
        : null,
    ),
    ...hedges.map((hedge) =>
      hedge.assets.length > 0
        ? { asset: hedge.assets[0], label: hedge.title, color: "var(--border-strong)" }
        : null,
    ),
  ].filter((pick): pick is { asset: AssetMetric; label: string; color: string } => pick !== null);

  return (
    <section class="builder" aria-label="Build a balanced portfolio">
      <div class="top-picks-heading">
        <h2>Build a balanced portfolio</h2>
        <p>
          Median annual return over each asset&apos;s complete measured history (3-year minimum).
          One core from each column; the angles below balance against the market.
        </p>
      </div>

      <button
        type="button"
        class={`blend-toggle ${blendOpen ? "active" : ""}`}
        aria-expanded={blendOpen}
        onClick={() => setBlendOpen((open) => !open)}
      >
        <span class="toggle-track" aria-hidden="true"><span /></span>
        {blendOpen ? "Hide the balanced blend" : "Show the balanced blend"}
      </button>
      {blendOpen && blendPicks.length > 0 ? <BlendVisual picks={blendPicks} /> : null}

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
      <div class="builder-angles">
        {hedges.map((angle) => (
          <article class="builder-angle" key={angle.behavior}>
            <h3>{angle.title}</h3>
            <small class="builder-hint">{angle.hint}</small>
            <BuilderList
              assets={angle.assets}
              openInfo={openInfo}
              onToggleInfo={toggleInfo}
              ranked={false}
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
}: {
  title: string;
  description: string;
  assets: AssetMetric[];
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
              <th>Median year, all measured</th>
              <th>Volatility</th>
              <th>Market role</th>
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
                <td class={tone(asset.median_annual_return)}>{percent(asset.median_annual_return)}</td>
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
        <div><h1>Discover</h1><p>Everything measured, funnelled to the picks that matter.</p></div>
        <div class="discover-compact-meta">
          <time dateTime={state.data.as_of}>Updated {new Date(state.data.as_of).toLocaleString()}</time>
        </div>
      </header>

      <PortfolioBuilder data={state.data} />

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
