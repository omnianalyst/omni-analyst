import { useState } from "preact/hooks";
import { describeError } from "../lib/api";
import {
  AuthRequiredError,
  postOverlap,
  type ExposureResult,
  type PositionInput,
} from "../lib/exposure";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

type AnalysisState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; data: ExposureResult }
  | { kind: "error"; message: string; detail?: string };

const DEFAULT_POSITIONS: PositionInput[] = [
  { symbol: "VTI", allocation: "0.40" },
  { symbol: "VXUS", allocation: "0.20" },
  { symbol: "QQQ", allocation: "0.10" },
  { symbol: "TLT", allocation: "0.15" },
  { symbol: "GLD", allocation: "0.10" },
  { symbol: "SLV", allocation: "0.05" },
];

function formatPct(weight: string): string {
  const n = parseFloat(weight);
  if (isNaN(n)) return weight;
  return (n * 100).toFixed(2) + "%";
}

export function ExposureView() {
  const [positions, setPositions] = useState<PositionInput[]>(DEFAULT_POSITIONS);
  const [state, setState] = useState<AnalysisState>({ kind: "idle" });

  async function analyze() {
    setState({ kind: "loading" });
    try {
      const result = await postOverlap({ positions });
      setState({ kind: "ok", data: result });
    } catch (err) {
      if (err instanceof AuthRequiredError) {
        setState({
          kind: "error",
          message: "Sign in required.",
          detail: "Holdings may be private to their owner.",
        });
        return;
      }
      const { message, detail } = describeError(err);
      setState({ kind: "error", message, detail });
    }
  }

  function updateRow(i: number, field: keyof PositionInput, value: string) {
    setPositions((prev) => {
      const next = [...prev];
      next[i] = { ...next[i], [field]: value };
      return next;
    });
  }

  function addRow() {
    setPositions((prev) => [...prev, { symbol: "", allocation: "0.00" }]);
  }

  function removeRow(i: number) {
    setPositions((prev) => prev.filter((_, idx) => idx !== i));
  }

  return (
    <div class="exposure-view">
      <header class="page-head">
        <h1>Exposure</h1>
        <p class="muted">
          What this portfolio actually holds through its ETFs, where the holdings
          overlap, and which names are concentrated beyond what any single fund
          sheet shows.
        </p>
      </header>

      <section class="panel">
        <h2 class="panel-title">Composition</h2>
        <table class="data-table">
          <thead>
            <tr>
              <th>ETF</th>
              <th>Allocation</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {positions.map((pos, i) => (
              <tr key={i}>
                <td>
                  <input
                    type="text"
                    value={pos.symbol}
                    onInput={(e) =>
                      updateRow(
                        i,
                        "symbol",
                        (e.target as HTMLInputElement).value.toUpperCase(),
                      )
                    }
                    placeholder="VTI"
                    style={{ width: "6em", textTransform: "uppercase" }}
                  />
                </td>
                <td>
                  <input
                    type="text"
                    value={pos.allocation}
                    onInput={(e) =>
                      updateRow(
                        i,
                        "allocation",
                        (e.target as HTMLInputElement).value,
                      )
                    }
                    placeholder="0.40"
                    style={{ width: "6em" }}
                  />
                </td>
                <td>
                  <button
                    type="button"
                    class="btn-mini"
                    onClick={() => removeRow(i)}
                    aria-label={`Remove ${pos.symbol || "row"}`}
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div style={{ display: "flex", gap: "12px", marginTop: "12px" }}>
          <button type="button" class="btn-secondary" onClick={addRow}>
            Add position
          </button>
          <button
            type="button"
            class="btn-primary"
            onClick={analyze}
            disabled={state.kind === "loading" || positions.length === 0}
          >
            {state.kind === "loading" ? "Analyzing..." : "Analyze exposure"}
          </button>
        </div>
      </section>

      {state.kind === "loading" && <Loading label="Computing exposure..." />}
      {state.kind === "error" && (
        <ErrorState message={state.message} detail={state.detail} />
      )}
      {state.kind === "ok" && <Results data={state.data} />}
    </div>
  );
}

function Results({ data }: { data: ExposureResult }) {
  const totalAllocation = data.bucket_exposure.reduce(
    (sum, b) => sum + parseFloat(b.allocation),
    0,
  );

  return (
    <>
      <section class="panel">
        <h2 class="panel-title">Bucket exposure</h2>
        {data.bucket_exposure.length === 0 ? (
          <p class="muted">No bucket data.</p>
        ) : (
          <div class="bucket-bars">
            {data.bucket_exposure.map((b) => (
              <BucketBar
                key={b.bucket}
                bucket={b.bucket}
                allocation={b.allocation}
                total={totalAllocation || 1}
              />
            ))}
          </div>
        )}
      </section>

      <section class="panel">
        <h2 class="panel-title">
          Concentration{" "}
          <span class="muted">
            ({data.concentration.length} flagged)
          </span>
        </h2>
        {data.concentration.length === 0 ? (
          <p class="muted">
            No single company exceeds the threshold across these funds.
          </p>
        ) : (
          <table class="data-table">
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Effective weight</th>
                <th>Held through</th>
              </tr>
            </thead>
            <tbody>
              {data.concentration.map((c) => (
                <tr key={c.ticker}>
                  <td class="mono">{c.ticker}</td>
                  <td class="mono">{formatPct(c.total_weight)}</td>
                  <td class="mono">{c.source_etfs.join(", ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section class="panel">
        <h2 class="panel-title">Overlap</h2>
        {data.overlaps.length === 0 ? (
          <p class="muted">No material overlap detected between these funds.</p>
        ) : (
          <table class="data-table">
            <thead>
              <tr>
                <th>Fund A</th>
                <th>Fund B</th>
                <th>Shared weight</th>
              </tr>
            </thead>
            <tbody>
              {data.overlaps.map((o) => (
                <tr key={`${o.etf_a}-${o.etf_b}`}>
                  <td class="mono">{o.etf_a}</td>
                  <td class="mono">{o.etf_b}</td>
                  <td class="mono">{formatPct(o.shared_weight)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section class="panel">
        <h2 class="panel-title">Top holdings</h2>
        {data.top_holdings.length === 0 ? (
          <p class="muted">
            No holdings data found. ETF holdings need to be ingested first.
          </p>
        ) : (
          <table class="data-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Ticker</th>
                <th>Effective weight</th>
              </tr>
            </thead>
            <tbody>
              {data.top_holdings.slice(0, 20).map((h, i) => (
                <tr key={h.ticker}>
                  <td class="muted">{i + 1}</td>
                  <td class="mono">{h.ticker}</td>
                  <td class="mono">{formatPct(h.weight)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}

function BucketBar({
  bucket,
  allocation,
  total,
}: {
  bucket: string;
  allocation: string;
  total: number;
}) {
  const pct = (parseFloat(allocation) / total) * 100;
  const label = bucket.replace(/_/g, " ");
  return (
    <div class="bucket-row">
      <span class="bucket-label" style={{ textTransform: "capitalize" }}>
        {label}
      </span>
      <div class="bucket-track">
        <div class="bucket-fill" style={{ width: `${pct}%` }} />
      </div>
      <span class="bucket-pct mono">{pct.toFixed(1)}%</span>
    </div>
  );
}
