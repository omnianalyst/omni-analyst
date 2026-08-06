import {
  formatConfidence,
  formatHitRate,
  type BriefingFinding,
  type DeductionLayer,
} from "../lib/briefing";

function asStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) =>
    typeof entry === "string" ? entry : JSON.stringify(entry),
  );
}

function chainSummary(chain: DeductionLayer[]): string {
  return chain
    .map((c) => {
      if (c.layer === "macro") return `${c.cycle_phase}/${c.risk_regime?.replace("_", " ")}`;
      if (c.layer === "sector") return `${c.etf_symbol} ${c.trend}/${c.macro_alignment}`;
      if (c.layer === "stock") return `${c.direction} @ ${formatConfidence(c.confidence)}`;
      return c.layer;
    })
    .join(" -> ");
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
    <div class="evidence-col">
      <p class="evidence-title">{title}</p>
      {items.length === 0 ? (
        <p class="evidence-empty">{emptyText}</p>
      ) : (
        <ul class="evidence-list">
          {items.map((text, i) => (
            <li key={i} class="evidence-item">
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
      <div class="evidence-grid">
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
      {finding.deduction_chain && finding.deduction_chain.length > 1 ? (
        <div class="deduction-box">
          <p class="deduction-title">Deduction chain</p>
          <p class="deduction-summary">{chainSummary(finding.deduction_chain)}</p>
        </div>
      ) : null}
    </li>
  );
}
