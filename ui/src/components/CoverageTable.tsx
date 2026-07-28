import type { CoverageGroup } from "../lib/api";
import { formatAge, stalenessTier } from "../lib/age";

function formatConfidence(value: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "\u2014";
  return Number(value).toFixed(2);
}

export function CoverageTable({ groups }: { groups: CoverageGroup[] }) {
  if (groups.length === 0) {
    return (
      <p class="empty">
        No coverage recorded for this entity yet. That is honest emptiness, not a
        loaded view that forgot to render.
      </p>
    );
  }
  return (
    <table class="coverage">
      <thead>
        <tr>
          <th>Claim type</th>
          <th class="num">Claims</th>
          <th>Newest</th>
          <th class="num">Sources</th>
          <th class="num">Confidence</th>
        </tr>
      </thead>
      <tbody>
        {groups.map((g) => {
          const tier = stalenessTier(g.age_seconds);
          return (
            <tr class={`row tier-${tier}`} key={g.claim_type}>
              <td class="claim-type">{g.claim_type}</td>
              <td class="num">{g.count}</td>
              <td class="age">
                <span class={`dot tier-${tier}`} aria-hidden="true" />
                <span class={`age-text tier-${tier}`}>
                  {formatAge(g.age_seconds)}
                </span>
              </td>
              <td class="num">{g.source_count}</td>
              <td class="num">{formatConfidence(g.mean_confidence)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
