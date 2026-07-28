import {
  explainRefusal,
  refusalTotal,
  type RefusalCounts,
} from "../lib/briefing";

export function RefusalList({ counts }: { counts: RefusalCounts }) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) {
    return (
      <p class="empty">
        Nothing has been refused yet. When the system stays quiet, the reason is
        recorded here &mdash; the denominator behind the scorecard.
      </p>
    );
  }
  const total = refusalTotal(counts);
  return (
    <ul class="gaps">
      {entries.map(([reason, n]) => (
        <li class="gap-row" key={reason}>
          <div class="gap-head">
            <span class="gap-type">{explainRefusal(reason)}</span>
            <span class="gap-class">{n}</span>
          </div>
        </li>
      ))}
      <li class="gap-row">
        <div class="gap-head">
          <span class="muted">
            total considered and not surfaced
          </span>
          <span class="gap-class">{total}</span>
        </div>
      </li>
    </ul>
  );
}
