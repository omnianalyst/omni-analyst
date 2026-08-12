import { useEffect, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError, request } from "../lib/api";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

interface AssetMetric {
  symbol: string;
  name?: string;
  returns?: {
    "7d"?: number | null;
    "30d"?: number | null;
    "90d"?: number | null;
    "365d"?: number | null;
  };
  sharpe?: number | null;
  volatility?: number | null;
  max_drawdown?: number | null;
  funding_apr?: number | null;
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
  | { kind: "error"; message: string; detail?: string };

function percent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;
}

function tone(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return value > 0 ? "value-positive" : value < 0 ? "value-negative" : "";
}

export function ScannerView() {
  const [state, setState] = useState<State>({ kind: "loading" });

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

  const populated = state.data.buckets.filter((bucket) => bucket.assets.length > 0);
  const assetCount = populated.reduce((total, bucket) => total + bucket.assets.length, 0);

  return (
    <div class="scanner-view product-page">
      <header class="discover-intro">
        <div>
          <p class="eyebrow">Discover</p>
          <h1>Understand the market without the noise</h1>
          <p>
            A compact view of the assets Omni is tracking. These are measurements,
            not recommendations.
          </p>
        </div>
        <div class="discover-meta">
          <strong>{assetCount}</strong>
          <span>assets measured</span>
          <small>Updated {new Date(state.data.as_of).toLocaleString()}</small>
        </div>
      </header>

      {populated.length === 0 ? (
        <section class="quiet-state compact">
          <h2>No market measurements are available</h2>
          <p>The scanner returned no assets, so there is nothing to rank or infer.</p>
        </section>
      ) : (
        <div class="market-lenses">
          {populated.map((bucket) => (
            <section class="market-lens" key={bucket.name}>
              <div class="market-lens-heading">
                <div>
                  <h2>{bucket.name}</h2>
                  <p>{bucket.role}</p>
                </div>
                <span class="count-badge">{bucket.assets.length}</span>
              </div>
              <div class="market-assets">
                {bucket.assets.map((asset) => (
                  <article class="market-asset" key={asset.symbol}>
                    <div class="market-asset-name">
                      <strong>{asset.symbol}</strong>
                      <span>{asset.name || bucket.name}</span>
                    </div>
                    <div class="market-measure">
                      <span>30 days</span>
                      <strong class={tone(asset.returns?.["30d"])}>
                        {percent(asset.returns?.["30d"])}
                      </strong>
                    </div>
                    <div class="market-measure">
                      <span>1 year</span>
                      <strong class={tone(asset.returns?.["365d"])}>
                        {percent(asset.returns?.["365d"])}
                      </strong>
                    </div>
                    <div class="market-measure market-measure-secondary">
                      <span>Volatility</span>
                      <strong>{percent(asset.volatility)}</strong>
                    </div>
                    {asset.funding_apr !== null && asset.funding_apr !== undefined ? (
                      <div class="market-measure market-measure-secondary">
                        <span>Funding APR</span>
                        <strong>{percent(asset.funding_apr)}</strong>
                      </div>
                    ) : null}
                  </article>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
