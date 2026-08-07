import type { CoverageGroup } from "../lib/api";
import { formatAge, stalenessTier } from "../lib/age";
import { Hint } from "./Hint";

function formatConfidence(value: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "\u2014";
  return Number(value).toFixed(2);
}

export function CoverageTable({
  groups,
  entityId,
  selectedType,
  contradictionTypes,
}: {
  groups: CoverageGroup[];
  entityId: string;
  selectedType?: string | null;
  contradictionTypes?: Set<string>;
}) {
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
          <th>
            <Hint term="claim">Kind of claim</Hint>
          </th>
          <th class="num">Held</th>
          <th>
            <Hint term="freshness">Newest</Hint>
          </th>
          <th class="num">Sources</th>
          <th class="num">
            <Hint term="confidence">Confidence</Hint>
          </th>
        </tr>
      </thead>
      <tbody>
        {groups.map((g) => {
          const tier = stalenessTier(g.age_seconds);
          const selected = g.claim_type === selectedType;
          return (
            <tr
              class={`row tier-${tier}${selected ? " selected" : ""}`}
              key={g.claim_type}
            >
              <td class="claim-type">
                <a href={`/entity/${entityId}?type=${encodeURIComponent(g.claim_type)}`}>
                  {g.claim_type}
                </a>
                {contradictionTypes?.has(g.claim_type) ? (
                  <Hint term="contradiction">
                    <span class="conflict-pill">conflict</span>
                  </Hint>
                ) : null}
              </td>
              <td class="num">
                {g.count}
                {g.private_count > 0 ? (
                  <Hint term="byo_scoped">
                    <span class="byo"> incl. {g.private_count} yours</span>
                  </Hint>
                ) : null}
              </td>
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
