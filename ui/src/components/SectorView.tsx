import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import { getSectors, type SectorEntry } from "../lib/autonomous";
import { Loading } from "./Loading";
import { ErrorState } from "./ErrorState";

function trendColor(trend: string): string {
  if (trend === "uptrend") return "var(--pos, #4ade80)";
  if (trend === "downtrend") return "var(--neg, #f87171)";
  return "var(--faint)";
}

function alignmentBadge(align: string): string {
  if (align === "favorable") return "badge badge-pos";
  if (align === "unfavorable") return "badge badge-neg";
  return "badge";
}

function rsBar(percentile: number): preact.JSX.Element {
  const width = Math.max(Math.min(percentile * 100, 100), 0);
  return (
    <div class="rs-bar-track">
      <div class="rs-bar-fill" style={{ width: `${width}%` }} />
    </div>
  );
}

export function SectorView() {
  const [sectors, setSectors] = useState<SectorEntry[] | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    getSectors()
      .then(setSectors)
      .catch((e) => setError(e));
  }, []);

  if (error) {
    const { message, detail } = describeError(error);
    return <ErrorState message={message} detail={detail} />;
  }
  if (!sectors) return <Loading />;
  if (sectors.length === 0) {
    return (
      <div class="card">
        <p class="faint">No sector scores. The scanner needs ETF prices (Polygon). Sign in as the operator to view byo_only scores.</p>
      </div>
    );
  }

  const sorted = [...sectors].sort(
    (a, b) => (b.score.rs_percentile ?? 0) - (a.score.rs_percentile ?? 0),
  );

  return (
    <table class="sector-table">
      <thead>
        <tr>
          <th>Sector</th>
          <th>Trend</th>
          <th>RS percentile</th>
          <th>Macro alignment</th>
        </tr>
      </thead>
      <tbody>
        {sorted.map((s) => (
          <tr key={s.symbol}>
            <td>
              <strong>{s.symbol}</strong>
              <span class="faint"> {s.name}</span>
            </td>
            <td>
              <span style={{ color: trendColor(s.score.trend) }}>{s.score.trend}</span>
            </td>
            <td>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                {rsBar(s.score.rs_percentile)}
                <span class="mono">{(s.score.rs_percentile * 100).toFixed(0)}</span>
              </div>
            </td>
            <td>
              <span class={alignmentBadge(s.score.macro_alignment)}>{s.score.macro_alignment}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
