import { useState } from "preact/hooks";
import {
  formatConfidence,
  type BriefingFinding,
  type DeductionLayer,
} from "../lib/briefing";
import { Hint } from "./Hint";
import {
  asymmetry,
  chainSteps,
  confidenceWord,
  directionGlyph,
  directionWord,
  hitRateFelt,
  invalidationLevel,
  oddsOfWrong,
  priceLabel,
} from "../lib/explain";

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
  term,
}: {
  title: string;
  items: string[];
  emptyText: string;
  term?: string;
}) {
  return (
    <div class="evidence-col">
      <p class="evidence-title">
        {term ? <Hint term={term}>{title}</Hint> : title}
      </p>
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

const CHAIN_GLYPH: Record<string, string> = {
  macro: "\u25CF",
  sector: "\u25B7",
  stock: "\u2605",
};

export function FindingCard({ finding }: { finding: BriefingFinding }) {
  const [showEvidence, setShowEvidence] = useState(false);
  const supporting = asStrings(finding.supporting);
  const disconfirming = asStrings(finding.disconfirming);
  const dir = finding.direction;
  const dirClass = dir === "up" ? "up" : dir === "down" ? "down" : "flat";
  const confPct = Math.max(0, Math.min(100, Math.round((finding.confidence || 0) * 100)));
  const invalidation = invalidationLevel(dir, finding.upper_barrier, finding.lower_barrier);
  // From the measured hit rate, never from the model's own confidence -- see
  // oddsOfWrong. The two disagreeing on one card is what this fixes.
  const wrong = oddsOfWrong(finding.calibrated_hit_rate);
  const steps = chainSteps(finding.deduction_chain as DeductionLayer[] | undefined);
  // Never inferred from an empty list -- that is the ambiguity the flag exists
  // to resolve. Absent (an older API) is treated as not searched, which is the
  // claim that cannot be wrong.
  const searched = finding.evidence_searched === true;
  // What the call's own geometry pays: risking X% to make Y%. Fixed at write
  // time by the prediction's barriers, so it is as pre-registered as the
  // invalidation level -- probability on one side, payoff on the other.
  const payoff = asymmetry(
    finding.direction,
    finding.entry_price,
    finding.upper_barrier,
    finding.lower_barrier,
  );

  return (
    <li class={`claim-card claim-${dirClass}`} key={finding.id}>
      <div class="claim-head">
        <span class={`claim-dir claim-dir-${dirClass}`} aria-label={`trend ${directionWord(dir)}`}>
          <span class="claim-glyph" aria-hidden="true">{directionGlyph(dir)}</span>
          {directionWord(dir)}
        </span>
        <span class="claim-ticker">{finding.entity.symbol ?? "\u2014"}</span>
        {finding.entity.name ? (
          <span class="claim-name muted">{finding.entity.name}</span>
        ) : null}
        <span class="claim-method">{finding.method}</span>
      </div>

      <div class="claim-confidence">
        <div class="conf-bar" aria-hidden="true">
          <div class={`conf-fill conf-fill-${dirClass}`} style={{ width: `${confPct}%` }} />
        </div>
        <Hint term="confidence">
          <span class="conf-word">{confidenceWord(finding.confidence)}</span>
        </Hint>
        {wrong ? <span class="conf-wrong">{wrong}</span> : null}
        <Hint term="hit_rate">
          <span class="conf-track">{hitRateFelt(finding.calibrated_hit_rate)}</span>
        </Hint>
      </div>

      {steps.length > 0 ? (
        <ol class="claim-chain">
          {steps.map((s, i) => (
            <li key={i} class={`chain-step chain-${s.layer}`}>
              <span class="chain-glyph" aria-hidden="true">{CHAIN_GLYPH[s.layer] || "\u2022"}</span>
              <span class="chain-text">{s.text}</span>
            </li>
          ))}
        </ol>
      ) : null}

      {invalidation !== null ? (
        <p class="claim-invalidation">
          <Hint term="invalidation">Proven wrong</Hint>{" "}
          {dir === "up" ? "below" : dir === "down" ? "above" : "at"}{" "}
          <strong>{priceLabel(invalidation)}</strong>
          <span class="muted"> &middot; called at {priceLabel(finding.entry_price)}</span>
        </p>
      ) : null}

      {payoff !== null ? (
        <p class="claim-payoff">
          <Hint term="payoff_asymmetry">
            <span class="mono">
              risking {payoff.riskPct.toFixed(1)}% to make {payoff.payoffPct.toFixed(1)}%
            </span>
          </Hint>{" "}
          <span class="conf-wrong">{"\u00b7"} {payoff.ratio.toFixed(1)}:1</span>
        </p>
      ) : null}

      <button
        type="button"
        class="claim-evidence-toggle"
        onClick={() => setShowEvidence((v) => !v)}
        aria-expanded={showEvidence}
      >
        {showEvidence
          ? "Hide evidence"
          : searched
            ? `Evidence (${supporting.length} for, ${disconfirming.length} against)`
            : `Evidence (${supporting.length} for, not assessed against)`}
      </button>
      {showEvidence ? (
        <div class="evidence-grid">
          <EvidenceColumn
            term="supporting"
            title="For this call"
            items={supporting}
            emptyText="none cited"
          />
          <EvidenceColumn
            term="disconfirming"
            title="Against this call"
            items={disconfirming}
            emptyText={
              searched
                ? "the checks ran and found none"
                : "not assessed \u2014 this call predates the counter-case checks"
            }
          />
        </div>
      ) : null}
    </li>
  );
}
