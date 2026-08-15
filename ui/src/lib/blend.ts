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
