import { useEffect, useState } from "preact/hooks";
import { request, authHeaderIfPresent, describeError } from "../lib/api";
import { Loading } from "./Loading";
import { ErrorState } from "./ErrorState";

interface AssetMetric {
  symbol: string;
  name: string;
  price: number | null;
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

type State =
  | { kind: "loading" }
  | { kind: "ok"; data: ScannerData }
  | { kind: "error"; message: string };

const RISK_LABELS: Record<string, string> = {
  Growth: "Medium risk",
  Debasement: "Medium-high risk",
  Deflation: "Low-medium risk",
  Safety: "Very low risk",
  Alpha: "Market-neutral",
};

const WHY_TEXT: Record<string, string> = {
  Growth: "Broad equity exposure. The most reliable long-term wealth engine when the economy grows.",
  Debasement: "Hard assets that preserve purchasing power when fiat currency loses value.",
  Deflation: "Long-duration government bonds that rally when interest rates fall.",
  Safety: "Short-term Treasuries. Cash-equivalent yield with near-zero risk of loss.",
  Alpha: "The carry book collects funding rates from perpetual futures. Earns regardless of market direction.",
};

function pick(buckets: Bucket[], name: string): AssetMetric | null {
  const b = buckets.find((x) => x.name === name);
  if (!b || b.assets.length === 0) return null;
  const ranked = [...b.assets].sort((a, b) => (b.sharpe ?? -99) - (a.sharpe ?? -99));
  return ranked[0];
}

export function ScannerView() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await request<ScannerData>("/scanner/market", authHeaderIfPresent());
        if (!cancelled) setState({ kind: "ok", data });
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
  const growth = pick(buckets, "Growth");
  const debasement = pick(buckets, "Debasement");
  const deflation = pick(buckets, "Deflation");
  const safety = pick(buckets, "Safety");
  const alpha = buckets.find((x) => x.name === "Alpha");

  return (
    <div class="scanner-view">
      <header class="page-head">
        <h1>Portfolio</h1>
        <p class="muted">
          Five areas of a well-distributed portfolio. Each hedge a different
          economic outcome. One reliable pick per area.
        </p>
      </header>

      <div class="portfolio-grid">
        {growth && <Card bucket="Growth" asset={growth} />}
        {debasement && <Card bucket="Debasement" asset={debasement} />}
        {deflation && <Card bucket="Deflation" asset={deflation} />}
        {safety && <Card bucket="Safety" asset={safety} />}
        {alpha && <AlphaCard bucket={alpha} />}
      </div>

      <p class="muted" style={{ fontSize: "11px", marginTop: "16px" }}>
        Updated {new Date(as_of).toLocaleString()}. Trailing performance from yfinance (display only).
        Carry book APRs from live funding rates.
      </p>
    </div>
  );
}

function Card({ bucket, asset }: { bucket: string; asset: AssetMetric }) {
  const ret90 = asset.returns["90d"];
  const ret1y = asset.returns["365d"];
  const bestReturn = ret1y ?? ret90;

  return (
    <div class={`portfolio-card portfolio-card-${bucket.toLowerCase()}`}>
      <div class="portfolio-card-bucket">{bucket}</div>
      <div class="portfolio-card-risk">{RISK_LABELS[bucket] ?? ""}</div>

      <div class="portfolio-card-symbol">{asset.symbol}</div>
      <div class="portfolio-card-name">{asset.name}</div>

      <div class="portfolio-card-return">
        {bestReturn !== null && bestReturn !== undefined ? (
          <>
            <span class="portfolio-return-value">
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

      <div class="portfolio-card-stats">
        {asset.sharpe !== null && (
          <span>Sharpe {asset.sharpe.toFixed(1)}</span>
        )}
        {asset.max_drawdown !== null && (
          <span class="portfolio-dd">Max DD {asset.max_drawdown.toFixed(0)}%</span>
        )}
        {asset.funding_apr !== null && (
          <span class="portfolio-funding">Funding {asset.funding_apr.toFixed(1)}%/yr</span>
        )}
      </div>

      <p class="portfolio-card-why">{WHY_TEXT[bucket]}</p>
    </div>
  );
}

function AlphaCard({ bucket }: { bucket: Bucket }) {
  const pairs = bucket.assets;
  if (pairs.length === 0) {
    return (
      <div class="portfolio-card portfolio-card-alpha">
        <div class="portfolio-card-bucket">Alpha</div>
        <div class="portfolio-card-risk">{RISK_LABELS["Alpha"]}</div>
        <div class="portfolio-card-symbol">Carry Book</div>
        <div class="portfolio-card-name">No active pairs</div>
        <p class="portfolio-card-why">{WHY_TEXT["Alpha"]}</p>
      </div>
    );
  }

  const topPairs = pairs.slice(0, 3).map((p) => `${p.symbol} ${p.funding_apr?.toFixed(1)}%`).join(" · ");

  return (
    <div class="portfolio-card portfolio-card-alpha">
      <div class="portfolio-card-bucket">Alpha</div>
      <div class="portfolio-card-risk">{RISK_LABELS["Alpha"]}</div>

      <div class="portfolio-card-symbol">Carry Book</div>
      <div class="portfolio-card-name">{topPairs}</div>

      <div class="portfolio-card-return">
        <span class="portfolio-return-value">~11%/yr</span>
        <span class="portfolio-return-label">on notional</span>
      </div>

      <div class="portfolio-card-stats">
        <span>Live</span>
        <span>t = 36.0</span>
      </div>

      <p class="portfolio-card-why">{WHY_TEXT["Alpha"]}</p>
    </div>
  );
}
