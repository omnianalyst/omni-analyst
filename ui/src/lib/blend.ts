// Blend arithmetic for The Portfolio. Isolated here because the values it
// combines are already in percent units -- the one mistake this module exists
// to prevent is multiplying them by 100 again.

export function equalWeightAverage(values: Array<number | null | undefined>): number {
  const present = values.filter((value): value is number => typeof value === "number");
  if (present.length === 0) return 0;
  return present.reduce((sum, value) => sum + value, 0) / present.length;
}

export function weightShare(count: number): number {
  if (count <= 0) return 0;
  return 1 / count;
}

// Each holding's share of the portfolio's total risk. With equal capital
// weights the weight cancels, so a holding's risk share is its measured
// volatility over the sum of all holdings' volatilities. Inputs are the
// measured annualised volatilities in percent; a holding with no measured
// volatility contributes nothing rather than being priced as zero-risk.
export function riskShares(vols: Array<number | null | undefined>): number[] {
  const present = vols.filter((value): value is number => typeof value === "number");
  const total = present.reduce((sum, value) => sum + value, 0);
  if (total <= 0) return [];
  return vols.map((value) =>
    typeof value === "number" ? value / total : 0,
  );
}
