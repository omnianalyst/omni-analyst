import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import { getRegime, type RegimeResponse } from "../lib/autonomous";
import { Loading } from "./Loading";
import { ErrorState } from "./ErrorState";

function badgeClass(value: string): string {
  const positive = ["expansion", "risk_on", "cooling", "dovish", "favorable", "uptrend"];
  const negative = ["contraction", "risk_off", "rising", "hawkish", "unfavorable", "downtrend"];
  const lower = value.toLowerCase();
  if (positive.includes(lower)) return "badge badge-pos";
  if (negative.includes(lower)) return "badge badge-neg";
  return "badge";
}

function Metric({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div class="metric">
      <span class="metric-label">{label}</span>
      <span class="metric-value">{value}</span>
      {sub ? <span class="metric-sub">{sub}</span> : null}
    </div>
  );
}

export function RegimeView() {
  const [regime, setRegime] = useState<RegimeResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    getRegime()
      .then((r) => setRegime(r))
      .catch((e) => setError(e));
  }, []);

  if (error) {
    const { message, detail } = describeError(error);
    return <ErrorState message={message} detail={detail} />;
  }
  if (!regime) return <Loading />;
  if (!regime.value || !regime.value.cycle_phase) {
    return (
      <div class="card">
        <p class="faint">No regime assessment yet. The macro loop abstains until FRED data is available.</p>
      </div>
    );
  }

  const v = regime.value;
  return (
    <div class="regime-panel">
      <div class="regime-header">
        <span class={badgeClass(v.cycle_phase)}>{v.cycle_phase}</span>
        <span class={badgeClass(v.risk_regime)}>{v.risk_regime.replace("_", " ")}</span>
        <span class={badgeClass(v.inflation_regime)}>inflation {v.inflation_regime}</span>
        <span class={badgeClass(v.policy_stance)}>{v.policy_stance}</span>
      </div>
      <div class="metric-grid">
        <Metric label="Recession probability" value={`${(v.recession_probability * 100).toFixed(0)}%`} sub={v.recession_assessment} />
        <Metric label="CPI YoY" value={`${v.inflation_yoy.toFixed(1)}%`} />
        <Metric label="Output gap" value={v.output_gap != null ? `${v.output_gap.toFixed(1)}%` : "—"} sub={v.output_gap_known === false ? "unknown" : undefined} />
        <Metric label="Yield curve spread" value={v.yield_curve_spread != null ? `${v.yield_curve_spread.toFixed(2)}%` : "—"} sub={v.yield_curve_inverted ? "inverted" : "normal"} />
        <Metric label="Sahm indicator" value={v.sahm_indicator != null ? v.sahm_indicator.toFixed(2) : "—"} sub={v.sahm_triggered ? "triggered" : "quiet"} />
        <Metric label="LEI 6m change" value={v.lei_change_6m != null ? `${v.lei_change_6m.toFixed(1)}%` : "—"} sub={v.lei_negative ? "declining" : "rising"} />
      </div>
      {regime.knowledge_date ? (
        <p class="faint" style={{ marginTop: "12px" }}>
          Assessed {regime.knowledge_date.slice(0, 10)} from FRED macro data
        </p>
      ) : null}
    </div>
  );
}
