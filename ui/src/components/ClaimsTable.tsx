import type { Claim } from "../lib/api";

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "\u2014";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function formatConfidence(value: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "\u2014";
  return Number(value).toFixed(2);
}

export function ClaimsTable({ claims }: { claims: Claim[] }) {
  if (claims.length === 0) {
    return (
      <p class="empty">
        No claims of this type recorded for this entity. That is honest
        emptiness, not a failed fetch.
      </p>
    );
  }
  return (
    <table class="coverage claims">
      <thead>
        <tr>
          <th>Key</th>
          <th>Value</th>
          <th>Source</th>
          <th>Event</th>
          <th class="num">Confidence</th>
        </tr>
      </thead>
      <tbody>
        {claims.map((c) => (
          <tr class="row" key={c.id}>
            <td class="claim-type">{c.key ?? "\u2014"}</td>
            <td class="claim-value">{formatValue(c.value)}</td>
            <td>
              {c.source}
              {/* A byo_only claim only ever reaches its owner through the
                  visibility CTE, so this badge is truthful for the viewer. */}
              {c.redistributable === "byo_only" ? (
                <span class="byo" title="visible only via your key"> BYO</span>
              ) : null}
            </td>
            <td class="age">
              {c.event_date ? c.event_date.slice(0, 10) : "\u2014"}
            </td>
            <td class="num">{formatConfidence(c.confidence)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
