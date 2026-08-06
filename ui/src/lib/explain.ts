// Deterministic plain-English renderers for the claim-card facets.
//
// No LLM, no free-form narration. Every string below is a fixed template over
// structured data, on purpose: a narrator that invents wording the analysis did
// not earn is how an honest intelligence product turns into a tips product that
// lies (AUTONOMOUS_PLAN §10.4). The numbers do the talking; this file only
// chooses the agreed words around them.

import type { DeductionLayer } from "./briefing";

// -- Confidence: felt, not just a number -------------------------------------
//
// "0.74" is opaque. A novice reads it as "high" and stops. The discipline is to
// surface BOTH a calibrated word AND the inverse -- roughly how often a call
// like this is WRONG -- because over-trust lives in the gap between "74% right"
// and "1 in 4 wrong". The word and the fraction are the same fact; both render.

export function confidenceWord(c: number | null | undefined): string {
  if (c === null || c === undefined || Number.isNaN(c)) return "uncalibrated";
  if (c < 0.4) return "low conviction";
  if (c < 0.6) return "moderate conviction";
  if (c < 0.75) return "high conviction";
  return "very high conviction";
}

export function oddsOfWrong(c: number | null | undefined): string | null {
  if (c === null || c === undefined || Number.isNaN(c) || c >= 0.97 || c <= 0.03) {
    return null;
  }
  const n = Math.round(1 / (1 - c));
  return `~1 in ${n} calls like this are wrong`;
}

// -- Direction ---------------------------------------------------------------

export function directionGlyph(d: "up" | "down" | null | undefined): string {
  if (d === "up") return "\u25B2"; // ▲
  if (d === "down") return "\u25BC"; // ▼
  return "\u2014";
}

export function directionWord(d: "up" | "down" | null | undefined): string {
  if (d === "up") return "up";
  if (d === "down") return "down";
  return "neutral";
}

// The level that proves the call wrong. For a trend-up call the invalidation is
// the lower barrier (price falls below it -> the trend the call was predicated
// on is broken); for trend-down it is the upper barrier. Returning null when the
// relevant barrier is missing keeps the card honest rather than inventing a level.
export function invalidationLevel(
  dir: "up" | "down" | null | undefined,
  upper: number | null,
  lower: number | null,
): number | null {
  if (dir === "up") return lower;
  if (dir === "down") return upper;
  return null;
}

// -- Track record ------------------------------------------------------------

export function hitRateFelt(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || Number.isNaN(rate)) {
    return "not yet calibrated";
  }
  const pct = Math.round(rate * 100);
  return `right ~${pct}% of the time on calls like this`;
}

// -- Market regime canopy (plain-English) ------------------------------------
//
// Generic, honest, non-advice implications only. These describe what has
// historically suited a regime -- never "you should buy". The line between
// intelligence and regulated advice is the verb: "is/has" (analysis), not
// "should" (action).

export interface RegimeLike {
  cycle_phase?: string | null;
  risk_regime?: string | null;
  inflation_regime?: string | null;
}

export function regimeHeadline(r: RegimeLike | null | undefined): string {
  if (!r || !r.cycle_phase) return "Market regime is still being assessed";
  const phase = r.cycle_phase;
  const risk = (r.risk_regime || "").replace("_", " ");
  if (risk) return `Markets: ${phase}, ${risk}`;
  return `Markets: ${phase}`;
}

export function regimeImplication(r: RegimeLike | null | undefined): string {
  if (!r || !r.cycle_phase) {
    return "The system waits for enough data before reading the regime -- silence here is honesty, not a failure.";
  }
  const phase = r.cycle_phase;
  const risk = r.risk_regime || "transition";
  if (phase === "expansion" && risk === "risk_on") {
    return "Broad growth exposure has historically suited expansion + risk-on regimes.";
  }
  if (phase === "contraction" || risk === "risk_off") {
    return "Defensive, lower-volatility names have historically held up better in downturns.";
  }
  if (phase === "peak") {
    return "Late-cycle; sector leadership has historically narrowed near peaks.";
  }
  if (phase === "trough") {
    return "Early-cycle leaders have historically led recoveries from troughs.";
  }
  return "No clean regime edge right now -- conviction is harder to come by, and the gate stays stricter.";
}

// -- Deduction chain as plain-English steps ----------------------------------
//
// Each layer becomes one human step. The chain is macro -> sector -> stock by
// construction; rendering it as ordered steps lets a novice follow the reasoning
// that produced the call, which is the whole point of "intelligence that shows
// its work".

export interface ChainStep {
  layer: "macro" | "sector" | "stock" | string;
  text: string;
}

export function chainSteps(chain: DeductionLayer[] | undefined): ChainStep[] {
  if (!chain || chain.length === 0) return [];
  return chain.map((c) => {
    if (c.layer === "macro") {
      const parts = [
        c.cycle_phase ? `economy ${c.cycle_phase}` : null,
        c.risk_regime ? c.risk_regime.replace("_", " ") : null,
      ].filter(Boolean);
      return { layer: "macro", text: parts.join(", ") || "macro read" };
    }
    if (c.layer === "sector") {
      const sym = c.etf_symbol || c.sector_etf || "sector";
      const trend = c.trend ? `${c.trend}` : null;
      const align = c.macro_alignment ? `macro-${c.macro_alignment}` : null;
      const parts = [`${sym}`, trend, align].filter(Boolean);
      return { layer: "sector", text: parts.join(" · ") };
    }
    if (c.layer === "stock") {
      const dir = directionWord(c.direction as "up" | "down" | null);
      const conv = confidenceWord(c.confidence);
      return { layer: "stock", text: `${dir}, ${conv}` };
    }
    return { layer: c.layer, text: c.layer };
  });
}

// -- Number formatting -------------------------------------------------------

export function priceLabel(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "\u2014";
  return `$${Number(v).toFixed(2)}`;
}
