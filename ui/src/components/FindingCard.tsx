import {
  formatConfidence,
  formatHitRate,
  type BriefingFinding,
} from "../lib/briefing";

function asStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) =>
    typeof entry === "string" ? entry : JSON.stringify(entry),
  );
}

// One column shape, rendered for both supporting and disconfirming, so neither
// side can drift to a weaker weight. The disconfirming column must never read as
// "not looked for": an empty list is "none found", not a blank.
function EvidenceColumn({
  title,
  items,
  emptyText,
}: {
  title: string;
  items: string[];
  emptyText: string;
}) {
  return (
    <div
      style={{
        padding: "12px",
        background: "var(--bg)",
        border: "1px solid var(--border)",
        borderRadius: "8px",
        minWidth: 0,
      }}
    >
      <p
        style={{
          margin: "0 0 8px",
          fontSize: "12px",
          textTransform: "uppercase",
          letterSpacing: "0.08em",
          color: "var(--faint)",
        }}
      >
        {title}
      </p>
      {items.length === 0 ? (
        <p
          style={{
            margin: 0,
            fontFamily: "var(--mono)",
            fontSize: "13px",
            color: "var(--faint)",
          }}
        >
          {emptyText}
        </p>
      ) : (
        <ul style={{ margin: 0, paddingLeft: "18px" }}>
          {items.map((text, i) => (
            <li
              key={i}
              style={{
                fontFamily: "var(--mono)",
                fontSize: "13px",
                color: "var(--muted)",
                marginBottom: "4px",
                wordBreak: "break-word",
              }}
            >
              {text}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function FindingCard({ finding }: { finding: BriefingFinding }) {
  const supporting = asStrings(finding.supporting);
  const disconfirming = asStrings(finding.disconfirming);
  return (
    <li class="gap-row" key={finding.id}>
      <div class="gap-head">
        <span class="gap-type">
          {finding.entity.symbol ?? "\u2014"}
          {finding.entity.name ? (
            <span class="gap-key"> &middot; {finding.entity.name}</span>
          ) : null}
        </span>
        <span class="gap-class">{finding.method}</span>
      </div>
      <div class="gap-meta">
        <span>
          confidence <strong>{formatConfidence(finding.confidence)}</strong>
        </span>
        <span>
          threshold <strong>{formatConfidence(finding.threshold)}</strong>
        </span>
        <span>
          hit rate <strong>{formatHitRate(finding.calibrated_hit_rate)}</strong>
        </span>
        {finding.created_at ? (
          <span class="faint">surfaced {finding.created_at.slice(0, 10)}</span>
        ) : null}
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: "12px",
          marginTop: "12px",
        }}
      >
        <EvidenceColumn
          title="Supporting"
          items={supporting}
          emptyText="none cited"
        />
        <EvidenceColumn
          title="Disconfirming"
          items={disconfirming}
          emptyText="none found"
        />
      </div>
    </li>
  );
}
