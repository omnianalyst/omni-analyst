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

const SECTOR_INFO: Record<string, { risk: string; why: string }> = {
  Growth: {
    risk: "Medium risk",
    why: "Equities compound wealth when the economy grows. The reliable long-term engine.",
  },
  Debasement: {
    risk: "Medium-high risk",
    why: "Hard assets that hold value when fiat devalues. Gold for stability, BTC for upside.",
  },
  Deflation: {
    risk: "Low-medium risk",
    why: "Long bonds rally when rates fall. The hedge against economic contraction.",
  },
  Safety: {
    risk: "Very low risk",
    why: "T-bills preserve capital and pay yield. Cash equivalent, near-zero drawdown.",
  },
};

const VERDICT_COLORS: Record<string, string> = {
  delta_neutral: "var(--green, #4ade80)",
  slight_drift: "var(--yellow, #fbbf24)",
  factor_exposed: "var(--red, #f87171)",
  flat: "var(--muted)",
  insufficient_data: "var(--muted)",
  no_portfolio: "var(--muted)",
};

const VERDICT_LABELS: Record<string, string> = {
  delta_neutral: "Delta-neutral",
  slight_drift: "Slight drift",
  factor_exposed: "Factor exposed",
  flat: "Flat",
  insufficient_data: "Insufficient data",
  no_portfolio: "No portfolio",
};

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
  const risk = state.risk;

  return (
    <div class="scanner-view">
      <header class="page-head">
        <h1>Portfolio</h1>
        <p class="muted">
          Five sectors. Top picks in each. One holistic view.
        </p>
      </header>

      {Object.entries(SECTOR_INFO).map(([name, info]) => {
        const bucket = buckets.find((b) => b.name === name);
        if (!bucket || bucket.assets.length === 0) return null;
        const ranked = [...bucket.assets].sort(
          (a, b) => (b.sharpe ?? -99) - (a.sharpe ?? -99),
        );
        return (
          <section key={name} class="sector-section">
            <div class="sector-header">
              <h2 class="sector-name">{name}</h2>
              <span class="sector-risk">{info.risk}</span>
            </div>
            <p class="sector-why">{info.why}</p>
            <div class="tile-row">
              {ranked.map((a, i) => (
                <AssetTile key={a.symbol} asset={a} top={i === 0} />
              ))}
            </div>
          </section>
        );
      })}

      {(() => {
        const alpha = buckets.find((b) => b.name === "Alpha");
        if (!alpha) return null;
        return (
          <section class="sector-section sector-alpha">
            <div class="sector-header">
              <h2 class="sector-name">Alpha</h2>
              <span class="sector-risk">Market-neutral</span>
            </div>
            <p class="sector-why">
              Carry book harvests funding rates. Earns regardless of direction.
            </p>
            <div class="tile-row">
              <AlphaTile bucket={alpha} risk={risk} />
            </div>
          </section>
        );
      })()}

      <p class="muted scanner-updated">
        Updated {new Date(as_of).toLocaleString()}
      </p>
    </div>
  );
}

function AssetTile({ asset, top }: { asset: AssetMetric; top: boolean }) {
  const ret = bestReturn(asset);
  return (
    <div class={`asset-tile ${top ? "asset-tile-top" : ""}`}>
      {top && <div class="asset-tile-badge">Best</div>}
      <div class="asset-tile-symbol">{asset.symbol}</div>
      <div class="asset-tile-name">{asset.name}</div>
      <div class="asset-tile-return" style={{ color: retColor(ret.value) }}>
        {fmtRet(ret.value)}
      </div>
      <div class="asset-tile-period">{ret.label}</div>
      <div class="asset-tile-sharpe">
        {asset.sharpe !== null ? `Sharpe ${asset.sharpe.toFixed(1)}` : ""}
      </div>
      {asset.funding_apr !== null && (
        <div class="asset-tile-funding">{asset.funding_apr.toFixed(1)}%/yr</div>
      )}
    </div>
  );
}

function AlphaTile({ bucket, risk }: { bucket: Bucket; risk: RiskData | null }) {
  const verdict = risk?.verdict ?? "unknown";
  const color = VERDICT_COLORS[verdict] ?? "var(--muted)";
  const label = VERDICT_LABELS[verdict] ?? verdict;
  const pairs = bucket.assets;

  return (
    <div class="asset-tile asset-tile-top asset-tile-alpha">
      <div class="asset-tile-badge">Live</div>
      <div class="asset-tile-symbol">Carry Book</div>
      <div class="asset-tile-name">
        {pairs.length > 0
          ? pairs.map((p) => p.symbol).join(" + ")
          : "No active pairs"}
      </div>
      <div class="asset-tile-return" style={{ color: "var(--green, #4ade80)" }}>
        ~11%/yr
      </div>
      <div class="asset-tile-period">on notional</div>
      <div class="asset-tile-sharpe">t = 36.0</div>
      <div class="asset-tile-status">
        <span class="status-dot" style={{ background: color }} />
        {label}
      </div>
    </div>
  );
}
