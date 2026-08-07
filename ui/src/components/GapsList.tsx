import type { Gap } from "../lib/api";
import { Hint } from "./Hint";

function safeClass(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function formatScore(value: number): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "\u2014";
  return Number(value).toFixed(2);
}

function formatDetail(detail: unknown): string | null {
  if (detail === null || detail === undefined) return null;
  return typeof detail === "string" ? detail : JSON.stringify(detail, null, 2);
}

export function GapsList({ gaps }: { gaps: Gap[] }) {
  if (gaps.length === 0) {
    return (
      <p class="empty">
        No open gaps. The network&apos;s coverage matches demand for this entity.
      </p>
    );
  }
  return (
    <ul class="gaps">
      {gaps.map((gap) => {
        const detail = formatDetail(gap.detail);
        return (
          <li class="gap-row" key={gap.id}>
            <div class="gap-head">
              <span class="gap-type">
                {gap.claim_type}
                {gap.key ? <span class="gap-key"> &middot; {gap.key}</span> : null}
              </span>
              <span class={`gap-class cls-${safeClass(gap.gap_class)}`}>
                {gap.gap_class}
              </span>
            </div>
            <div class="gap-meta">
              <span>
                score <strong>{formatScore(gap.score)}</strong>
              </span>
              <span>
                attempts <strong>{gap.attempts}</strong>
              </span>
              {gap.audience_user_id ? (
                // The tooltip used to be the raw audience UUID, which told a
                // reader nothing and leaked an internal id into the surface.
                <Hint term="byo_scoped">
                  <span class="byo">Your key only</span>
                </Hint>
              ) : null}
              {gap.detected_at ? (
                <span class="faint">detected {gap.detected_at.slice(0, 10)}</span>
              ) : null}
            </div>
            {detail ? <pre class="gap-detail">{detail}</pre> : null}
          </li>
        );
      })}
    </ul>
  );
}
