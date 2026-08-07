import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import { getRegime, type RegimeResponse } from "../lib/autonomous";
import { Hint } from "./Hint";
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

function Metric({
  label,
  value,
  sub,
  term,
}: {
  label: string;
  value: string;
  sub?: string;
  term?: string;
}) {
  return (
    <div class="metric">
      <span class="metric-label">
        {term ? <Hint term={term}>{label}</Hint> : label}
      </span>
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
        <p class="faint">No regime yet &mdash; the system waits for enough macro data before reading the market.</p>
      </div>
    );
  }

  const v = regime.value;
  return (
    <div class="regime-panel">
      <div class="regime-header">
        <span class={badgeClass(v.cycle_phase)}>
          <span class="badge-label">
            <Hint term="cycle_phase">cycle</Hint>
          </span>{" "}
          {v.cycle_phase}
        </span>
        <span class={badgeClass(v.risk_regime)}>
          <span class="badge-label">
            <Hint term="risk_regime">risk</Hint>
          </span>{" "}
          {v.risk_regime.replace("_", " ")}
        </span>
        <span class={badgeClass(v.inflation_regime)}>
          <span class="badge-label">
            <Hint term="inflation_regime">inflation</Hint>
          </span>{" "}
          {v.inflation_regime}
        </span>
        <span class={badgeClass(v.policy_stance)}>
          <span class="badge-label">
            <Hint term="policy_stance">policy</Hint>
          </span>{" "}
          {v.policy_stance}
        </span>
      </div>
      <div class="metric-grid">
        <Metric term="recession_probability" label="Recession probability" value={`${(v.recession_probability * 100).toFixed(0)}%`} sub={v.recession_assessment} />
        <Metric label="CPI year on year" value={`${v.inflation_yoy.toFixed(1)}%`} />
        <Metric term="output_gap" label="Output gap" value={v.output_gap != null ? `${v.output_gap.toFixed(1)}%` : "—"} sub={v.output_gap_known === false ? "unknown" : undefined} />
        <Metric term="yield_curve" label="Yield curve spread" value={v.yield_curve_spread != null ? `${v.yield_curve_spread.toFixed(2)}%` : "—"} sub={v.yield_curve_inverted ? "inverted" : "normal"} />
        <Metric term="sahm_rule" label="Sahm indicator" value={v.sahm_indicator != null ? v.sahm_indicator.toFixed(2) : "—"} sub={v.sahm_triggered ? "triggered" : "quiet"} />
        <Metric term="lei" label="Leading indicators, 6-month change" value={v.lei_change_6m != null ? `${v.lei_change_6m.toFixed(1)}%` : "—"} sub={v.lei_negative ? "declining" : "rising"} />
      </div>
      {regime.knowledge_date ? (
        <p class="faint" style={{ marginTop: "12px" }}>
          Assessed {regime.knowledge_date.slice(0, 10)} from FRED macro data
        </p>
      ) : null}
    </div>
  );
}
