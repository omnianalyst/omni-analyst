import { useEffect, useState } from "preact/hooks";
import { request, authHeaderIfPresent, describeError } from "../lib/api";
import { Loading } from "./Loading";
import { ErrorState } from "./ErrorState";

interface AssetMetric {
  symbol: string;
  name: string;
  returns: { "7d": number | null; "30d": number | null; "90d": number | null; "365d": number | null };
  sharpe: number | null;
  volatility: number | null;
  max_drawdown: number | null;
  funding_apr: number | null;
}

interface Bucket {
  name: string;
  role: string;
  assets: AssetMetric[];
}

interface ScannerData {
  buckets: Bucket[];
  as_of: string;
}

interface RiskData {
  verdict: string;
  net_ratio?: number;
  pc1_share?: number;
  pc1_label?: string;
}

type State =
  | { kind: "loading" }
  | { kind: "ok"; data: ScannerData; risk: RiskData | null }
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

type ViewMode = "tiers" | "sectors";

const SECTORS = [
  {
    name: "Growth",
    risk: "Medium risk",
    desc: "Broad equity exposure. The most reliable long-term wealth engine.",
    assets: ["VTI", "QQQ", "VXUS"],
  },
  {
    name: "Debasement",
    risk: "Medium-high risk",
    desc: "Hard assets that preserve value when fiat devalues. Gold for stability, BTC for upside.",
    assets: ["GLD", "SLV", "BTC", "ETH", "SOL"],
  },
  {
    name: "Deflation",
    risk: "Low-medium risk",
    desc: "Long bonds rally when rates fall.",
    assets: ["TLT"],
  },
  {
    name: "Safety",
    risk: "Very low risk",
    desc: "T-bills preserve capital with near-zero risk.",
    assets: ["SHV"],
  },
] as const;

const TIERS = [
  {
    name: "Low Risk",
    subtitle: "Preserve capital",
    desc: "Bonds and cash equivalents. Near-zero drawdown. The floor under everything else.",
    assets: ["SHV", "TLT"],
  },
  {
    name: "Medium Risk",
    subtitle: "Steady growth",
    desc: "Broad equities and precious metals. The reliable long-term wealth engine.",
    assets: ["VTI", "QQQ", "VXUS", "GLD", "SLV"],
  },
  {
    name: "High Risk",
    subtitle: "Maximum upside",
    desc: "Crypto. High volatility, asymmetric returns. Size positions accordingly.",
    assets: ["BTC", "ETH", "SOL"],
  },
] as const;

function fmtRet(n: number | null): string {
  if (n === null) return "--";
  return `${n > 0 ? "+" : ""}${n.toFixed(1)}%`;
}
function retColor(n: number | null): string {
  if (n === null) return "var(--muted)";
  return n >= 0 ? "var(--green, #4ade80)" : "var(--red, #f87171)";
}
function bestReturn(a: AssetMetric): { value: number | null; label: string } {
  if (!a.returns) return { value: null, label: "" };
  if (a.returns["365d"] !== null && a.returns["365d"] !== undefined) return { value: a.returns["365d"], label: "1yr" };
  if (a.returns["90d"] !== null && a.returns["90d"] !== undefined) return { value: a.returns["90d"], label: "90d" };
  if (a.returns["30d"] !== null && a.returns["30d"] !== undefined) return { value: a.returns["30d"], label: "30d" };
  return { value: null, label: "" };
}

export function ScannerView() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [view, setView] = useState<ViewMode>("tiers");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [data, risk] = await Promise.all([
          request<ScannerData>("/scanner/market", authHeaderIfPresent()),
          request<RiskData>("/scanner/risk", authHeaderIfPresent()).catch(() => null),
        ]);
        if (!cancelled) setState({ kind: "ok", data, risk });
      } catch (err) {
        if (!cancelled) {
          const { message } = describeError(err);
          setState({ kind: "error", message });
        }
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (state.kind === "loading") return <Loading label="Loading portfolio..." />;
  if (state.kind === "error") return <ErrorState message={state.message} />;

  const { buckets, as_of } = state.data;
  const allAssets: Map<string, AssetMetric> = new Map();
  for (const b of buckets) {
    if (b.name === "Alpha") continue;
    for (const a of b.assets) allAssets.set(a.symbol, a);
  }

  const alpha = buckets.find((b) => b.name === "Alpha");
  const risk = state.risk;

  return (
    <div class="scanner-view">
      <header class="page-head">
        <h1>Portfolio</h1>
        <p class="muted">
          Top picks across every asset class. Backed by long-term data.
        </p>
      </header>

      <div class="view-toggle">
        <button
          class={`toggle-btn ${view === "tiers" ? "toggle-active" : ""}`}
          onClick={() => setView("tiers")}
        >
          Risk Tiers
        </button>
        <button
          class={`toggle-btn ${view === "sectors" ? "toggle-active" : ""}`}
          onClick={() => setView("sectors")}
        >
          Sectors
        </button>
      </div>

      {(() => {
        const groups = view === "tiers" ? TIERS : SECTORS;
        return groups.map((group) => {
          const groupAssets = group.assets
            .map((sym) => allAssets.get(sym))
            .filter((a): a is AssetMetric => a !== undefined);
          if (groupAssets.length === 0) return null;
          const ranked = [...groupAssets].sort((a, b) => (b.sharpe ?? -99) - (a.sharpe ?? -99));
          const subtitle = "subtitle" in group ? group.subtitle : "risk" in group ? group.risk : "";

          return (
            <section key={group.name} class="tier-section">
              <div class="tier-header">
                <h2 class="tier-name">{group.name}</h2>
                <span class="tier-subtitle">{subtitle}</span>
              </div>
              <p class="tier-desc">{group.desc}</p>
              <div class="tile-row">
                {ranked.map((a, i) => (
                  <Tile key={a.symbol} asset={a} top={i === 0} />
                ))}
              </div>
            </section>
          );
        });
      })()}

      <section class="tier-section tier-alpha">
        <div class="tier-header">
          <h2 class="tier-name">Alpha</h2>
          <span class="tier-subtitle">Market-neutral</span>
        </div>
        <p class="tier-desc">
          The carry book collects funding rates from perpetual futures. It earns
          regardless of what any asset does. Uncorrelated to everything above.
        </p>
        <div class="tile-row">
          <AlphaTile bucket={alpha} risk={risk} />
        </div>
      </section>

      <p class="muted scanner-updated">
        Updated {new Date(as_of).toLocaleString()}
      </p>
    </div>
  );
}

function Tile({ asset, top }: { asset: AssetMetric; top: boolean }) {
  const ret = bestReturn(asset);
  return (
    <div class={`tile ${top ? "tile-top" : ""}`}>
      {top && <span class="tile-badge">Best</span>}
      <div class="tile-symbol">{asset.symbol}</div>
      <div class="tile-name">{asset.name}</div>
      <div class="tile-return" style={{ color: retColor(ret.value) }}>
        {fmtRet(ret.value)}
      </div>
      <div class="tile-period">{ret.label}</div>
      <div class="tile-sharpe">
        {asset.sharpe !== null ? `Sharpe ${asset.sharpe.toFixed(1)}` : ""}
      </div>
      {asset.funding_apr !== null && (
        <div class="tile-funding">{asset.funding_apr.toFixed(1)}%/yr funding</div>
      )}
    </div>
  );
}

function AlphaTile({ bucket, risk }: { bucket: Bucket | undefined; risk: RiskData | null }) {
  const verdict = risk?.verdict ?? "unknown";
  const color = VERDICT_COLORS[verdict] ?? "var(--muted)";
  const label = VERDICT_LABELS[verdict] ?? verdict;
  const pairs = bucket?.assets ?? [];

  return (
    <div class="tile tile-top tile-alpha">
      <span class="tile-badge">Live</span>
      <div class="tile-symbol">Carry Book</div>
      <div class="tile-name">
        {pairs.length > 0 ? pairs.map((p) => p.symbol).join(" + ") : "No active pairs"}
      </div>
      <div class="tile-return" style={{ color: "var(--green, #4ade80)" }}>~11%/yr</div>
      <div class="tile-period">on notional</div>
      <div class="tile-sharpe">t = 36.0</div>
      <div class="tile-status">
        <span class="status-dot" style={{ background: color }} />
        {label}
      </div>
    </div>
  );
}
