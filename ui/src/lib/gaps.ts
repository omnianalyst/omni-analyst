import type { Gap } from "./api";

// The gap engine ranks `contradictory` first (GAP_CLASS_WEIGHTS = 1000.0): two
// sources looked at the same fact and disagreed. Deriving the set of affected
// claim types from the gaps feed keeps the gap engine the single source of
// truth -- the coverage view marks a row only because a contradiction gap
// exists for it, never from a second server-side computation.
export function contradictionTypes(gaps: Gap[]): Set<string> {
  const types = new Set<string>();
  for (const g of gaps) {
    if (g.gap_class === "contradictory" && g.claim_type) {
      types.add(g.claim_type);
    }
  }
  return types;
}
