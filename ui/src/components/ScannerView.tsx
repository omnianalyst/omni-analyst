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

function fmt(n: number | null | undefined, suffix = "%"): string {
  if (n === null || n === undefined) return "--";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(1)}${suffix}`;
}

function fmtColor(n: number | null | undefined): string {
  if (n === null || n === undefined) return "var(--muted)";
  return n > 0 ? "var(--green, #4ade80)" : n < 0 ? "var(--red, #f87171)" : "var(--text)";
}

export function ScannerView() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await request<ScannerData>("/scanner/market", authHeaderIfPresent());
        if (!cancelled) setState({ kind: "ok", data });
      } catch (err) {
        if (!cancelled) {
          const { message } = describeError(err);
          setState({ kind: "error", message });
        }
      }
    };
    load();
    return () => { cancelled = true; };
  }, []);

  if (state.kind === "loading") return <Loading label="Scanning markets..." />;
  if (state.kind === "error") return <ErrorState message={state.message} />;

  const { buckets, as_of } = state.data;

  return (
    <div class="scanner-view">
      <header class="page-head">
        <h1>Scanner</h1>
        <p class="muted">
          Cross-asset ranking across five regime buckets. Each bucket hedges a
          different economic outcome. Updated hourly.
        </p>
        <p class="muted" style={{ fontSize: "11px", marginTop: "4px" }}>
          As of {new Date(as_of).toLocaleString()}
        </p>
      </header>

      <div class="scanner-buckets">
        {buckets.map((bucket) => (
          <BucketSection key={bucket.name} bucket={bucket} />
        ))}
      </div>
    </div>
  );
}

function BucketSection({ bucket }: { bucket: Bucket }) {
  const isAlpha = bucket.name === "Alpha";

  return (
    <section class="panel scanner-bucket">
      <div class="scanner-bucket-header">
        <h2 class="panel-title">{bucket.name}</h2>
        <span class="muted scanner-bucket-role">{bucket.role}</span>
      </div>

      {bucket.assets.length === 0 ? (
        <p class="muted">No data available.</p>
      ) : isAlpha ? (
        <div class="scanner-alpha">
          {bucket.assets.map((a) => (
            <div key={a.symbol} class="scanner-alpha-row">
              <span class="mono scanner-symbol">{a.symbol}</span>
              <span class="mono scanner-funding">
                {a.funding_apr !== null ? `${a.funding_apr.toFixed(1)}%/yr` : "--"}
              </span>
              <span class="muted">funding carry</span>
            </div>
          ))}
        </div>
      ) : (
        <table class="data-table scanner-table">
          <thead>
            <tr>
              <th>Asset</th>
              <th class="num">7d</th>
              <th class="num">30d</th>
              <th class="num">90d</th>
              <th class="num">1yr</th>
              <th class="num">Sharpe</th>
              <th class="num">Vol</th>
              <th class="num">MaxDD</th>
              {bucket.assets.some((a) => a.funding_apr !== null) && (
                <th class="num">Funding</th>
              )}
            </tr>
          </thead>
          <tbody>
            {bucket.assets.map((a, i) => (
              <tr key={a.symbol} class={i === 0 ? "scanner-top" : ""}>
                <td>
                  <span class="mono scanner-symbol">{a.symbol}</span>
                  <span class="muted scanner-name">{a.name}</span>
                </td>
                <td class="num mono" style={{ color: fmtColor(a.returns["7d"]) }}>
                  {fmt(a.returns["7d"])}
                </td>
                <td class="num mono" style={{ color: fmtColor(a.returns["30d"]) }}>
                  {fmt(a.returns["30d"])}
                </td>
                <td class="num mono" style={{ color: fmtColor(a.returns["90d"]) }}>
                  {fmt(a.returns["90d"])}
                </td>
                <td class="num mono" style={{ color: fmtColor(a.returns["365d"]) }}>
                  {fmt(a.returns["365d"])}
                </td>
                <td class="num mono">{a.sharpe !== null ? a.sharpe.toFixed(2) : "--"}</td>
                <td class="num muted">{a.volatility !== null ? `${a.volatility.toFixed(0)}%` : "--"}</td>
                <td class="num mono" style={{ color: "var(--red, #f87171)" }}>
                  {a.max_drawdown !== null ? `${a.max_drawdown.toFixed(1)}%` : "--"}
                </td>
                {bucket.assets.some((aa) => aa.funding_apr !== null) && (
                  <td class="num mono" style={{ color: "var(--green, #4ade80)" }}>
                    {a.funding_apr !== null ? `${a.funding_apr.toFixed(1)}%/yr` : "--"}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
