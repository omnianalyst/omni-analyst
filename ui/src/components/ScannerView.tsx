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
  if (a.returns["365d"] !== null) return { value: a.returns["365d"], label: "1yr" };
  if (a.returns["90d"] !== null) return { value: a.returns["90d"], label: "90d" };
  if (a.returns["30d"] !== null) return { value: a.returns["30d"], label: "30d" };
  return { value: null, label: "" };
}

export function ScannerView() {
  const [state, setState] = useState<State>({ kind: "loading" });

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
    for (const a of b.assets) allAssets.set(a.symbol, a);
  }

  const alpha = buckets.find((b) => b.name === "Alpha");
  const risk = state.risk;

  return (
    <div class="scanner-view">
      <header class="page-head">
        <h1>Portfolio</h1>
        <p class="muted">
          Three risk tiers. Top picks in each. One carry book underneath it all.
        </p>
      </header>

      {TIERS.map((tier) => {
        const tierAssets = tier.assets
          .map((sym) => allAssets.get(sym))
          .filter((a): a is AssetMetric => a !== undefined);
        if (tierAssets.length === 0) return null;
        const ranked = [...tierAssets].sort((a, b) => (b.sharpe ?? -99) - (a.sharpe ?? -99));

        return (
          <section key={tier.name} class="tier-section">
            <div class="tier-header">
              <h2 class="tier-name">{tier.name}</h2>
              <span class="tier-subtitle">{tier.subtitle}</span>
            </div>
            <p class="tier-desc">{tier.desc}</p>
            <div class="tile-row">
              {ranked.map((a, i) => (
                <Tile key={a.symbol} asset={a} top={i === 0} />
              ))}
            </div>
          </section>
        );
      })}

      <section class="tier-section tier-alpha">
        <div class="tier-header">
          <h2 class="tier-name">Alpha</h2>
          <span class="tier-subtitle">Market-neutral</span>
        </div>
        <p class="tier-desc">
          The carry book collects funding rates from perpetual futures. It earns
          regardless of what any asset does. Uncorrelated to all three tiers above.
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
