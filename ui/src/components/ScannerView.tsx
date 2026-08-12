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
  sectors: SectorLeaders[];
  sector_coverage: {
    available: number;
    total: number;
    window_sessions: number;
  };
  as_of: string;
}

interface SectorLeader {
  symbol: string;
  name: string;
  return_30d: number;
  as_of: string;
}

interface SectorLeaders {
  name: string;
  symbol: string;
  coverage: number;
  leaders: SectorLeader[];
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
  const companyCount = state.data.sectors.reduce(
    (total, sector) => total + sector.coverage,
    0,
  );

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
          <strong>{companyCount || assetCount}</strong>
          <span>{companyCount ? "companies compared" : "assets measured"}</span>
          {companyCount ? <span>{assetCount} broad assets in context</span> : null}
          <small>Updated {new Date(state.data.as_of).toLocaleString()}</small>
        </div>
      </header>

      {populated.length === 0 && state.data.sectors.length === 0 ? (
        <section class="quiet-state compact">
          <h2>No market measurements are available</h2>
          <p>The scanner returned no assets, so there is nothing to rank or infer.</p>
        </section>
      ) : (
        <>
          <section class="sector-leaders-section">
            <div class="section-heading-row">
              <div>
                <p class="eyebrow">Leadership</p>
                <h2>Top performers in each sector</h2>
                <p>
                  Ranked by {state.data.sector_coverage.window_sessions}-session return
                  across the price histories available to you.
                </p>
              </div>
              <span class="coverage-note">
                {state.data.sector_coverage.available} of {state.data.sector_coverage.total} sectors measured
              </span>
            </div>

            {state.data.sectors.length === 0 ? (
              <div class="quiet-state compact">
                <h3>Sector leadership is still building</h3>
                <p>
                  No company has enough visible price history for a measured
                  {" "}{state.data.sector_coverage.window_sessions}-session comparison yet.
                </p>
              </div>
            ) : (
              <div class="sector-leader-grid">
                {state.data.sectors.map((sector) => (
                  <article class="sector-leader-card" key={sector.symbol}>
                    <header>
                      <div>
                        <span>{sector.symbol}</span>
                        <h3>{sector.name}</h3>
                      </div>
                      <small>{sector.coverage} compared</small>
                    </header>
                    <ol>
                      {sector.leaders.map((leader, index) => (
                        <li key={leader.symbol}>
                          <span class="leader-rank">{index + 1}</span>
                          <span class="leader-company">
                            <strong>{leader.symbol}</strong>
                            <small>{leader.name}</small>
                          </span>
                          <strong class={tone(leader.return_30d)}>
                            {percent(leader.return_30d)}
                          </strong>
                        </li>
                      ))}
                    </ol>
                  </article>
                ))}
              </div>
            )}
          </section>

          {populated.length > 0 ? (
            <section class="market-context-section">
              <div class="section-heading-row">
                <div>
                  <p class="eyebrow">Context</p>
                  <h2>How major assets are behaving</h2>
                  <p>Broad regime lenses for comparing growth, protection, and carry.</p>
                </div>
              </div>
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
            </section>
          ) : null}
        </>
      )}
    </div>
  );
}
