// Monetary inputs are integer cents. Binary float multiplication silently
// re-rounds sub-cent text (1.005 -> 100 cents) and can exceed the exact
// integer range; both are refused here rather than rounded by a hidden rule.
export function parseCents(raw: string): number {
  const match = /^([+-]?)(\d+)(?:\.(\d{1,2}))?$/.exec(raw.trim());
  if (!match) {
    throw new Error("Enter a decimal amount with at most two fractional digits");
  }
  const magnitude = BigInt(match[2]) * 100n + BigInt((match[3] ?? "").padEnd(2, "0"));
  const cents = match[1] === "-" ? -magnitude : magnitude;
  const limit = BigInt(Number.MAX_SAFE_INTEGER);
  if (cents < -limit || cents > limit) {
    throw new Error("Amount exceeds the exact supported range");
  }
  return Number(cents);
}
