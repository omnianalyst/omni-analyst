import { formatHitRate, type ScorecardRow } from "../lib/briefing";
import { Hint } from "./Hint";

function payoffLabel(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined) return "\u2014";
  return `${ratio.toFixed(2)}x`;
}

export function ScorecardTable({ rows }: { rows: ScorecardRow[] }) {
  if (rows.length === 0) {
    return (
      <p class="empty">
        No surfaced findings have been scored yet. The hit rate appears once
        predictions resolve &mdash; a number before then would be an opinion
        wearing a percentage sign.
      </p>
    );
  }
  return (
    <table class="coverage">
      <thead>
        <tr>
          <th>Method</th>
          <th class="num">Surfaced</th>
          <th class="num">Resolved</th>
          <th class="num">Hits</th>
          <th class="num"><Hint term="hit_rate">Hit rate</Hint></th>
          <th class="num"><Hint term="payoff_asymmetry">Payoff</Hint></th>
          <th class="num">Risked / paid</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.method}>
            <td class="claim-type">{r.method}</td>
            <td class="num">{r.surfaced}</td>
            <td class="num">{r.resolved}</td>
            <td class="num">{r.hits}</td>
            <td class="num">{formatHitRate(r.hit_rate)}</td>
            <td class="num">{payoffLabel(r.payoff_ratio)}</td>
            <td class="num">
              {r.avg_risk_pct != null && r.avg_payoff_pct != null
                ? `${r.avg_risk_pct.toFixed(1)}% / ${r.avg_payoff_pct.toFixed(1)}%`
                : "\u2014"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
