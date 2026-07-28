import { formatHitRate, type ScorecardRow } from "../lib/briefing";

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
          <th class="num">Hit rate</th>
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
          </tr>
        ))}
      </tbody>
    </table>
  );
}
