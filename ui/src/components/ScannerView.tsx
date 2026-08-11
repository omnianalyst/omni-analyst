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
  detail?: string;
}

type State =
  | { kind: "loading" }
  | { kind: "ok"; data: ScannerData; risk: RiskData | null }
  | { kind: "error"; message: string };

const PICKS: Record<string, { symbol: string; reason: string }> = {
  Growth: {
    symbol: "VTI",
    reason: "Broad US market. The most reliable long-term growth engine — the S&P 500 equivalent. ~10%/yr over decades through every cycle.",
  },
  Debasement: {
    symbol: "GLD",
    reason: "Gold preserves purchasing power when fiat devalues. BTC adds asymmetric upside; both hedge the inflation/stagflation quadrant.",
  },
  Deflation: {
    symbol: "TLT",
    reason: "Long-duration Treasuries rally when rates fall. The only asset that wins in a deflationary downturn.",
  },
  Safety: {
    symbol: "SHV",
    reason: "Short-term T-bills. Cash-equivalent yield with near-zero risk. Capital preservation when nothing else works.",
  },
};

const VERDICT_LABELS: Record<string, { label: string; color: string }> = {
  delta_neutral: { label: "Delta-neutral", color: "var(--green, #4ade80)" },
  slight_drift: { label: "Slight drift", color: "var(--yellow, #fbbf24)" },
  factor_exposed: { label: "Factor exposed", color: "var(--red, #f87171)" },
  flat: { label: "Flat", color: "var(--muted)" },
  insufficient_data: { label: "Insufficient data", color: "var(--muted)" },
  no_portfolio: { label: "No portfolio", color: "var(--muted)" },
};

function pickAsset(buckets: Bucket[], bucketName: string): AssetMetric | null {
  const preferred = PICKS[bucketName];
  if (!preferred) return null;
  const bucket = buckets.find((b) => b.name === bucketName);
  if (!bucket) return null;
  return bucket.assets.find((a) => a.symbol === preferred.symbol) ?? bucket.assets[0] ?? null;
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
  const growth = pickAsset(buckets, "Growth");
  const debasement = pickAsset(buckets, "Debasement");
  const deflation = pickAsset(buckets, "Deflation");
  const safety = pickAsset(buckets, "Safety");
  const alpha = buckets.find((x) => x.name === "Alpha");
  const risk = state.risk;

  return (
    <div class="scanner-view">
      <header class="page-head">
        <h1>Portfolio</h1>
        <p class="muted">
          Five areas. One reliable pick each. Backed by long-term data, not
          short-term speculation.
        </p>
      </header>

      <div class="portfolio-grid">
        {growth && <Card bucket="Growth" asset={growth} />}
        {debasement && <Card bucket="Debasement" asset={debasement} />}
        {deflation && <Card bucket="Deflation" asset={deflation} />}
        {safety && <Card bucket="Safety" asset={safety} />}
        {alpha && <AlphaCard bucket={alpha} risk={risk} />}
      </div>

      <p class="muted" style={{ fontSize: "11px", marginTop: "16px" }}>
        Updated {new Date(as_of).toLocaleString()}. Picks are structural, not
        trading signals.
      </p>
    </div>
  );
}

function Card({ bucket, asset }: { bucket: string; asset: AssetMetric }) {
  const ret90 = asset.returns["90d"];
  const ret1y = asset.returns["365d"];
  const bestReturn = ret1y ?? ret90;
  const pick = PICKS[bucket];

  return (
    <div class="portfolio-card">
      <div class="portfolio-card-bucket">{bucket}</div>

      <div class="portfolio-card-symbol">{asset.symbol}</div>
      <div class="portfolio-card-name">{asset.name}</div>

      <div class="portfolio-card-return">
        {bestReturn !== null && bestReturn !== undefined ? (
          <>
            <span
              class="portfolio-return-value"
              style={{ color: bestReturn >= 0 ? "var(--green, #4ade80)" : "var(--red, #f87171)" }}
            >
              {bestReturn > 0 ? "+" : ""}{bestReturn.toFixed(1)}%
            </span>
            <span class="portfolio-return-label">
              {ret1y !== null && ret1y !== undefined ? "past year" : "past 90 days"}
            </span>
          </>
        ) : (
          <span class="muted">--</span>
        )}
      </div>

      {pick && <p class="portfolio-card-why">{pick.reason}</p>}
    </div>
  );
}

function AlphaCard({ bucket, risk }: { bucket: Bucket; risk: RiskData | null }) {
  const pairs = bucket.assets;
  const verdict = risk?.verdict ?? "unknown";
  const vLabel = VERDICT_LABELS[verdict] ?? { label: verdict, color: "var(--muted)" };

  return (
    <div class="portfolio-card portfolio-card-alpha">
      <div class="portfolio-card-bucket">Alpha</div>

      <div class="portfolio-card-symbol">Carry Book</div>
      <div class="portfolio-card-name">
        {pairs.length > 0
          ? pairs.slice(0, 3).map((p) => p.symbol).join(" + ")
          : "No active pairs"}
      </div>

      <div class="portfolio-card-return">
        <span class="portfolio-return-value">~11%/yr</span>
        <span class="portfolio-return-label">on notional</span>
      </div>

      <div class="portfolio-risk-badge" style={{ color: vLabel.color }}>
        <span class="portfolio-risk-dot" style={{ background: vLabel.color }} />
        {vLabel.label}
      </div>

      {risk?.pc1_label && (
        <div class="muted" style={{ fontSize: "11px", marginTop: "4px" }}>
          {risk.pc1_label}
          {risk.net_ratio !== undefined && ` · net ratio ${(risk.net_ratio * 100).toFixed(1)}%`}
        </div>
      )}

      <p class="portfolio-card-why">
        Collects funding rates from perpetual futures. Earns regardless of
        market direction. PCA confirms the book is {vLabel.label.toLowerCase()}.
      </p>
    </div>
  );
}
